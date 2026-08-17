"""Снайпинг свежесозданных пар Uniswap V2 (Factory `PairCreated`) —
принципиально третья стратегия после backrun-арбитража (main.py) и
копитрейдинга (copytrade.py): здесь нет ни существующего ценового перекоса
(арбитраж), ни кита, за которым следить (копитрейдинг) — вход по моментуму
сразу после создания пары, прошедшей дешёвый safety-фильтр
(chains/eth/snipe_filter.py — ЧЕСТНО не honeypot-детектор, см. его docstring),
выход по trailing-stop (common/trailing_stop.py), а не по сигналу извне.

РИСК: подавляющее большинство новых пар — rug/dead в первые минуты. Это
принципиально более рискованная стратегия, чем арбитраж/копитрейдинг —
держите SNIPE_SIZE_PCT маленьким, используйте canary (см. README
"Поэтапный ввод капитала") на новых профилях, отдельный кошелёк.

Вход и выход идут в ПУБЛИЧНЫЙ мемпул напрямую (не Flashbots) — тот же
компромисс скорости против sandwich-риска, что и в copytrade.py (см. его
docstring): при снайпинге скорость входа критична, а Flashbots relay-хоп её
не улучшает.

Требует ОТДЕЛЬНОГО процесса/кошелька от других 3 стратегий при общем
ETH_PRIVATE_KEY — иначе конфликт nonce (то же ограничение, что у всех
ETH-путей этого проекта).
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
from wakefinder.chains.eth.abi import ERC20_ABI, ROUTER_ABI
from wakefinder.chains.eth.liquidity_watcher import LiquidityAddWatcher
from wakefinder.chains.eth.pair_watcher import PairCreatedWatcher
from wakefinder.chains.eth.sender import FlashbotsBundleSender
from wakefinder.chains.eth.snipe_filter import check_backrun_sellable, check_new_pool, check_round_trip_sellable
from wakefinder.common import heartbeat, killswitch, pnl_ledger, trade_log
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.amm import get_amount_out
from wakefinder.common.canary import CanaryController
from wakefinder.common.config import get_settings
from wakefinder.common.drawdown import check_drawdown
from wakefinder.common.interfaces import Bundle
from wakefinder.common.reconnect import with_reconnect
from wakefinder.common.trailing_stop import TrailingStopTracker

SLIPPAGE_BPS = 300  # шире, чем у арбитража/копитрейдинга (100) — свежесозданный пул волатильнее
GAS_LIMIT = 250_000
RECEIPT_TIMEOUT_SECONDS = 60
RECEIPT_POLL_SECONDS = 2
_ENCODER = Web3()

logger = logging.getLogger("wakefinder.eth.snipe")


@dataclass
class SnipePosition:
    token: str
    pool_address: str
    amount_held: int
    entry_amount_in_wei: int  # сколько ETH потрачено на вход
    opened_at: float
    approved: bool = False


def _load_positions(path: str) -> dict[str, SnipePosition]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {k: SnipePosition(**v) for k, v in raw.items()}


def _save_positions(path: str, positions: dict[str, SnipePosition]) -> None:
    with open(path, "w") as f:
        json.dump({k: asdict(v) for k, v in positions.items()}, f, indent=2)


async def _fees(w3: AsyncWeb3) -> tuple[int, int]:
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    return max_fee, priority_fee


async def _wait_for_receipt(w3: AsyncWeb3, tx_hash) -> bool:
    deadline = time.time() + RECEIPT_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            receipt = await w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            receipt = None
        if receipt is not None:
            return receipt.get("status") == 1
        await asyncio.sleep(RECEIPT_POLL_SECONDS)
    return False


async def _send_raw(w3: AsyncWeb3, raw: bytes) -> tuple[bool, str]:
    tx_hash = await w3.eth.send_raw_transaction(raw)
    ok = await _wait_for_receipt(w3, tx_hash)
    return ok, tx_hash.hex()


async def _buy(
    w3: AsyncWeb3, account, router_address: str, chain_id: int, weth_address: str, token: str, amount_in_wei: int,
) -> tuple[bool, str, int]:
    """ETH -> token, возвращает (included, tx_hash, bought_amount) — bought_amount
    приблизительный (из котировки перед отправкой, не из логов receipt'а: декодировать
    Transfer-лог надёжнее, но для fee-on-transfer-токенов даже это не даёт точной
    цифры без знания налога — тот же компромисс, что и в copytrade.py)."""
    router = _ENCODER.eth.contract(address=router_address, abi=ROUTER_ABI)
    quote = await router.functions.getAmountsOut(amount_in_wei, [weth_address, token]).call()
    expected_out = quote[-1]
    amount_out_min = expected_out * (10_000 - SLIPPAGE_BPS) // 10_000

    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    max_fee, priority_fee = await _fees(w3)
    tx = router.functions.swapExactETHForTokens(
        amountOutMin=amount_out_min, path=[weth_address, token], to=account.address, deadline=int(time.time()) + 60,
    ).build_transaction(
        {
            "from": account.address, "value": amount_in_wei, "nonce": nonce, "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).rawTransaction
    included, tx_hash = await _send_raw(w3, raw)
    return included, tx_hash, expected_out


def _to_0x_hex(raw: bytes) -> str:
    hex_str = bytes(raw).hex()
    return hex_str if hex_str.startswith("0x") else "0x" + hex_str


async def _buy_backrun(
    w3: AsyncWeb3, sender, account, router_address: str, chain_id: int, weth_address: str, token: str,
    amount_in_wei: int, victim_raw: bytes, reserve_weth: int, reserve_token: int, target_block: int,
) -> tuple[bool, str, int]:
    """Тот же смысл, что _buy(), но вход идёт ОДНИМ Flashbots-бандлом
    [victim_raw, buy_raw] на target_block — своп исполняется в ТОМ ЖЕ блоке,
    что и addLiquidityETH создателя (см. docstring liquidity_watcher.py), а
    не после того, как пул уже подтверждён и виден публичному мемпулу.
    Котировка — через common/amm.py:get_amount_out по reserve_weth/
    reserve_token (декодированным из calldata victim-транзакции), не
    router.getAmountsOut() — тот вызов упал бы, пары ещё нет на цепи."""
    expected_out = get_amount_out(amount_in_wei, reserve_weth, reserve_token)
    amount_out_min = expected_out * (10_000 - SLIPPAGE_BPS) // 10_000

    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    max_fee, priority_fee = await _fees(w3)
    router = _ENCODER.eth.contract(address=router_address, abi=ROUTER_ABI)
    tx = router.functions.swapExactETHForTokens(
        amountOutMin=amount_out_min, path=[weth_address, token], to=account.address, deadline=int(time.time()) + 60,
    ).build_transaction(
        {
            "from": account.address, "value": amount_in_wei, "nonce": nonce, "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    buy_raw = account.sign_transaction(tx).rawTransaction

    bundle = Bundle(raw_txs=[_to_0x_hex(victim_raw), _to_0x_hex(buy_raw)], target_block=target_block)
    included = await sender.send(bundle)
    tx_hash = _to_0x_hex(Web3.keccak(buy_raw))
    return included, tx_hash, expected_out


async def _approve(w3: AsyncWeb3, account, router_address: str, chain_id: int, token: str) -> bool:
    erc20 = _ENCODER.eth.contract(address=token, abi=ERC20_ABI)
    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    max_fee, priority_fee = await _fees(w3)
    tx = erc20.functions.approve(router_address, 2**256 - 1).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": 60_000,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).rawTransaction
    included, _ = await _send_raw(w3, raw)
    return included


async def _sell(
    w3: AsyncWeb3, account, router_address: str, chain_id: int, weth_address: str, token: str, amount_in: int,
) -> tuple[bool, int]:
    """token -> ETH, возвращает (included, полученный ETH по котировке)."""
    router = _ENCODER.eth.contract(address=router_address, abi=ROUTER_ABI)
    quote = await router.functions.getAmountsOut(amount_in, [token, weth_address]).call()
    expected_out = quote[-1]
    amount_out_min = expected_out * (10_000 - SLIPPAGE_BPS) // 10_000

    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    max_fee, priority_fee = await _fees(w3)
    tx = router.functions.swapExactTokensForETH(
        amountIn=amount_in, amountOutMin=amount_out_min, path=[token, weth_address], to=account.address, deadline=int(time.time()) + 60,
    ).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).rawTransaction
    included, _ = await _send_raw(w3, raw)
    return included, expected_out


async def _current_value(w3: AsyncWeb3, router_address: str, weth_address: str, token: str, amount_held: int) -> int | None:
    router = _ENCODER.eth.contract(address=router_address, abi=ROUTER_ABI)
    try:
        quote = await router.functions.getAmountsOut(amount_held, [token, weth_address]).call()
        return quote[-1]
    except Exception:
        return None  # ликвидность высохла/rug — не можем оценить, не считаем нулём (см. drawdown-заметки в copytrade.py)


async def _exit_position(
    w3, account, router_address, chain_id, weth_address, positions, positions_lock, positions_file, trade_log_file,
    token: str, reason: str,
) -> None:
    async with positions_lock:
        pos = positions.pop(token, None)
        if pos is not None:
            _save_positions(positions_file, positions)
    if pos is None:
        return
    included, amount_out = await _sell(w3, account, router_address, chain_id, weth_address, token, pos.amount_held)
    logger.info("снайп-выход (%s): токен=%s included=%s", reason, token, included)
    trade_log.log_attempt(trade_log_file, "eth", pos.pool_address, amount_out, included, [], strategy="snipe_exit")
    if included:
        settings = get_settings()
        pnl_ledger.record_closed_trade(
            settings.pnl_ledger_file, "eth", "snipe", amount_out - pos.entry_amount_in_wei,
            token=token, opened_at=pos.opened_at,
        )
    if not included:
        settings = get_settings()
        send_telegram_alert(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
            f"[wakefinder/eth snipe] выход ({reason}) не попал в блок: токен={token} — позиция потеряна из вида, проверьте вручную",
        )


async def _trailing_stop_loop(
    w3, account, router_address, chain_id, weth_address, positions, positions_lock, positions_file, trade_log_file,
    trackers: dict[str, TrailingStopTracker], interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        async with positions_lock:
            snapshot = dict(positions)
        for token, pos in snapshot.items():
            current = await _current_value(w3, router_address, weth_address, token, pos.amount_held)
            if current is None:
                continue
            tracker = trackers.setdefault(token, TrailingStopTracker(trail_pct=get_settings().snipe_trailing_stop_pct))
            if tracker.update(current):
                await _exit_position(
                    w3, account, router_address, chain_id, weth_address, positions, positions_lock,
                    positions_file, trade_log_file, token, "trailing-stop",
                )
                trackers.pop(token, None)


async def _handle_mined_candidate(
    w3, sender, account, chain_id, settings, pool, token_denylist,
    positions, positions_lock, trackers, test_amount_wei, min_liquidity_weth,
) -> None:
    """Дефолтный путь: пул уже смайнен (pool_watcher.PairCreatedWatcher),
    вход через ПУБЛИЧНЫЙ мемпул — см. docstring модуля."""
    result = await check_new_pool(
        w3, settings.eth_router_address, pool.pool_address, pool.token0, pool.token1,
        settings.eth_weth_address, test_amount_wei, min_liquidity_weth,
    )
    if not result.passed:
        logger.info("снайп-фильтр отклонил пул=%s: %s", pool.pool_address, result.reason)
        return

    if token_denylist and result.token.lower() in {t.lower() for t in token_denylist}:
        return

    async with positions_lock:
        already_held = result.token in positions
    if already_held:
        return

    if settings.snipe_round_trip_check:
        round_trip = await check_round_trip_sellable(
            w3, sender, account, settings.eth_router_address, settings.eth_weth_address,
            result.token, chain_id, test_amount_wei,
        )
        if not round_trip.passed:
            logger.info("снайп round-trip проверка отклонила токен=%s: %s", result.token, round_trip.reason)
            return

    balance = await w3.eth.get_balance(account.address)
    amount_in = int(balance * settings.snipe_size_pct / 100)
    if amount_in <= 0:
        return

    latency_ms = (time.time() - pool.detected_at) * 1000
    included, tx_hash, bought_amount = await _buy(
        w3, account, settings.eth_router_address, chain_id, settings.eth_weth_address, result.token, amount_in,
    )
    logger.info("снайп-вход: токен=%s пул=%s amount_in=%d included=%s", result.token, pool.pool_address, amount_in, included)
    trade_log.log_attempt(settings.trade_log_file, "eth", pool.pool_address, amount_in, included, [tx_hash], strategy="snipe_entry", latency_ms=latency_ms)
    if not included:
        return

    await _approve(w3, account, settings.eth_router_address, chain_id, result.token)

    async with positions_lock:
        positions[result.token] = SnipePosition(
            token=result.token, pool_address=pool.pool_address, amount_held=bought_amount,
            entry_amount_in_wei=amount_in, opened_at=time.time(), approved=True,
        )
        trackers[result.token] = TrailingStopTracker(trail_pct=settings.snipe_trailing_stop_pct)
        _save_positions(settings.snipe_positions_file, positions)


async def _handle_backrun_candidate(
    w3, sender, account, chain_id, settings, pending, token_denylist,
    positions, positions_lock, trackers,
) -> None:
    """SNIPE_BACKRUN_MODE: pending addLiquidityETH создателя
    (liquidity_watcher.LiquidityAddWatcher), вход ОДНИМ Flashbots-бандлом
    [victim_raw, buy_raw] в target_block = block_number+1 — см. docstring
    _buy_backrun(). pending.token/amount_token_desired/amount_eth — это
    НАМЕРЕНИЕ из calldata, не гарантированный факт (см. PendingLiquidityAdd)."""
    token = pending.token
    if token_denylist and token.lower() in {t.lower() for t in token_denylist}:
        return

    async with positions_lock:
        already_held = token in positions
    if already_held:
        return

    try:
        victim_raw = await w3.eth.get_raw_transaction(pending.tx_hash)
    except Exception as exc:
        logger.error(
            "get_raw_transaction не удался для %s (%s) — ваш RPC-провайдер, вероятно, не "
            "поддерживает eth_getRawTransactionByHash; без этого бот не может собрать бандл.",
            pending.tx_hash, type(exc).__name__,
        )
        return

    block_number = await w3.eth.block_number
    target_block = block_number + 1
    reserve_weth, reserve_token = pending.amount_eth, pending.amount_token_desired

    if settings.snipe_round_trip_check:
        test_amount_wei = Web3.to_wei(settings.snipe_test_amount_eth, "ether")
        round_trip = await check_backrun_sellable(
            w3, sender, account, settings.eth_router_address, settings.eth_weth_address, token, chain_id,
            test_amount_wei, victim_raw, target_block, reserve_weth, reserve_token,
        )
        if not round_trip.passed:
            logger.info("снайп backrun round-trip проверка отклонила токен=%s: %s", token, round_trip.reason)
            return

    balance = await w3.eth.get_balance(account.address)
    amount_in = int(balance * settings.snipe_size_pct / 100)
    if amount_in <= 0:
        return

    latency_ms = (time.time() - pending.detected_at) * 1000
    included, tx_hash, bought_amount = await _buy_backrun(
        w3, sender, account, settings.eth_router_address, chain_id, settings.eth_weth_address, token,
        amount_in, victim_raw, reserve_weth, reserve_token, target_block,
    )
    logger.info("снайп backrun-вход: токен=%s amount_in=%d target_block=%d included=%s", token, amount_in, target_block, included)
    trade_log.log_attempt(settings.trade_log_file, "eth", "", amount_in, included, [tx_hash], strategy="snipe_entry", latency_ms=latency_ms)
    if not included:
        return

    await _approve(w3, account, settings.eth_router_address, chain_id, token)

    async with positions_lock:
        positions[token] = SnipePosition(
            token=token, pool_address="", amount_held=bought_amount,
            entry_amount_in_wei=amount_in, opened_at=time.time(), approved=True,
        )
        trackers[token] = TrailingStopTracker(trail_pct=settings.snipe_trailing_stop_pct)
        _save_positions(settings.snipe_positions_file, positions)


async def run(factory_address: str | None = None, token_denylist: frozenset[str] = frozenset()):
    settings = get_settings()
    account = Account.from_key(settings.resolved_eth_private_key())
    fb_signer = Account.from_key(settings.resolved_flashbots_signer_key())
    # В обычном режиме — ТОЛЬКО для round-trip симуляции (check_round_trip_sellable),
    # реальные вход/выход по-прежнему идут в публичный мемпул (см. docstring
    # модуля). В SNIPE_BACKRUN_MODE этот же sender ещё и реально ОТПРАВЛЯЕТ
    # бандл входа (_buy_backrun) — тот же relay-клиент, не отдельный.
    sender = FlashbotsBundleSender(rpc_url=settings.eth_rpc_http_url.get_secret_value(), signer_account=fb_signer)

    token_denylist = {a.lower() for a in token_denylist}
    live_config.seed_if_missing(settings.live_config_file, set(), set(), token_denylist)

    positions = _load_positions(settings.snipe_positions_file)
    positions_lock = asyncio.Lock()
    trackers: dict[str, TrailingStopTracker] = {}
    canary = CanaryController(settings, settings.canary_start_fraction, settings.canary_ramp_trades)
    last_drawdown_check = 0.0
    last_live_config_check = 0.0
    test_amount_wei = Web3.to_wei(settings.snipe_test_amount_eth, "ether")
    min_liquidity_weth = Web3.to_wei(settings.snipe_min_liquidity_weth, "ether")

    async with AsyncExitStack() as stack:
        w3 = await stack.enter_async_context(AsyncWeb3.persistent_websocket(WebsocketProviderV2(settings.eth_rpc_ws_url.get_secret_value())))
        chain_id = await w3.eth.chain_id
        watcher = (
            LiquidityAddWatcher(w3, settings.eth_router_address, min_liquidity_weth)
            if settings.snipe_backrun_mode
            else PairCreatedWatcher(w3, factory_address or settings.eth_factory_address)
        )

        trailing_task = asyncio.create_task(
            _trailing_stop_loop(
                w3, account, settings.eth_router_address, chain_id, settings.eth_weth_address,
                positions, positions_lock, settings.snipe_positions_file, settings.trade_log_file,
                trackers, settings.snipe_trailing_stop_check_interval_seconds,
            )
        )
        heartbeat_path = os.path.join(settings.heartbeat_dir, "eth_snipe.heartbeat")
        heartbeat_task = asyncio.create_task(heartbeat.loop(heartbeat_path, settings.heartbeat_interval_seconds))

        try:
            async for pool in with_reconnect(watcher.watch):
                if killswitch.is_engaged(settings.kill_switch_file):
                    logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                    send_telegram_alert(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, "[wakefinder/eth snipe] kill switch присутствует — бот остановлен")
                    return

                now = time.time()
                if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                    last_drawdown_check = now
                    fraction = canary.update(settings.trade_log_file, "eth")
                    if fraction < 1.0:
                        logger.info("canary: текущий размер позиции = %.0f%% от полного", fraction * 100)
                    status = check_drawdown(settings.trade_log_file, "eth", settings.drawdown_window_seconds, int(settings.max_drawdown_eth * 10**18))
                    if status.breached:
                        logger.critical("просадка за окно %d wei превысила лимит — включаю kill switch", status.realized_pnl)
                        send_telegram_alert(
                            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
                            f"[wakefinder/eth snipe] просадка {status.realized_pnl} wei превысила лимит — kill switch",
                        )
                        killswitch.engage(settings.kill_switch_file, "drawdown breach: eth snipe")
                        return

                if now - last_live_config_check >= settings.live_config_check_interval_seconds:
                    last_live_config_check = now
                    live = live_config.load_live_config(settings.live_config_file)
                    if live_config.sync_set(token_denylist, live["token_denylist"]):
                        logger.info("live-конфиг: token_denylist обновлён (%d)", len(token_denylist))
                    applied = live_config.apply_risk_overrides_live(settings, live["risk"])
                    if applied:
                        logger.info("live-конфиг: risk-параметры обновлены: %s", applied)
                        test_amount_wei = Web3.to_wei(settings.snipe_test_amount_eth, "ether")
                        min_liquidity_weth = Web3.to_wei(settings.snipe_min_liquidity_weth, "ether")

                async with positions_lock:
                    at_capacity = len(positions) >= settings.snipe_max_concurrent_positions

                if at_capacity:
                    continue

                if settings.snipe_backrun_mode:
                    await _handle_backrun_candidate(
                        w3, sender, account, chain_id, settings, pool, token_denylist,
                        positions, positions_lock, trackers,
                    )
                else:
                    await _handle_mined_candidate(
                        w3, sender, account, chain_id, settings, pool, token_denylist,
                        positions, positions_lock, trackers, test_amount_wei, min_liquidity_weth,
                    )
        finally:
            trailing_task.cancel()
            heartbeat_task.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
