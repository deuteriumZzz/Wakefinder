"""Копитрейдинг: зеркалим ВХОД и ВЫХОД watchlist-кошельков, а не арбитражим
ценовой перекос от их сделки (сама арбитражная стратегия — в simulator.py).
Принципиально другой профиль риска: здесь бот реально держит позицию с
открытым направленным риском между входом и выходом — не атомарная сделка с
гарантированной на момент исполнения прибылью, как арбитраж.

Размер входа — ДОЛЯ ОТ НАШЕГО баланса (COPYTRADE_SIZE_PCT), не сумма кита:
у кита может быть бюджет в миллионы, у нас — сколько есть; повторяем
направление ставки, не её абсолютный размер.

Вход требует КОНСЕНСУСА: >= COPYTRADE_MIN_CONSENSUS_WALLETS разных watched-
кошельков должны купить один и тот же токен в течение
COPYTRADE_CONSENSUS_WINDOW_SECONDS — один кит может ошибаться, несколько
независимых китов почти одновременно — сильнее сигнал.

Осознанное отличие от арбитража: наши собственные сделки (вход и выход) идут
в ПУБЛИЧНЫЙ мемпул (`send_raw_transaction`), не через Flashbots. Watcher уже
реагирует на PENDING-транзакцию кита (не дожидаясь подтверждения), и здесь
нужна максимальная скорость входа, пока рынок ещё не отреагировал — relay-хоп
Flashbots этому не помогает. Плата за это: наша транзакция видна в публичном
мемпуле и теоретически может быть засэндвичена другими MEV-ботами — та же
атака, которую применяет арбитражная часть этого проекта против чужих сделок.

Выход по двум независимым триггерам:
- зеркальный: любой watched-кошелёк продаёт токен, который мы держим -> продаём
- стоп-лосс (фоновая задача): цена позиции упала на COPYTRADE_STOP_LOSS_PCT от
  цены входа -> продаём независимо от китов (защита на случай, если все они
  держат "до нуля" или мы упустили их выход)

ponytail: позиции хранятся в плоском JSON-файле, не в БД — при таком масштабе
(один бот, одна пара кошелёк/сеть) этого достаточно, и рестарт не теряет
открытые позиции молча.

Требует ОТДЕЛЬНОГО процесса от wakefinder.chains.eth.main (арбитраж) или
запуска не одновременно с ним при общем ETH_PRIVATE_KEY — иначе оба процесса
независимо считают nonce и конфликтуют друг с другом.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass

from eth_account import Account
from web3 import AsyncWeb3, Web3, WebsocketProviderV2

from wakefinder import live_config
from wakefinder.chains.eth.abi import ERC20_ABI, PAIR_ABI, ROUTER_ABI
from wakefinder.chains.eth.watcher import UniswapV2Watcher
from wakefinder.common import heartbeat, killswitch, pnl_ledger, trade_log, wallet_lock
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.amm import get_amount_out
from wakefinder.common.exposure import total_token_exposure_eth
from wakefinder.common.canary import CanaryController
from wakefinder.common.config import get_settings
from wakefinder.common.consensus import ConsensusTracker
from wakefinder.common.cowswap import ensure_vault_relayer_approved, place_and_wait_for_exit_order
from wakefinder.common.drawdown import check_drawdown
from wakefinder.common.position_reconciliation import find_mismatches
from wakefinder.common.position_sizing import win_rate_size_multiplier
from wakefinder.common.protected_rpc import send_raw_via_protected_rpc
from wakefinder.common.race import race_watchers
from wakefinder.common.reconnect import with_reconnect
from wakefinder.common.sandwich_detector import check_own_tx_for_sandwich
from wakefinder.common.stuck_position import StuckPositionTracker
from wakefinder.common.wallet_stats import compute_wallet_stats

SLIPPAGE_BPS = 100
GAS_LIMIT = 200_000
RECEIPT_TIMEOUT_SECONDS = 60
RECEIPT_POLL_SECONDS = 2
_ENCODER = Web3()

logger = logging.getLogger("wakefinder.eth.copytrade")


@dataclass
class Position:
    token: str
    token_in: str
    pool_address: str
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


def _sign_swap(router_address, account, chain_id, nonce, max_fee, priority_fee, path, amount_in, amount_out_min) -> bytes:
    router = _ENCODER.eth.contract(address=router_address, abi=ROUTER_ABI)
    tx = router.functions.swapExactTokensForTokens(
        amountIn=amount_in, amountOutMin=amount_out_min, path=path, to=account.address, deadline=int(time.time()) + 60,
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "chainId": chain_id,
        }
    )
    return account.sign_transaction(tx).rawTransaction


async def _reserves(w3: AsyncWeb3, pool_address: str, token_in: str) -> tuple[int, int]:
    pool = w3.eth.contract(address=pool_address, abi=PAIR_ABI)
    r0, r1, _ = await pool.functions.getReserves().call()
    token0 = await pool.functions.token0().call()
    if token0.lower() == token_in.lower():
        return r0, r1
    return r1, r0


async def _unrealized_pnl(w3: AsyncWeb3, positions: dict[str, "Position"]) -> int:
    """Текущая переоценка всех открытых позиций минус то, что за них
    заплачено — для drawdown-проверки (см. common/drawdown.py). Позицию,
    которую не удалось оценить (RPC-сбой), пропускаем, а не считаем нулевой
    прибылью/убытком — недооценка просадки безопаснее переоценки."""
    total = 0
    for pos in positions.values():
        try:
            reserve_in, reserve_out = await _reserves(w3, pos.pool_address, pos.token)
            current_value = get_amount_out(pos.amount_held, reserve_in, reserve_out)
        except Exception:
            continue
        total += current_value - pos.entry_amount_in
    return total


async def _wait_for_receipt(w3: AsyncWeb3, tx_hash, timeout_seconds: float = RECEIPT_TIMEOUT_SECONDS) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            receipt = await w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            receipt = None
        if receipt is not None:
            return receipt.get("status") == 1
        await asyncio.sleep(RECEIPT_POLL_SECONDS)
    return False  # не дождались — считаем неудачей, а не зависаем навсегда


async def _send_single_swap(
    w3: AsyncWeb3, account, router_address: str, chain_id: int,
    path: list[str], amount_in: int, amount_out_min: int,
) -> tuple[bool, str]:
    """Отправляет в публичный мемпул напрямую (не через Flashbots — см. docstring
    модуля про компромисс скорости против защиты от сэндвича). Возвращает
    (успех, tx_hash)."""
    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    raw = _sign_swap(router_address, account, chain_id, nonce, max_fee, priority_fee, path, amount_in, amount_out_min)
    settings = get_settings()
    if settings.dry_run:
        logger.info("[DRY RUN] транзакция подписана, реальная отправка в мемпул пропущена")
        return True, "0x" + "0" * 64
    if settings.eth_mev_protect_rpc_url:
        tx_hash = await send_raw_via_protected_rpc(settings.eth_mev_protect_rpc_url, raw)
    else:
        tx_hash = await w3.eth.send_raw_transaction(raw)
    ok = await _wait_for_receipt(w3, tx_hash)
    return ok, tx_hash.hex()


async def _try_enter(
    w3, account, router_address, chain_id, token_in, token_out, pool_address, watched_wallet,
    size_pct, max_total_exposure_pct, positions, positions_lock, positions_file, trade_log_file,
    detected_at: float,
):
    balance = await w3.eth.get_balance(account.address)

    async with positions_lock:
        if token_out.lower() in positions:
            return  # уже держим этот токен, не наращиваем позицию повторно
        current_exposure = sum(p.entry_amount_in for p in positions.values())

    settings = get_settings()
    wallet_stats = compute_wallet_stats(trade_log_file).get(watched_wallet.lower())
    multiplier = (
        win_rate_size_multiplier(
            wallet_stats.win_rate, wallet_stats.exits,
            settings.copytrade_sizing_min_trades, settings.copytrade_sizing_min_multiplier, settings.copytrade_sizing_max_multiplier,
        )
        if wallet_stats else 1.0
    )
    if wallet_stats and multiplier != 1.0:  # wallet_stats всегда truthy здесь (иначе multiplier == 1.0) — доп. проверка только для mypy
        logger.info("win-rate множитель размера для %s: %.2fx (win_rate=%.0f%%, сделок=%d)", watched_wallet, multiplier, wallet_stats.win_rate * 100, wallet_stats.exits)

    # ponytail: тот же приём, что и в арбитраже — предполагаем token_in 18-decimal (WETH)
    amount_in = int(balance * size_pct * multiplier / 100)
    exposure_cap = int(balance * max_total_exposure_pct / 100)
    if current_exposure + amount_in > exposure_cap:
        logger.info(
            "пропуск входа: суммарная экспозиция %d + новый вход %d превысили бы кэп %d",
            current_exposure, amount_in, exposure_cap,
        )
        return
    if amount_in <= 0:
        return

    if settings.max_token_exposure_eth is not None:
        cross_strategy_exposure = total_token_exposure_eth(token_out, settings)
        cap_wei = int(settings.max_token_exposure_eth * 10**18)
        if cross_strategy_exposure + amount_in > cap_wei:
            logger.info(
                "пропуск входа: суммарная экспозиция по токену %s через ВСЕ стратегии %d + новый вход %d превысили бы кэп %d",
                token_out, cross_strategy_exposure, amount_in, cap_wei,
            )
            return

    reserve_in, reserve_out = await _reserves(w3, pool_address, token_in)
    expected_out = get_amount_out(amount_in, reserve_in, reserve_out)
    if expected_out <= 0:
        return
    amount_out_min = expected_out * (10_000 - SLIPPAGE_BPS) // 10_000

    latency_ms = (time.time() - detected_at) * 1000
    included, tx_hash = await _send_single_swap(w3, account, router_address, chain_id, [token_in, token_out], amount_in, amount_out_min)
    logger.info("копитрейд-вход (консенсус): токен=%s триггер-кошелёк=%s amount_in=%d included=%s", token_out, watched_wallet, amount_in, included)
    trade_log.log_attempt(trade_log_file, "eth", pool_address, amount_in, included, [tx_hash], strategy="copytrade_entry", wallet=watched_wallet, latency_ms=latency_ms)
    if not included:
        return

    if not settings.dry_run:
        asyncio.create_task(_check_sandwich(w3, tx_hash, token_out))

    async with positions_lock:
        positions[token_out.lower()] = Position(
            token=token_out, token_in=token_in, pool_address=pool_address,
            amount_held=expected_out, entry_amount_in=amount_in,
            watched_wallet=watched_wallet, opened_at=time.time(),
        )
        _save_positions(positions_file, positions)


async def _exit_position(w3, account, router_address, chain_id, token: str, reason: str, positions, positions_lock, positions_file, trade_log_file):
    async with positions_lock:
        pos = positions.get(token.lower())
        if pos is None:
            return
        del positions[token.lower()]
        _save_positions(positions_file, positions)

    reserve_in, reserve_out = await _reserves(w3, pos.pool_address, pos.token)  # pos.token — то, что продаём
    expected_out = get_amount_out(pos.amount_held, reserve_in, reserve_out)
    amount_out_min = expected_out * (10_000 - SLIPPAGE_BPS) // 10_000

    settings = get_settings()
    included = False
    tx_hash = ""
    actual_out = expected_out
    if settings.exit_via_cowswap:
        # Выход — не вход: скорость решает НАША проверка цены по таймеру, а
        # не гонка с чужой pending-транзакцией, поэтому торможение ради
        # лучшей цены/MEV-защиты через CoW Protocol уместно (см. docstring
        # common/cowswap.py). При любом сбое — откат на прямой AMM-своп ниже.
        try:
            await ensure_vault_relayer_approved(w3, account, chain_id, pos.token, pos.amount_held)
            filled, cow_buy_amount = await place_and_wait_for_exit_order(
                account, chain_id, pos.token, pos.token_in, pos.amount_held, amount_out_min,
                settings.cowswap_order_valid_seconds, settings.cowswap_order_poll_timeout_seconds,
            )
        except Exception as exc:
            logger.warning("CoWSwap выход не удался (%s) — откат на прямой AMM-своп", type(exc).__name__)
            filled, cow_buy_amount = False, 0
        if filled:
            included, actual_out = True, cow_buy_amount
            logger.info("копитрейд-выход (%s) через CoWSwap: токен=%s получено=%d", reason, token, cow_buy_amount)

    if not included:
        included, tx_hash = await _send_single_swap(
            w3, account, router_address, chain_id, [pos.token, pos.token_in], pos.amount_held, amount_out_min
        )
        actual_out = expected_out

    logger.info("копитрейд-выход (%s): токен=%s included=%s", reason, token, included)
    trade_log.log_attempt(trade_log_file, "eth", pos.pool_address, actual_out, included, [tx_hash] if tx_hash else [], strategy="copytrade_exit", wallet=pos.watched_wallet)
    if included:
        pnl_ledger.record_closed_trade(
            settings.pnl_ledger_file, "eth", "copytrade", actual_out - pos.entry_amount_in,
            token=pos.token, wallet=pos.watched_wallet, opened_at=pos.opened_at,
        )
    if reason == "стоп-лосс":
        send_telegram_alert(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, f"[wakefinder/eth copytrade] стоп-лосс: токен={token} included={included}")


async def _check_sandwich(w3: AsyncWeb3, tx_hash: str, token: str) -> None:
    """Fire-and-forget постфактум-проверка — не блокирует основной цикл
    (см. docstring common/sandwich_detector.py про честную границу этой
    детекции)."""
    result = await check_own_tx_for_sandwich(w3, tx_hash)
    if not result.likely_sandwiched:
        return
    logger.warning("вход по токену %s похож на sandwich (front-run/back-run с %s): tx=%s", token, result.front_run_from, tx_hash)
    settings = get_settings()
    send_telegram_alert(
        settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
        f"[wakefinder/eth copytrade] вход по токену {token} похож на sandwich-атаку (front-run/back-run с одного адреса {result.front_run_from}): tx={tx_hash}",
    )


async def _mark_stuck(positions, positions_lock, positions_file, token: str, stuck: bool) -> None:
    async with positions_lock:
        pos = positions.get(token.lower())
        if pos is None or pos.stuck == stuck:
            return
        pos.stuck = stuck
        _save_positions(positions_file, positions)
    settings = get_settings()
    if stuck:
        send_telegram_alert(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
            f"[wakefinder/eth copytrade] позиция ЗАВИСЛА (не удаётся оценить цену {settings.stuck_position_threshold}+ раз подряд): "
            f"токен={token} — вероятно rug/высохшая ликвидность, проверьте вручную",
        )
    else:
        logger.info("позиция %s вышла из зависшего состояния — цена снова доступна", token)


async def _stop_loss_loop(
    w3, account, router_address, chain_id, positions, positions_lock, positions_file, trade_log_file, stop_loss_pct,
    interval_seconds, stuck_tracker: StuckPositionTracker,
):
    while True:
        await asyncio.sleep(interval_seconds)
        async with positions_lock:
            snapshot = dict(positions)
        for token, pos in snapshot.items():
            try:
                reserve_in, reserve_out = await _reserves(w3, pos.pool_address, pos.token)
                current_value = get_amount_out(pos.amount_held, reserve_in, reserve_out)
            except Exception as exc:
                logger.warning("не удалось проверить цену позиции %s (%s)", token, type(exc).__name__)
                if stuck_tracker.record_failure(token):
                    await _mark_stuck(positions, positions_lock, positions_file, token, True)
                continue
            if stuck_tracker.record_success(token):
                await _mark_stuck(positions, positions_lock, positions_file, token, False)
            floor = pos.entry_amount_in * (100 - stop_loss_pct) // 100
            if current_value < floor:
                await _exit_position(w3, account, router_address, chain_id, token, "стоп-лосс", positions, positions_lock, positions_file, trade_log_file)


async def _reconcile_positions_startup(w3: AsyncWeb3, account_address: str, positions: dict[str, Position], settings) -> None:
    """При старте (после падения/рестарта) сверяем то, что записано в
    positions.json, с тем, что РЕАЛЬНО на кошельке — см. docstring
    common/position_reconciliation.py про честную границу этой проверки."""
    if not positions:
        return
    balances: dict[str, int] = {}
    for token in positions:
        try:
            erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            balances[token] = await erc20.functions.balanceOf(account_address).call()
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
        f"[wakefinder/eth copytrade] расхождение positions.json с реальным балансом при старте — проверьте вручную: {lines}",
    )


async def run(
    watched_wallets: frozenset[str],
    token_allowlist: frozenset[str] = frozenset(),
    token_denylist: frozenset[str] = frozenset(),
):
    settings = get_settings()
    account = Account.from_key(settings.resolved_eth_private_key())
    _wallet_lock_handle = wallet_lock.acquire_wallet_lock(settings.heartbeat_dir, account.address, "eth_copytrade")

    # Мутируемые копии — живой конфиг (см. wakefinder/live_config.py)
    # обновляет ИХ ЖЕ объекты in place на каждом опросе, не пересоздаёт.
    watched_wallets = {a.lower() for a in watched_wallets}
    token_allowlist = {a.lower() for a in token_allowlist}
    token_denylist = {a.lower() for a in token_denylist}
    live_config.seed_if_missing(settings.live_config_file, watched_wallets, token_allowlist, token_denylist)

    positions = _load_positions(settings.copytrade_positions_file)
    positions_lock = asyncio.Lock()
    stuck_tracker = StuckPositionTracker(settings.stuck_position_threshold)
    consensus = ConsensusTracker(settings.copytrade_min_consensus_wallets, settings.copytrade_consensus_window_seconds)
    canary = CanaryController(settings, settings.canary_start_fraction, settings.canary_ramp_trades)
    last_drawdown_check = 0.0
    last_live_config_check = 0.0

    # Основной WS-провайдер + опциональные дополнительные для гонки за
    # обнаружение pending-tx кита (см. common/race.py и ту же заметку в
    # chains/eth/main.py) — здесь особенно важно: копитрейд и так уже
    # оптимизирован под скорость входа, гонка провайдеров — тот же принцип.
    ws_urls = [settings.eth_rpc_ws_url.get_secret_value()]
    ws_urls += [u.strip() for u in settings.eth_rpc_ws_urls.split(",") if u.strip()]

    async with AsyncExitStack() as stack:
        w3_connections = [
            await stack.enter_async_context(AsyncWeb3.persistent_websocket(WebsocketProviderV2(url)))
            for url in ws_urls
        ]
        w3 = w3_connections[0]
        chain_id = await w3.eth.chain_id
        await _reconcile_positions_startup(w3, account.address, positions, settings)
        watchers = [
            UniswapV2Watcher(
                conn, settings.eth_router_address, pool_registry={}, min_amount_in=2**256 - 1,
                watched_wallets=watched_wallets, factory_address=settings.eth_factory_address,
            )
            for conn in w3_connections
        ]

        stop_loss_task = asyncio.create_task(
            _stop_loss_loop(
                w3, account, settings.eth_router_address, chain_id, positions, positions_lock,
                settings.copytrade_positions_file, settings.trade_log_file, settings.copytrade_stop_loss_pct,
                settings.copytrade_stop_loss_check_interval_seconds, stuck_tracker,
            )
        )
        heartbeat_path = os.path.join(settings.heartbeat_dir, "eth_copytrade.heartbeat")
        heartbeat_task = asyncio.create_task(heartbeat.loop(heartbeat_path, settings.heartbeat_interval_seconds))

        watch_streams = [(lambda w=watcher: with_reconnect(w.watch)) for watcher in watchers]
        try:
            async for swap in race_watchers(watch_streams):
                if killswitch.is_engaged(settings.kill_switch_file):
                    logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                    send_telegram_alert(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, "[wakefinder/eth copytrade] kill switch присутствует — бот остановлен")
                    return

                now = time.time()
                if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                    last_drawdown_check = now
                    fraction = canary.update(settings.trade_log_file, "eth")
                    if fraction < 1.0:
                        logger.info("canary: текущий размер позиции = %.0f%% от полного (разгон по мере накопления сделок)", fraction * 100)
                    async with positions_lock:
                        positions_snapshot = dict(positions)
                    unrealized = await _unrealized_pnl(w3, positions_snapshot)
                    status = check_drawdown(
                        settings.trade_log_file, "eth", settings.drawdown_window_seconds,
                        int(settings.max_drawdown_eth * 10**18), unrealized_pnl=unrealized,
                    )
                    if status.breached:
                        logger.critical(
                            "просадка за окно realized=%d unrealized=%d wei превысила лимит — включаю kill switch",
                            status.realized_pnl, status.unrealized_pnl,
                        )
                        send_telegram_alert(
                            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
                            f"[wakefinder/eth copytrade] просадка realized={status.realized_pnl} "
                            f"unrealized={status.unrealized_pnl} wei превысила лимит — kill switch",
                        )
                        killswitch.engage(settings.kill_switch_file, "drawdown breach: eth copytrade")
                        return

                if now - last_live_config_check >= settings.live_config_check_interval_seconds:
                    last_live_config_check = now
                    live = live_config.load_live_config(settings.live_config_file)
                    if live_config.sync_set(watched_wallets, live["watched_wallets"]):
                        for w in watchers:
                            live_config.sync_set(w.watched_wallets, live["watched_wallets"])
                        logger.info("live-конфиг: watched_wallets обновлены (%d)", len(watched_wallets))
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
                    holds_token_in = swap.token_in.lower() in positions

                if holds_token_in:
                    await _exit_position(
                        w3, account, settings.eth_router_address, chain_id, swap.token_in,
                        "зеркальный выход за китом", positions, positions_lock, settings.copytrade_positions_file, settings.trade_log_file,
                    )
                    consensus.clear(swap.token_in)
                    continue

                # найден сигнал покупки от watched-кошелька -> копим консенсус,
                # входим только когда наберётся нужное число РАЗНЫХ кошельков
                reached = consensus.record_buy(swap.token_out, swap.sender)
                if not reached:
                    continue
                consensus.clear(swap.token_out)

                await _try_enter(
                    w3, account, settings.eth_router_address, chain_id, swap.token_in, swap.token_out,
                    swap.pool_address, swap.sender, settings.copytrade_size_pct, settings.copytrade_max_total_exposure_pct,
                    positions, positions_lock, settings.copytrade_positions_file, settings.trade_log_file, swap.detected_at,
                )
        finally:
            stop_loss_task.cancel()
            heartbeat_task.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(watched_wallets=frozenset()))
