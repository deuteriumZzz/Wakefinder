"""Копитрейдинг на Solana — зеркалим вход/выход watchlist-кошельков, найденных
через DEX-агностичный wallet_watcher.py (balance-diff, не декодер конкретных
DEX-инструкций). Тот же принцип, что и wakefinder/chains/eth/copytrade.py:
размер входа — доля НАШЕГО баланса (COPYTRADE_SIZE_PCT), не сумма кита; вход
только по консенсусу нескольких разных watched-кошельков; выход по
зеркальному триггеру ИЛИ стоп-лоссу (фоновая задача).

Ноги сделок строит Jupiter (тот же выбор, что и в backrun-арбитраже — не
кодируем инструкции конкретных DEX вручную), отправка — через Jito с
обязательным tip.

ponytail: позиции — плоский JSON-файл, переживает рестарт. Требует отдельного
процесса/кошелька от wakefinder.chains.solana.main (арбитраж), если работают
одновременно — общий кошелёк с параллельно отправляемыми транзакциями не
тестировался намеренно совместно, разделяйте кошельки для безопасности.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass

from jupiter_python_sdk.jupiter import Jupiter
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from wakefinder.chains.solana.main import _build_tip_tx, _sign_unsigned_tx, _tip_lamports
from wakefinder.chains.solana.sender import JitoBundleSender, to_base64
from wakefinder.chains.solana.wallet_watcher import WalletSwapWatcher
from wakefinder.common import heartbeat, killswitch, trade_log
from wakefinder.common.adaptive_tip import AdaptiveTipController
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.config import get_settings
from wakefinder.common.consensus import ConsensusTracker
from wakefinder.common.drawdown import check_drawdown
from wakefinder.common.interfaces import Bundle
from wakefinder.common.reconnect import with_reconnect

SLIPPAGE_BPS = 100

logger = logging.getLogger("wakefinder.solana.copytrade")


@dataclass
class Position:
    token: str
    token_in: str
    amount_held: int
    entry_amount_in: int
    watched_wallet: str
    opened_at: float


def _load_positions(path: str) -> dict[str, Position]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {k: Position(**v) for k, v in raw.items()}


def _save_positions(path: str, positions: dict[str, Position]) -> None:
    with open(path, "w") as f:
        json.dump({k: asdict(v) for k, v in positions.items()}, f, indent=2)


async def _swap_via_jupiter_and_send(
    client: AsyncClient, jupiter: Jupiter, sender: JitoBundleSender, keypair: Keypair, tip: AdaptiveTipController,
    input_mint: str, output_mint: str, amount_in: int,
) -> tuple[bool, int]:
    """Возвращает (included, ориентировочный amount_out — до слиппеджа)."""
    try:
        quote = await jupiter.quote(
            input_mint=input_mint, output_mint=output_mint, amount=amount_in,
            slippage_bps=SLIPPAGE_BPS, only_direct_routes=True,
        )
        expected_out = int(quote["outAmount"])
        unsigned = await jupiter.swap(
            input_mint=input_mint, output_mint=output_mint, amount=amount_in,
            slippage_bps=SLIPPAGE_BPS, only_direct_routes=True,
        )
    except Exception as exc:
        logger.error("Jupiter не смог построить транзакцию (%s)", type(exc).__name__)
        return False, 0

    tip_account = await sender.get_tip_account()
    tip_lamports = _tip_lamports(expected_out, tip.current_bps)
    blockhash = (await client.get_latest_blockhash()).value.blockhash

    swap_tx = _sign_unsigned_tx(unsigned, keypair)
    tip_tx = _build_tip_tx(keypair, tip_account, tip_lamports, blockhash)

    bundle = Bundle(raw_txs=[to_base64(bytes(swap_tx)), to_base64(bytes(tip_tx))], target_block=0)
    included = await sender.send(bundle)
    tip.record_outcome(included)
    return included, expected_out


async def _has_sufficient_balance(client: AsyncClient, owner: Pubkey, token_mint: str, amount_in: int) -> bool:
    if token_mint == "So11111111111111111111111111111111111111112":  # wrapped SOL
        balance = (await client.get_balance(owner)).value
        return balance >= amount_in + 20_000  # запас на fee/tip
    ata = get_associated_token_address(owner, Pubkey.from_string(token_mint))
    try:
        resp = await client.get_token_account_balance(ata)
    except Exception:
        return False
    return int(resp.value.amount) >= amount_in


async def _exit_position(client, jupiter, sender, keypair, tip, positions, positions_lock, positions_file, trade_log_file, token: str, reason: str):
    async with positions_lock:
        pos = positions.pop(token, None)
        if pos is not None:
            _save_positions(positions_file, positions)
    if pos is None:
        return
    included, amount_out = await _swap_via_jupiter_and_send(client, jupiter, sender, keypair, tip, pos.token, pos.token_in, pos.amount_held)
    logger.info("копитрейд-выход (%s): токен=%s included=%s", reason, token, included)
    trade_log.log_attempt(trade_log_file, "solana", "", amount_out, included, [], strategy="copytrade_exit", wallet=pos.watched_wallet)
    if reason == "стоп-лосс":
        settings = get_settings()
        send_telegram_alert(settings.telegram_bot_token, settings.telegram_chat_id, f"[wakefinder/solana copytrade] стоп-лосс: токен={token} included={included}")


async def _stop_loss_loop(client, jupiter, sender, keypair, tip, positions, positions_lock, positions_file, trade_log_file, stop_loss_pct, interval_seconds):
    while True:
        await asyncio.sleep(interval_seconds)
        async with positions_lock:
            snapshot = dict(positions)
        for token, pos in snapshot.items():
            try:
                quote = await jupiter.quote(
                    input_mint=pos.token, output_mint=pos.token_in, amount=pos.amount_held,
                    slippage_bps=SLIPPAGE_BPS, only_direct_routes=True,
                )
                current_value = int(quote["outAmount"])
            except Exception as exc:
                logger.warning("не удалось проверить цену позиции %s (%s)", token, type(exc).__name__)
                continue
            floor = pos.entry_amount_in * (100 - stop_loss_pct) // 100
            if current_value < floor:
                await _exit_position(client, jupiter, sender, keypair, tip, positions, positions_lock, positions_file, trade_log_file, token, "стоп-лосс")


async def run(
    watched_wallets: frozenset[str],
    token_allowlist: frozenset[str] = frozenset(),
    token_denylist: frozenset[str] = frozenset(),
):
    settings = get_settings()
    if not (settings.solana_rpc_ws_url and settings.solana_rpc_http_url and settings.solana_private_key):
        raise RuntimeError("SOLANA_RPC_WS_URL / SOLANA_RPC_HTTP_URL / SOLANA_PRIVATE_KEY не настроены")

    keypair = Keypair.from_base58_string(settings.solana_private_key.get_secret_value())
    client = AsyncClient(settings.solana_rpc_http_url.get_secret_value())
    jupiter = Jupiter(client, keypair)
    sender = JitoBundleSender(settings.jito_block_engine_url, keypair)
    tip = AdaptiveTipController(initial_bps=settings.profit_share_bps)

    positions = _load_positions(settings.solana_copytrade_positions_file)
    positions_lock = asyncio.Lock()
    consensus = ConsensusTracker(settings.copytrade_min_consensus_wallets, settings.copytrade_consensus_window_seconds)
    last_drawdown_check = 0.0

    watcher = WalletSwapWatcher(settings.solana_rpc_ws_url.get_secret_value(), client, watched_wallets)

    stop_loss_task = asyncio.create_task(
        _stop_loss_loop(
            client, jupiter, sender, keypair, tip, positions, positions_lock,
            settings.solana_copytrade_positions_file, settings.trade_log_file, settings.copytrade_stop_loss_pct,
            settings.copytrade_stop_loss_check_interval_seconds,
        )
    )
    heartbeat_path = os.path.join(settings.heartbeat_dir, "solana_copytrade.heartbeat")
    heartbeat_task = asyncio.create_task(heartbeat.loop(heartbeat_path, settings.heartbeat_interval_seconds))

    try:
        async for swap in with_reconnect(watcher.watch):
            if killswitch.is_engaged(settings.kill_switch_file):
                logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                send_telegram_alert(settings.telegram_bot_token, settings.telegram_chat_id, "[wakefinder/solana copytrade] kill switch присутствует — бот остановлен")
                return

            now = time.time()
            if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                last_drawdown_check = now
                status = check_drawdown(settings.trade_log_file, "solana", settings.drawdown_window_seconds, int(settings.max_drawdown_sol * 10**9))
                if status.breached:
                    logger.critical("просадка за окно %d lamports превысила лимит — включаю kill switch", status.realized_pnl)
                    send_telegram_alert(
                        settings.telegram_bot_token, settings.telegram_chat_id,
                        f"[wakefinder/solana copytrade] просадка {status.realized_pnl} lamports превысила лимит — kill switch",
                    )
                    killswitch.engage(settings.kill_switch_file, "drawdown breach: solana copytrade")
                    return

            if token_allowlist and swap.token_out.lower() not in {t.lower() for t in token_allowlist}:
                continue
            if token_denylist and swap.token_out.lower() in {t.lower() for t in token_denylist}:
                continue

            async with positions_lock:
                holds_token_in = swap.token_in in positions

            if holds_token_in:
                await _exit_position(
                    client, jupiter, sender, keypair, tip, positions, positions_lock,
                    settings.solana_copytrade_positions_file, settings.trade_log_file, swap.token_in, "зеркальный выход за китом",
                )
                consensus.clear(swap.token_in)
                continue

            balance = (await client.get_balance(keypair.pubkey())).value

            async with positions_lock:
                already_held = swap.token_out in positions
                current_exposure = sum(p.entry_amount_in for p in positions.values())
            if already_held:
                continue

            reached = consensus.record_buy(swap.token_out, swap.sender)
            if not reached:
                continue
            consensus.clear(swap.token_out)

            amount_in = int(balance * settings.copytrade_size_pct / 100)
            exposure_cap = int(balance * settings.copytrade_max_total_exposure_pct / 100)
            if current_exposure + amount_in > exposure_cap:
                logger.info(
                    "пропуск входа: суммарная экспозиция %d + новый вход %d превысили бы кэп %d",
                    current_exposure, amount_in, exposure_cap,
                )
                continue
            if amount_in <= 0:
                continue
            if not await _has_sufficient_balance(client, keypair.pubkey(), swap.token_in, amount_in):
                continue

            included, amount_out = await _swap_via_jupiter_and_send(
                client, jupiter, sender, keypair, tip, swap.token_in, swap.token_out, amount_in
            )
            logger.info(
                "копитрейд-вход (консенсус): токен=%s триггер-кошелёк=%s amount_in=%d included=%s",
                swap.token_out, swap.sender, amount_in, included,
            )
            trade_log.log_attempt(settings.trade_log_file, "solana", "", amount_in, included, [], strategy="copytrade_entry", wallet=swap.sender)
            if not included:
                continue

            async with positions_lock:
                positions[swap.token_out] = Position(
                    token=swap.token_out, token_in=swap.token_in, amount_held=amount_out,
                    entry_amount_in=amount_in, watched_wallet=swap.sender, opened_at=time.time(),
                )
                _save_positions(settings.solana_copytrade_positions_file, positions)
    finally:
        stop_loss_task.cancel()
        heartbeat_task.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(watched_wallets=frozenset()))
