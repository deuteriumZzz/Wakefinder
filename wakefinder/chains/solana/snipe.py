"""Снайпинг новых SPL-токенов на Solana — третья, отдельная стратегия от
арбитража (main.py) и копитрейдинга (copytrade.py): нет ни ценового
перекоса, ни кита, за которым следить. Реагирует на факт создания НОВОГО
МИНТА (см. подробный docstring mint_watcher.py про то, почему детекция идёт
именно так, а не через parsing конкретного AMM), подтверждает торгуемость
через Jupiter (snipe_filter.py), входит по моментуму, выходит по
trailing-stop (common/trailing_stop.py, та же логика, что и в ETH-варианте).

РИСК тот же, что у ETH-снайпинга (chains/eth/snipe.py, см. её docstring) —
подавляющее большинство новых минтов никогда не получают ликвидный пул,
из тех, что получают, многие rug/dead в первые минуты. Держите
SNIPE_SIZE_PCT маленьким, используйте canary, отдельный кошелёк от
main.py/copytrade.py (то же ограничение по nonce/параллельным транзакциям,
что и везде на Solana в этом проекте).

Вход и выход — через Jupiter + Jito bundle с tip (тот же путь, что
copytrade.py:_swap_via_jupiter_and_send, переиспользуется напрямую, не
дублируется) — не собственное кодирование инструкций DEX."""

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
from wakefinder.chains.solana.copytrade import _swap_via_jupiter_and_send
from wakefinder.chains.solana.mint_watcher import NewMintWatcher
from wakefinder.chains.solana.sender import JitoBundleSender
from wakefinder.chains.solana.snipe_filter import check_mint_tradeable
from wakefinder.common import heartbeat, killswitch, pnl_ledger, trade_log, wallet_lock
from wakefinder.common.adaptive_tip import AdaptiveTipController
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.canary import CanaryController
from wakefinder.common.exposure import total_token_exposure_solana
from wakefinder.common.config import get_settings
from wakefinder.common.drawdown import check_drawdown
from wakefinder.common.race import race_watchers
from wakefinder.common.position_reconciliation import find_mismatches
from wakefinder.common.reconnect import with_reconnect
from wakefinder.common.stuck_position import StuckPositionTracker
from wakefinder.common.trailing_stop import TrailingStopTracker

SLIPPAGE_BPS = 300  # шире дефолта copytrade (100) — свежесозданный пул волатильнее, см. docstring модуля

logger = logging.getLogger("wakefinder.solana.snipe")


@dataclass
class SnipePosition:
    mint: str
    amount_held: int
    entry_amount_in: int  # сколько lamports SOL потрачено на вход
    opened_at: float
    stuck: bool = False


def _load_positions(path: str) -> dict[str, SnipePosition]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {k: SnipePosition(**v) for k, v in raw.items()}


def _save_positions(path: str, positions: dict[str, SnipePosition]) -> None:
    with open(path, "w") as f:
        json.dump({k: asdict(v) for k, v in positions.items()}, f, indent=2)


async def _current_value(jupiter: Jupiter, wsol_address: str, mint: str, amount_held: int) -> int | None:
    try:
        quote = await jupiter.quote(
            input_mint=mint, output_mint=wsol_address, amount=amount_held, slippage_bps=SLIPPAGE_BPS, only_direct_routes=True,
        )
        return int(quote["outAmount"])
    except Exception:
        return None  # ликвидность высохла/rug — не можем оценить, не считаем нулём (см. drawdown-заметки в copytrade.py)


async def _exit_position(
    client, jupiter, sender, keypair, tip, positions, positions_lock, positions_file, trade_log_file, wsol_address: str,
    mint: str, reason: str,
) -> None:
    async with positions_lock:
        pos = positions.pop(mint, None)
        if pos is not None:
            _save_positions(positions_file, positions)
    if pos is None:
        return
    included, amount_out = await _swap_via_jupiter_and_send(
        client, jupiter, sender, keypair, tip, mint, wsol_address, pos.amount_held, slippage_bps=SLIPPAGE_BPS,
    )
    logger.info("снайп-выход (%s): mint=%s included=%s", reason, mint, included)
    trade_log.log_attempt(trade_log_file, "solana", "", amount_out, included, [], strategy="snipe_exit")
    if included:
        settings = get_settings()
        pnl_ledger.record_closed_trade(
            settings.pnl_ledger_file, "solana", "snipe", amount_out - pos.entry_amount_in,
            token=mint, opened_at=pos.opened_at,
        )
    if not included:
        settings = get_settings()
        send_telegram_alert(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
            f"[wakefinder/solana snipe] выход ({reason}) не попал в блок: mint={mint} — позиция потеряна из вида, проверьте вручную",
        )


async def _mark_stuck(positions, positions_lock, positions_file, mint: str, stuck: bool) -> None:
    async with positions_lock:
        pos = positions.get(mint)
        if pos is None or pos.stuck == stuck:
            return
        pos.stuck = stuck
        _save_positions(positions_file, positions)
    settings = get_settings()
    if stuck:
        send_telegram_alert(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
            f"[wakefinder/solana snipe] позиция ЗАВИСЛА (не удаётся оценить цену {settings.stuck_position_threshold}+ раз подряд): "
            f"mint={mint} — вероятно rug/высохшая ликвидность, проверьте вручную",
        )
    else:
        logger.info("позиция %s вышла из зависшего состояния — цена снова доступна", mint)


async def _trailing_stop_loop(
    client, jupiter, sender, keypair, tip, positions, positions_lock, positions_file, trade_log_file, wsol_address: str,
    trackers: dict[str, TrailingStopTracker], interval_seconds: float, stuck_tracker: StuckPositionTracker,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        async with positions_lock:
            snapshot = dict(positions)
        for mint, pos in snapshot.items():
            current = await _current_value(jupiter, wsol_address, mint, pos.amount_held)
            if current is None:
                if stuck_tracker.record_failure(mint):
                    await _mark_stuck(positions, positions_lock, positions_file, mint, True)
                continue
            if stuck_tracker.record_success(mint):
                await _mark_stuck(positions, positions_lock, positions_file, mint, False)
            tracker = trackers.setdefault(mint, TrailingStopTracker(trail_pct=get_settings().snipe_trailing_stop_pct, momentum_reversal_pct=get_settings().snipe_momentum_reversal_pct))
            if tracker.update(current):
                await _exit_position(client, jupiter, sender, keypair, tip, positions, positions_lock, positions_file, trade_log_file, wsol_address, mint, "trailing-stop")
                trackers.pop(mint, None)


async def _reconcile_positions_startup(client: AsyncClient, owner: Pubkey, positions: dict[str, SnipePosition], settings) -> None:
    """См. solana/copytrade.py:_reconcile_positions_startup — тот же принцип."""
    if not positions:
        return
    balances: dict[str, int] = {}
    for mint in positions:
        ata = get_associated_token_address(owner, Pubkey.from_string(mint))
        try:
            resp = await client.get_token_account_balance(ata)
            balances[mint] = int(resp.value.amount)
        except Exception as exc:
            logger.warning("не удалось проверить баланс минта %s при старте (%s)", mint, type(exc).__name__)
    recorded = {mint: pos.amount_held for mint, pos in positions.items()}
    mismatches = find_mismatches(recorded, balances)
    if not mismatches:
        return
    lines = "; ".join(f"{m.token}: записано {m.recorded_amount}, на кошельке {m.actual_balance}" for m in mismatches)
    logger.warning("расхождение positions.json с on-chain балансом при старте: %s", lines)
    send_telegram_alert(
        settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
        f"[wakefinder/solana snipe] расхождение positions.json с реальным балансом при старте — проверьте вручную: {lines}",
    )


async def run(token_denylist: frozenset[str] = frozenset()):
    settings = get_settings()
    if not (settings.solana_rpc_ws_url and settings.solana_rpc_http_url and (settings.solana_private_key or settings.solana_private_key_file)):
        raise RuntimeError("SOLANA_RPC_WS_URL / SOLANA_RPC_HTTP_URL / SOLANA_PRIVATE_KEY(_FILE) не настроены")

    token_denylist = {a.lower() for a in token_denylist}
    live_config.seed_if_missing(settings.live_config_file, set(), set(), token_denylist)

    solana_private_key = settings.resolved_solana_private_key()
    assert solana_private_key is not None  # гарантировано проверкой выше — здесь только для mypy
    keypair = Keypair.from_base58_string(solana_private_key)
    _wallet_lock_handle = wallet_lock.acquire_wallet_lock(settings.heartbeat_dir, str(keypair.pubkey()), "solana_snipe")
    client = AsyncClient(settings.solana_rpc_http_url.get_secret_value())
    jupiter = Jupiter(client, keypair)
    sender = JitoBundleSender(settings.jito_block_engine_url, keypair, dry_run=settings.dry_run)
    tip = AdaptiveTipController(initial_bps=settings.profit_share_bps)

    positions = _load_positions(settings.solana_snipe_positions_file)
    await _reconcile_positions_startup(client, keypair.pubkey(), positions, settings)
    positions_lock = asyncio.Lock()
    trackers: dict[str, TrailingStopTracker] = {}
    stuck_tracker = StuckPositionTracker(settings.stuck_position_threshold)
    canary = CanaryController(settings, settings.canary_start_fraction, settings.canary_ramp_trades)
    last_drawdown_check = 0.0
    last_live_config_check = 0.0
    test_amount_lamports = int(settings.solana_snipe_test_amount_sol * 10**9)
    min_liquidity_lamports = int(settings.solana_snipe_min_liquidity_sol * 10**9)

    ws_urls = [settings.solana_rpc_ws_url.get_secret_value()]
    ws_urls += [u.strip() for u in settings.solana_rpc_ws_urls.split(",") if u.strip()]
    watchers = [NewMintWatcher(url, client) for url in ws_urls]

    trailing_task = asyncio.create_task(
        _trailing_stop_loop(
            client, jupiter, sender, keypair, tip, positions, positions_lock,
            settings.solana_snipe_positions_file, settings.trade_log_file, settings.solana_wsol_address,
            trackers, settings.snipe_trailing_stop_check_interval_seconds, stuck_tracker,
        )
    )
    heartbeat_path = os.path.join(settings.heartbeat_dir, "solana_snipe.heartbeat")
    heartbeat_task = asyncio.create_task(heartbeat.loop(heartbeat_path, settings.heartbeat_interval_seconds))

    watch_streams = [(lambda w=watcher: with_reconnect(w.watch)) for watcher in watchers]
    try:
        async for new_mint in race_watchers(watch_streams):
            if killswitch.is_engaged(settings.kill_switch_file):
                logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                send_telegram_alert(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, "[wakefinder/solana snipe] kill switch присутствует — бот остановлен")
                return

            now = time.time()
            if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                last_drawdown_check = now
                fraction = canary.update(settings.trade_log_file, "solana")
                if fraction < 1.0:
                    logger.info("canary: текущий размер позиции = %.0f%% от полного", fraction * 100)
                status = check_drawdown(settings.trade_log_file, "solana", settings.drawdown_window_seconds, int(settings.max_drawdown_sol * 10**9))
                if status.breached:
                    logger.critical("просадка за окно %d lamports превысила лимит — включаю kill switch", status.realized_pnl)
                    send_telegram_alert(
                        settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
                        f"[wakefinder/solana snipe] просадка {status.realized_pnl} lamports превысила лимит — kill switch",
                    )
                    killswitch.engage(settings.kill_switch_file, "drawdown breach: solana snipe")
                    return

            if now - last_live_config_check >= settings.live_config_check_interval_seconds:
                last_live_config_check = now
                live = live_config.load_live_config(settings.live_config_file)
                if live_config.sync_set(token_denylist, live["token_denylist"]):
                    logger.info("live-конфиг: token_denylist обновлён (%d)", len(token_denylist))
                applied = live_config.apply_risk_overrides_live(settings, live["risk"])
                if applied:
                    logger.info("live-конфиг: risk-параметры обновлены: %s", applied)
                    test_amount_lamports = int(settings.solana_snipe_test_amount_sol * 10**9)
                    min_liquidity_lamports = int(settings.solana_snipe_min_liquidity_sol * 10**9)

            if token_denylist and new_mint.mint_address.lower() in token_denylist:
                continue

            async with positions_lock:
                at_capacity = len(positions) >= settings.snipe_max_concurrent_positions
                already_held = new_mint.mint_address in positions
            if at_capacity or already_held:
                continue

            result = await check_mint_tradeable(jupiter, new_mint.mint_address, settings.solana_wsol_address, test_amount_lamports, min_liquidity_lamports)
            if not result.passed:
                logger.info("снайп-фильтр отклонил mint=%s: %s", new_mint.mint_address, result.reason)
                continue

            balance = (await client.get_balance(keypair.pubkey())).value
            amount_in = int(balance * settings.snipe_size_pct / 100)
            if amount_in <= 0:
                continue

            if settings.max_token_exposure_sol is not None:
                cross_strategy_exposure = total_token_exposure_solana(new_mint.mint_address, settings)
                cap_lamports = int(settings.max_token_exposure_sol * 10**9)
                if cross_strategy_exposure + amount_in > cap_lamports:
                    logger.info(
                        "пропуск входа: суммарная экспозиция по mint=%s через ВСЕ стратегии %d + новый вход %d превысили бы кэп %d",
                        new_mint.mint_address, cross_strategy_exposure, amount_in, cap_lamports,
                    )
                    continue

            latency_ms = (time.time() - new_mint.detected_at) * 1000
            included, bought_amount = await _swap_via_jupiter_and_send(
                client, jupiter, sender, keypair, tip, settings.solana_wsol_address, new_mint.mint_address, amount_in, slippage_bps=SLIPPAGE_BPS,
            )
            logger.info("снайп-вход: mint=%s amount_in=%d included=%s", new_mint.mint_address, amount_in, included)
            trade_log.log_attempt(settings.trade_log_file, "solana", "", amount_in, included, [new_mint.tx_hash], strategy="snipe_entry", latency_ms=latency_ms)
            if not included:
                continue

            async with positions_lock:
                positions[new_mint.mint_address] = SnipePosition(
                    mint=new_mint.mint_address, amount_held=bought_amount, entry_amount_in=amount_in, opened_at=time.time(),
                )
                trackers[new_mint.mint_address] = TrailingStopTracker(trail_pct=settings.snipe_trailing_stop_pct, momentum_reversal_pct=settings.snipe_momentum_reversal_pct)
                _save_positions(settings.solana_snipe_positions_file, positions)
    finally:
        trailing_task.cancel()
        heartbeat_task.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
