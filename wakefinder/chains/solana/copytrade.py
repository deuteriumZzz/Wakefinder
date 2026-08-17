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

from wakefinder import live_config
from wakefinder.chains.solana.main import _build_tip_tx, _sign_unsigned_tx, _tip_lamports
from wakefinder.chains.solana.sender import JitoBundleSender, to_base64
from wakefinder.chains.solana.wallet_watcher import SUBSCRIPTION_SYNC_INTERVAL_SECONDS, WalletSwapWatcher
from wakefinder.common import heartbeat, killswitch, pnl_ledger, trade_log, wallet_lock
from wakefinder.common.adaptive_tip import AdaptiveTipController
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.canary import CanaryController
from wakefinder.common.exposure import total_token_exposure_solana
from wakefinder.common.config import get_settings
from wakefinder.common.consensus import ConsensusTracker
from wakefinder.common.drawdown import check_drawdown
from wakefinder.common.interfaces import Bundle
from wakefinder.common.position_reconciliation import find_mismatches
from wakefinder.common.position_sizing import win_rate_size_multiplier
from wakefinder.common.race import race_watchers
from wakefinder.common.reconnect import with_reconnect
from wakefinder.common.stuck_position import StuckPositionTracker
from wakefinder.common.wallet_stats import compute_wallet_stats

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
    stuck: bool = False


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
    input_mint: str, output_mint: str, amount_in: int, slippage_bps: int = SLIPPAGE_BPS,
) -> tuple[bool, int]:
    """Возвращает (included, ориентировочный amount_out — до слиппеджа).
    slippage_bps переопределяем для снайпинга (chains/solana/snipe.py) —
    свежесозданный пул волатильнее обычного копитрейд-входа, дефолт здесь
    остаётся прежним для существующих вызовов."""
    try:
        quote = await jupiter.quote(
            input_mint=input_mint, output_mint=output_mint, amount=amount_in,
            slippage_bps=slippage_bps, only_direct_routes=True,
        )
        expected_out = int(quote["outAmount"])
        unsigned = await jupiter.swap(
            input_mint=input_mint, output_mint=output_mint, amount=amount_in,
            slippage_bps=slippage_bps, only_direct_routes=True,
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
    if included:
        settings = get_settings()
        pnl_ledger.record_closed_trade(
            settings.pnl_ledger_file, "solana", "copytrade", amount_out - pos.entry_amount_in,
            token=pos.token, wallet=pos.watched_wallet, opened_at=pos.opened_at,
        )
    if reason == "стоп-лосс":
        settings = get_settings()
        send_telegram_alert(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, f"[wakefinder/solana copytrade] стоп-лосс: токен={token} included={included}")


async def _mark_stuck(positions, positions_lock, positions_file, token: str, stuck: bool) -> None:
    async with positions_lock:
        pos = positions.get(token)
        if pos is None or pos.stuck == stuck:
            return
        pos.stuck = stuck
        _save_positions(positions_file, positions)
    settings = get_settings()
    if stuck:
        send_telegram_alert(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
            f"[wakefinder/solana copytrade] позиция ЗАВИСЛА (не удаётся оценить цену {settings.stuck_position_threshold}+ раз подряд): "
            f"токен={token} — вероятно rug/высохшая ликвидность, проверьте вручную",
        )
    else:
        logger.info("позиция %s вышла из зависшего состояния — цена снова доступна", token)


async def _unrealized_pnl(jupiter, positions: dict[str, Position]) -> int:
    """Текущая переоценка всех открытых позиций минус то, что за них
    заплачено — для drawdown-проверки (см. common/drawdown.py). Позицию,
    которую не удалось оценить (quote-сбой), пропускаем, а не считаем
    нулевой прибылью/убытком — недооценка просадки безопаснее переоценки."""
    total = 0
    for pos in positions.values():
        try:
            quote = await jupiter.quote(
                input_mint=pos.token, output_mint=pos.token_in, amount=pos.amount_held,
                slippage_bps=SLIPPAGE_BPS, only_direct_routes=True,
            )
            current_value = int(quote["outAmount"])
        except Exception:
            continue
        total += current_value - pos.entry_amount_in
    return total


async def _stop_loss_loop(
    client, jupiter, sender, keypair, tip, positions, positions_lock, positions_file, trade_log_file, stop_loss_pct,
    interval_seconds, stuck_tracker: StuckPositionTracker,
):
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
                if stuck_tracker.record_failure(token):
                    await _mark_stuck(positions, positions_lock, positions_file, token, True)
                continue
            if stuck_tracker.record_success(token):
                await _mark_stuck(positions, positions_lock, positions_file, token, False)
            floor = pos.entry_amount_in * (100 - stop_loss_pct) // 100
            if current_value < floor:
                await _exit_position(client, jupiter, sender, keypair, tip, positions, positions_lock, positions_file, trade_log_file, token, "стоп-лосс")


async def _reconcile_positions_startup(client: AsyncClient, owner: Pubkey, positions: dict[str, Position], settings) -> None:
    """См. eth/copytrade.py:_reconcile_positions_startup — тот же принцип,
    баланс SPL-токена — через ATA (тот же путь, что и _has_sufficient_balance)."""
    if not positions:
        return
    balances: dict[str, int] = {}
    for token in positions:
        ata = get_associated_token_address(owner, Pubkey.from_string(token))
        try:
            resp = await client.get_token_account_balance(ata)
            balances[token] = int(resp.value.amount)
        except Exception as exc:
            logger.warning("не удалось проверить баланс токена %s при старте (%s)", token, type(exc).__name__)
    recorded = {token: pos.amount_held for token, pos in positions.items()}
    mismatches = find_mismatches(recorded, balances)
    if not mismatches:
        return
    lines = "; ".join(f"{m.token}: записано {m.recorded_amount}, на кошельке {m.actual_balance}" for m in mismatches)
    logger.warning("расхождение positions.json с on-chain балансом при старте: %s", lines)
    send_telegram_alert(
        settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
        f"[wakefinder/solana copytrade] расхождение positions.json с реальным балансом при старте — проверьте вручную: {lines}",
    )


async def run(
    watched_wallets: frozenset[str],
    token_allowlist: frozenset[str] = frozenset(),
    token_denylist: frozenset[str] = frozenset(),
):
    settings = get_settings()
    if not (settings.solana_rpc_ws_url and settings.solana_rpc_http_url and (settings.solana_private_key or settings.solana_private_key_file)):
        raise RuntimeError("SOLANA_RPC_WS_URL / SOLANA_RPC_HTTP_URL / SOLANA_PRIVATE_KEY(_FILE) не настроены")

    solana_private_key = settings.resolved_solana_private_key()
    assert solana_private_key is not None  # гарантировано проверкой выше — здесь только для mypy
    keypair = Keypair.from_base58_string(solana_private_key)
    _wallet_lock_handle = wallet_lock.acquire_wallet_lock(settings.heartbeat_dir, str(keypair.pubkey()), "solana_copytrade")
    client = AsyncClient(settings.solana_rpc_http_url.get_secret_value())
    jupiter = Jupiter(client, keypair)
    sender = JitoBundleSender(settings.jito_block_engine_url, keypair)
    tip = AdaptiveTipController(initial_bps=settings.profit_share_bps)

    # Мутируемые копии — живой конфиг (wakefinder/live_config.py) обновляет
    # ИХ ЖЕ объекты in place. WalletSwapWatcher.watch() сам периодически
    # сверяет self.watched_wallets и до/отписывается без реконнекта (см.
    # SUBSCRIPTION_SYNC_INTERVAL_SECONDS в wallet_watcher.py).
    watched_wallets = {a.lower() for a in watched_wallets}
    token_allowlist = {a.lower() for a in token_allowlist}
    token_denylist = {a.lower() for a in token_denylist}
    live_config.seed_if_missing(settings.live_config_file, watched_wallets, token_allowlist, token_denylist)

    positions = _load_positions(settings.solana_copytrade_positions_file)
    await _reconcile_positions_startup(client, keypair.pubkey(), positions, settings)
    positions_lock = asyncio.Lock()
    stuck_tracker = StuckPositionTracker(settings.stuck_position_threshold)
    consensus = ConsensusTracker(settings.copytrade_min_consensus_wallets, settings.copytrade_consensus_window_seconds)
    canary = CanaryController(settings, settings.canary_start_fraction, settings.canary_ramp_trades)
    last_drawdown_check = 0.0
    last_live_config_check = 0.0

    ws_urls = [settings.solana_rpc_ws_url.get_secret_value()]
    ws_urls += [u.strip() for u in settings.solana_rpc_ws_urls.split(",") if u.strip()]
    watchers = [WalletSwapWatcher(url, client, watched_wallets) for url in ws_urls]

    stop_loss_task = asyncio.create_task(
        _stop_loss_loop(
            client, jupiter, sender, keypair, tip, positions, positions_lock,
            settings.solana_copytrade_positions_file, settings.trade_log_file, settings.copytrade_stop_loss_pct,
            settings.copytrade_stop_loss_check_interval_seconds, stuck_tracker,
        )
    )
    heartbeat_path = os.path.join(settings.heartbeat_dir, "solana_copytrade.heartbeat")
    heartbeat_task = asyncio.create_task(heartbeat.loop(heartbeat_path, settings.heartbeat_interval_seconds))

    watch_streams = [(lambda w=watcher: with_reconnect(w.watch)) for watcher in watchers]
    try:
        async for swap in race_watchers(watch_streams):
            if killswitch.is_engaged(settings.kill_switch_file):
                logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                send_telegram_alert(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, "[wakefinder/solana copytrade] kill switch присутствует — бот остановлен")
                return

            now = time.time()
            if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                last_drawdown_check = now
                fraction = canary.update(settings.trade_log_file, "solana")
                if fraction < 1.0:
                    logger.info("canary: текущий размер позиции = %.0f%% от полного (разгон по мере накопления сделок)", fraction * 100)
                async with positions_lock:
                    positions_snapshot = dict(positions)
                unrealized = await _unrealized_pnl(jupiter, positions_snapshot)
                status = check_drawdown(
                    settings.trade_log_file, "solana", settings.drawdown_window_seconds,
                    int(settings.max_drawdown_sol * 10**9), unrealized_pnl=unrealized,
                )
                if status.breached:
                    logger.critical(
                        "просадка за окно realized=%d unrealized=%d lamports превысила лимит — включаю kill switch",
                        status.realized_pnl, status.unrealized_pnl,
                    )
                    send_telegram_alert(
                        settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
                        f"[wakefinder/solana copytrade] просадка realized={status.realized_pnl} "
                        f"unrealized={status.unrealized_pnl} lamports превысила лимит — kill switch",
                    )
                    killswitch.engage(settings.kill_switch_file, "drawdown breach: solana copytrade")
                    return

            if now - last_live_config_check >= settings.live_config_check_interval_seconds:
                last_live_config_check = now
                live = live_config.load_live_config(settings.live_config_file)
                if live_config.sync_set(watched_wallets, live["watched_wallets"]):
                    for w in watchers:
                        live_config.sync_set(w.watched_wallets, live["watched_wallets"])
                    logger.info("live-конфиг: watched_wallets обновлены (%d, подписки на изменение обновятся в течение %d с без реконнекта)", len(watched_wallets), SUBSCRIPTION_SYNC_INTERVAL_SECONDS)
                if live_config.sync_set(token_allowlist, live["token_allowlist"]):
                    logger.info("live-конфиг: token_allowlist обновлён (%d)", len(token_allowlist))
                if live_config.sync_set(token_denylist, live["token_denylist"]):
                    logger.info("live-конфиг: token_denylist обновлён (%d)", len(token_denylist))
                applied = live_config.apply_risk_overrides_live(settings, live["risk"])
                if applied:
                    logger.info("live-конфиг: risk-параметры обновлены: %s", applied)

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

            wallet_stats = compute_wallet_stats(settings.trade_log_file).get(swap.sender.lower())
            multiplier = (
                win_rate_size_multiplier(
                    wallet_stats.win_rate, wallet_stats.exits,
                    settings.copytrade_sizing_min_trades, settings.copytrade_sizing_min_multiplier, settings.copytrade_sizing_max_multiplier,
                )
                if wallet_stats else 1.0
            )
            if wallet_stats and multiplier != 1.0:  # wallet_stats всегда truthy здесь (иначе multiplier == 1.0) — доп. проверка только для mypy
                logger.info("win-rate множитель размера для %s: %.2fx (win_rate=%.0f%%, сделок=%d)", swap.sender, multiplier, wallet_stats.win_rate * 100, wallet_stats.exits)

            amount_in = int(balance * settings.copytrade_size_pct * multiplier / 100)
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

            if settings.max_token_exposure_sol is not None:
                cross_strategy_exposure = total_token_exposure_solana(swap.token_out, settings)
                cap_lamports = int(settings.max_token_exposure_sol * 10**9)
                if cross_strategy_exposure + amount_in > cap_lamports:
                    logger.info(
                        "пропуск входа: суммарная экспозиция по токену %s через ВСЕ стратегии %d + новый вход %d превысили бы кэп %d",
                        swap.token_out, cross_strategy_exposure, amount_in, cap_lamports,
                    )
                    continue

            latency_ms = (time.time() - swap.detected_at) * 1000
            included, amount_out = await _swap_via_jupiter_and_send(
                client, jupiter, sender, keypair, tip, swap.token_in, swap.token_out, amount_in
            )
            logger.info(
                "копитрейд-вход (консенсус): токен=%s триггер-кошелёк=%s amount_in=%d included=%s",
                swap.token_out, swap.sender, amount_in, included,
            )
            trade_log.log_attempt(settings.trade_log_file, "solana", "", amount_in, included, [], strategy="copytrade_entry", wallet=swap.sender, latency_ms=latency_ms)
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
