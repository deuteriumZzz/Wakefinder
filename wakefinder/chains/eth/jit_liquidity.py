"""JIT (just-in-time) liquidity — пятая стратегия: добавление concentrated
liquidity в Uniswap V3 ПРЯМО ПЕРЕД большим pending-свопом, чтобы захватить
непропорциональную долю торговой комиссии, и вывод ликвидности сразу после.

КОНСЕРВАТИВНЫЙ ПЕРВЫЙ ПРОХОД (осознанный выбор, не временная заглушка):

1. Широкий ФИКСИРОВАННЫЙ tick-диапазон вокруг текущей цены
   (JIT_TICK_RANGE_HALF_WIDTH), а не точный расчёт диапазона под размер
   конкретного свопа жертвы — см. docstring common/univ3_math.py. Меньше
   потенциальной прибыли (наша ликвидность разбавлена шире), но самая
   рискованная (не проверяемая вживую в этой песочнице) часть математики
   упрощена и изолирована намеренно.

2. НЕ строго атомарно в один Flashbots-бандл. Бандл [mint, tx-жертвы]
   гарантирует, что НАША ликвидность активна ДО исполнения свопа жертвы —
   это единственное, для чего реально нужна атомарность. Вывод ликвидности
   (decreaseLiquidity+collect) идёт ОТДЕЛЬНОЙ обычной транзакцией СЛЕДУЮЩИМ
   блоком, когда tokenId уже ИЗВЕСТЕН из receipt mint-транзакции — не
   предсказан заранее. Строго атомарный mint+withdraw в одной транзакции
   потребовал бы отдельного Solidity-контракта (чтобы использовать
   возвращаемый tokenId мидтранзакционно) — того, что этот Python-проект
   нигде не делает (см. docstring univ3_abi.py). Цена компромисса: капитал
   находится в позиции лишний блок (~12с) — при широком диапазоне это
   пренебрежимо малый ценовой риск.

3. PnL честно считается ТОЛЬКО в WETH-стороне пула (JIT_WETH_SIDE) — без
   оракула для конвертации второго актива в единую метрику пришлось бы либо
   тянуть внешнюю цену на горячий путь (см. отказ от этого в docstring
   drawdown.py), либо давать fake-precision цифру. Не-WETH сторона
   логируется отдельно, информационно, не участвует в expected_profit.

Требует ОТДЕЛЬНОГО процесса/кошелька от остальных 4 стратегий при общем
ETH_PRIVATE_KEY (то же ограничение по nonce, что и везде в проекте)."""

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack

from eth_account import Account
from web3 import AsyncWeb3, Web3, WebsocketProviderV2

from wakefinder.chains.eth.abi import ERC20_ABI
from wakefinder.chains.eth.sender import FlashbotsBundleSender
from wakefinder.chains.eth.univ3_abi import NPM_ABI, POOL_ABI
from wakefinder.chains.eth.v3_swap_watcher import V3LargeSwapWatcher
from wakefinder.common import heartbeat, killswitch, pnl_ledger, trade_log, wallet_lock
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.config import get_settings
from wakefinder.common.drawdown import check_drawdown
from wakefinder.common.interfaces import Bundle, PendingLargeSwap
from wakefinder.common.reconnect import with_reconnect
from wakefinder.common.univ3_math import liquidity_for_amounts, wide_range_around_tick

GAS_LIMIT_APPROVE = 60_000
GAS_LIMIT_MINT = 500_000
GAS_LIMIT_WITHDRAW = 350_000  # decreaseLiquidity + collect через multicall, одна транзакция
DEADLINE_SECONDS = 120
_ENCODER = Web3()

logger = logging.getLogger("wakefinder.eth.jit_liquidity")


def _to_0x_hex(raw: bytes) -> str:
    hex_str = bytes(raw).hex()
    return hex_str if hex_str.startswith("0x") else "0x" + hex_str


async def _approve_token(w3: AsyncWeb3, account, npm_address: str, chain_id: int, token: str) -> bool:
    erc20 = _ENCODER.eth.contract(address=token, abi=ERC20_ABI)
    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    tx = erc20.functions.approve(npm_address, 2**256 - 1).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": GAS_LIMIT_APPROVE,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).rawTransaction
    tx_hash = await w3.eth.send_raw_transaction(raw)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return receipt.get("status") == 1


async def _withdraw_position(w3: AsyncWeb3, account, npm, chain_id: int, token_id: int, liquidity: int) -> tuple[int, int]:
    """decreaseLiquidity + collect через multicall — одна транзакция,
    следующий блок после mint (tokenId к этому моменту уже известен из
    receipt'а mint-транзакции, см. docstring модуля). Возвращает
    (amount0_collected, amount1_collected)."""
    deadline = int(time.time()) + DEADLINE_SECONDS
    decrease_calldata = npm.encode_abi(
        "decreaseLiquidity", args=[(token_id, liquidity, 0, 0, deadline)],
    )
    collect_calldata = npm.encode_abi(
        "collect", args=[(token_id, account.address, 2**128 - 1, 2**128 - 1)],
    )

    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    tx = npm.functions.multicall([decrease_calldata, collect_calldata]).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": GAS_LIMIT_WITHDRAW,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).rawTransaction
    tx_hash = await w3.eth.send_raw_transaction(raw)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.get("status") != 1:
        logger.error("withdraw-транзакция (tokenId=%d) не удалась — ликвидность/позиция могли остаться незакрытыми, проверьте вручную", token_id)
        return 0, 0

    collect_events = npm.events.Collect().process_receipt(receipt)
    if not collect_events:
        logger.warning("withdraw-транзакция (tokenId=%d) прошла, но событие Collect не найдено в receipt — суммы неизвестны", token_id)
        return 0, 0
    args = collect_events[0]["args"]
    return args["amount0"], args["amount1"]


async def _handle_pending_swap(
    w3: AsyncWeb3, account, chain_id: int, settings, sender, pool, npm,
    token0: str, token1: str, fee: int, tick_spacing: int, weth_side: str,
    capital0: int, capital1: int, pending: PendingLargeSwap,
) -> bool | None:
    """Возвращает None, если бандл не отправлялся (пропуск не считается неудачей
    для счётчика consecutive_failures в run()), иначе True/False = included."""
    slot0 = await pool.functions.slot0().call()
    current_tick = slot0[1]

    tick_lower, tick_upper = wide_range_around_tick(current_tick, tick_spacing, settings.jit_tick_range_half_width)
    amounts = liquidity_for_amounts(current_tick, tick_lower, tick_upper, capital0, capital1)
    if amounts.liquidity <= 0:
        logger.info("JIT: нулевая ликвидность из сконфигурированного капитала при текущей цене — пропуск")
        return None

    try:
        victim_raw = await w3.eth.get_raw_transaction(pending.tx_hash)
    except Exception as exc:
        logger.error(
            "get_raw_transaction не удался для %s (%s) — RPC-провайдер, вероятно, не поддерживает eth_getRawTransactionByHash",
            pending.tx_hash, type(exc).__name__,
        )
        return None

    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    deadline = int(time.time()) + DEADLINE_SECONDS
    amount0_min = amounts.amount0 * (10_000 - settings.jit_slippage_bps) // 10_000
    amount1_min = amounts.amount1 * (10_000 - settings.jit_slippage_bps) // 10_000
    mint_tx = npm.functions.mint(
        (
            token0, token1, fee, tick_lower, tick_upper,
            amounts.amount0, amounts.amount1, amount0_min, amount1_min,
            account.address, deadline,
        )
    ).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": GAS_LIMIT_MINT,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    mint_raw = account.sign_transaction(mint_tx).rawTransaction

    target_block = await w3.eth.block_number + 1
    bundle = Bundle(raw_txs=[_to_0x_hex(mint_raw), _to_0x_hex(victim_raw)], target_block=target_block)
    included = await sender.send(bundle)

    latency_ms = (time.time() - pending.detected_at) * 1000
    logger.info("JIT mint включён=%s tick_range=[%d,%d] капитал0=%d капитал1=%d", included, tick_lower, tick_upper, amounts.amount0, amounts.amount1)
    if not included:
        trade_log.log_attempt(settings.trade_log_file, "eth", "", 0, False, [_to_0x_hex(mint_raw)], strategy="jit_liquidity", latency_ms=latency_ms)
        return False

    mint_receipt = await w3.eth.wait_for_transaction_receipt(Web3.keccak(mint_raw), timeout=60)
    increase_events = npm.events.IncreaseLiquidity().process_receipt(mint_receipt)
    if not increase_events:
        logger.error("mint включён, но событие IncreaseLiquidity не найдено — не можем определить tokenId для вывода, проверьте вручную")
        send_telegram_alert(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
            "[wakefinder/eth jit] mint включён, но tokenId не определён — позиция открыта, проверьте вручную",
        )
        return True  # бандл всё же включился — это не неудача отправки, счётчик сбрасываем
    token_id = increase_events[0]["args"]["tokenId"]

    collected0, collected1 = await _withdraw_position(w3, account, npm, chain_id, token_id, amounts.liquidity)

    weth_used = amounts.amount0 if weth_side == "token0" else amounts.amount1
    weth_collected = collected0 if weth_side == "token0" else collected1
    profit_wei = weth_collected - weth_used
    other_used = amounts.amount1 if weth_side == "token0" else amounts.amount0
    other_collected = collected1 if weth_side == "token0" else collected0
    logger.info(
        "JIT-позиция tokenId=%d закрыта: WETH-сторона профит=%d wei, другая сторона использовано=%d получено=%d (информационно, не в profit_wei)",
        token_id, profit_wei, other_used, other_collected,
    )

    trade_log.log_attempt(settings.trade_log_file, "eth", "", profit_wei, True, [_to_0x_hex(mint_raw)], strategy="jit_liquidity", latency_ms=latency_ms)
    pnl_ledger.record_closed_trade(settings.pnl_ledger_file, "eth", "jit_liquidity", profit_wei, token=pending.token_in)
    return True


def _resolve_weth_side(token0: str, token1: str, weth: str) -> str:
    if token0.lower() == weth:
        return "token0"
    if token1.lower() == weth:
        return "token1"
    raise RuntimeError("JIT: ни token0, ни token1 пула не совпадает с ETH_WETH_ADDRESS; см. README 'JIT-ликвидность' про WETH-сторону, обязательную для расчёта PnL")


async def _watch_pool_into_queue(watcher: V3LargeSwapWatcher, pool_ctx: dict, queue: "asyncio.Queue[tuple[dict, PendingLargeSwap]]") -> None:
    """Один producer-таск на пул — отдельная with_reconnect-подписка на КАЖДЫЙ
    пул (обрыв соединения по одному пулу не блокирует остальные), все события
    сливаются в общую очередь. Основной цикл в run() забирает из неё
    ПОСЛЕДОВАТЕЛЬНО — это то, что позволяет нескольким пулам шарить один
    account/nonce без блокировки: обработка (nonce -> sign -> send) никогда
    не идёт параллельно между пулами, только сама подписка на мемпул."""
    async for pending in with_reconnect(watcher.watch):
        await queue.put((pool_ctx, pending))


async def run(pools: list[dict] | None = None) -> None:
    """pools=None (по умолчанию) — старое поведение, один пул из JIT_POOL_*
    настроек. pools=[{"pool_address", "token0", "token1", "fee",
    "capital0_wei", "capital1_wei"}, ...] — несколько пулов одним процессом/
    кошельком (см. [[jit_pools]] в cli.py и configs/eth-jit-multi.toml)."""
    settings = get_settings()
    account = Account.from_key(settings.resolved_eth_private_key())
    _wallet_lock_handle = wallet_lock.acquire_wallet_lock(settings.heartbeat_dir, account.address, "eth_jit_liquidity")  # noqa: F841 — держим handle живым весь run(), см. wallet_lock.py
    fb_signer = Account.from_key(settings.resolved_flashbots_signer_key())

    if pools is None:
        pools = [{
            "pool_address": settings.jit_pool_address, "token0": settings.jit_pool_token0, "token1": settings.jit_pool_token1,
            "fee": settings.jit_pool_fee, "capital0_wei": settings.jit_capital0_wei, "capital1_wei": settings.jit_capital1_wei,
        }]
    if not pools:
        raise RuntimeError("JIT: пустой список пулов — нечего делать (см. JIT_POOL_ADDRESS или [[jit_pools]] в профиле)")

    weth = settings.eth_weth_address.lower()
    last_drawdown_check = 0.0
    consecutive_failures = 0

    async with AsyncExitStack() as stack:
        w3 = await stack.enter_async_context(AsyncWeb3.persistent_websocket(WebsocketProviderV2(settings.eth_rpc_ws_url.get_secret_value())))
        chain_id = await w3.eth.chain_id

        npm = w3.eth.contract(address=Web3.to_checksum_address(settings.jit_npm_address), abi=NPM_ABI)
        sender = FlashbotsBundleSender(
            rpc_url=settings.eth_rpc_http_url.get_secret_value(),
            signer_account=fb_signer,
            relay_urls=[u.strip() for u in settings.eth_relay_urls.split(",") if u.strip()],
            relay_api_keys=[k.strip() for k in settings.eth_relay_api_keys.split(",")],
            dry_run=settings.dry_run,
        )

        queue: asyncio.Queue = asyncio.Queue()
        approved_tokens: set[str] = set()
        producer_tasks = []
        for p in pools:
            token0 = Web3.to_checksum_address(p["token0"])
            token1 = Web3.to_checksum_address(p["token1"])
            weth_side = _resolve_weth_side(token0, token1, weth)  # проверка на СТАРТЕ, до подписки — fail loud, не на первом свопе

            pool_contract = w3.eth.contract(address=Web3.to_checksum_address(p["pool_address"]), abi=POOL_ABI)
            tick_spacing = await pool_contract.functions.tickSpacing().call()

            if not settings.dry_run:
                for token in (token0, token1):
                    if token not in approved_tokens:
                        await _approve_token(w3, account, settings.jit_npm_address, chain_id, token)
                        approved_tokens.add(token)

            pool_ctx = {
                "pool": pool_contract, "token0": token0, "token1": token1, "fee": int(p["fee"]), "tick_spacing": tick_spacing,
                "weth_side": weth_side, "capital0": int(p["capital0_wei"]), "capital1": int(p["capital1_wei"]),
            }
            watcher = V3LargeSwapWatcher(w3, settings.jit_swap_router_address, token0, token1, int(p["fee"]), int(settings.jit_min_swap_amount_in_wei))
            producer_tasks.append(asyncio.create_task(_watch_pool_into_queue(watcher, pool_ctx, queue)))

        heartbeat_path = os.path.join(settings.heartbeat_dir, "eth_jit_liquidity.heartbeat")
        heartbeat_task = asyncio.create_task(heartbeat.loop(heartbeat_path, settings.heartbeat_interval_seconds))

        try:
            while True:
                if killswitch.is_engaged(settings.kill_switch_file):
                    logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                    send_telegram_alert(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, "[wakefinder/eth jit] kill switch присутствует — бот остановлен")
                    return

                now = time.time()
                if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                    last_drawdown_check = now
                    status = check_drawdown(settings.trade_log_file, "eth", settings.drawdown_window_seconds, int(settings.max_drawdown_eth * 10**18))
                    if status.breached:
                        logger.critical("просадка за окно %d wei превысила лимит — включаю kill switch", status.realized_pnl)
                        send_telegram_alert(
                            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
                            f"[wakefinder/eth jit] просадка {status.realized_pnl} wei превысила лимит — kill switch",
                        )
                        killswitch.engage(settings.kill_switch_file, "drawdown breach: eth jit_liquidity")
                        return

                pool_ctx, pending = await queue.get()
                included = await _handle_pending_swap(
                    w3, account, chain_id, settings, sender, pool_ctx["pool"], npm, pool_ctx["token0"], pool_ctx["token1"],
                    pool_ctx["fee"], pool_ctx["tick_spacing"], pool_ctx["weth_side"], pool_ctx["capital0"], pool_ctx["capital1"], pending,
                )
                if included is None:
                    continue  # пропуск (нулевая ликвидность/RPC-ошибка получения raw tx) — не попытка, счётчик не трогаем

                consecutive_failures = 0 if included else consecutive_failures + 1
                if consecutive_failures >= settings.max_consecutive_failures:
                    logger.critical(
                        "%d бандлов подряд не попали в блок — включаю kill switch, проверьте бота вручную",
                        consecutive_failures,
                    )
                    send_telegram_alert(
                        settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
                        f"[wakefinder/eth jit] {consecutive_failures} бандлов подряд не попали в блок — авто-kill switch",
                    )
                    killswitch.engage(settings.kill_switch_file, "consecutive failures: eth jit_liquidity")
                    heartbeat_task.cancel()
                    return
        finally:
            heartbeat_task.cancel()
            for task in producer_tasks:
                task.cancel()
