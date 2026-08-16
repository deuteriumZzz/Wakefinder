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

from wakefinder.chains.eth.abi import ERC20_ABI, ROUTER_ABI
from wakefinder.chains.eth.pair_watcher import PairCreatedWatcher
from wakefinder.chains.eth.snipe_filter import check_new_pool
from wakefinder.common import heartbeat, killswitch, trade_log
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.canary import CanaryController
from wakefinder.common.config import get_settings
from wakefinder.common.drawdown import check_drawdown
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
        amount_out_min, [weth_address, token], account.address, int(time.time()) + 60,
    ).build_transaction(
        {
            "from": account.address, "value": amount_in_wei, "nonce": nonce, "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).raw_transaction
    included, tx_hash = await _send_raw(w3, raw)
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
    raw = account.sign_transaction(tx).raw_transaction
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
        amount_in, amount_out_min, [token, weth_address], account.address, int(time.time()) + 60,
    ).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).raw_transaction
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


async def run(factory_address: str | None = None, token_denylist: frozenset[str] = frozenset()):
    settings = get_settings()
    account = Account.from_key(settings.resolved_eth_private_key())

    positions = _load_positions(settings.snipe_positions_file)
    positions_lock = asyncio.Lock()
    trackers: dict[str, TrailingStopTracker] = {}
    canary = CanaryController(settings, settings.canary_start_fraction, settings.canary_ramp_trades)
    last_drawdown_check = 0.0
    test_amount_wei = Web3.to_wei(settings.snipe_test_amount_eth, "ether")
    min_liquidity_weth = Web3.to_wei(settings.snipe_min_liquidity_weth, "ether")

    async with AsyncExitStack() as stack:
        w3 = await stack.enter_async_context(AsyncWeb3.persistent_websocket(WebsocketProviderV2(settings.eth_rpc_ws_url.get_secret_value())))
        chain_id = await w3.eth.chain_id
        watcher = PairCreatedWatcher(w3, factory_address or settings.eth_factory_address)

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

                async with positions_lock:
                    at_capacity = len(positions) >= settings.snipe_max_concurrent_positions

                if at_capacity:
                    continue

                result = await check_new_pool(
                    w3, settings.eth_router_address, pool.pool_address, pool.token0, pool.token1,
                    settings.eth_weth_address, test_amount_wei, min_liquidity_weth,
                )
                if not result.passed:
                    logger.info("снайп-фильтр отклонил пул=%s: %s", pool.pool_address, result.reason)
                    continue

                if token_denylist and result.token.lower() in {t.lower() for t in token_denylist}:
                    continue

                async with positions_lock:
                    already_held = result.token in positions
                if already_held:
                    continue

                balance = await w3.eth.get_balance(account.address)
                amount_in = int(balance * settings.snipe_size_pct / 100)
                if amount_in <= 0:
                    continue

                included, tx_hash, bought_amount = await _buy(
                    w3, account, settings.eth_router_address, chain_id, settings.eth_weth_address, result.token, amount_in,
                )
                logger.info("снайп-вход: токен=%s пул=%s amount_in=%d included=%s", result.token, pool.pool_address, amount_in, included)
                trade_log.log_attempt(settings.trade_log_file, "eth", pool.pool_address, amount_in, included, [tx_hash], strategy="snipe_entry")
                if not included:
                    continue

                await _approve(w3, account, settings.eth_router_address, chain_id, result.token)

                async with positions_lock:
                    positions[result.token] = SnipePosition(
                        token=result.token, pool_address=pool.pool_address, amount_held=bought_amount,
                        entry_amount_in_wei=amount_in, opened_at=time.time(), approved=True,
                    )
                    trackers[result.token] = TrailingStopTracker(trail_pct=settings.snipe_trailing_stop_pct)
                    _save_positions(settings.snipe_positions_file, positions)
        finally:
            trailing_task.cancel()
            heartbeat_task.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
