"""Полный цикл: слежка -> симуляция -> сборка двуногого бандла -> отправка.

Порядок в бандле — [victim_raw, buy_leg_raw, sell_leg_raw]: нога продажи ОБЯЗАНА
приземлиться после транзакции жертвы — в этом весь смысл, именно сделка жертвы
делает цену в целевом пуле привлекательной для продажи. Упаковка всех трёх
транзакций вместе означает, что либо весь арбитраж исполняется атомарно, либо
не исполняется ничего: без частичных исполнений, и без траты газа, если бандл
не попал в блок (отправка только через Flashbots никогда не касается
публичного мемпула, так что неприбыльный/непросимулированный бандл просто
никогда не рассылается).

Требует, чтобы каждый роутер, через который может пройти сделка (целевой + все
референсные), уже имел ERC20-approve от кошелька ETH_PRIVATE_KEY на каждый
задействованный токен — разовая настройка, не часть горячего пути.

Требует RPC-провайдера, поддерживающего eth_getRawTransactionByHash для
транзакции жертвы — это не универсально; при отсутствии поддержки падаем
громко (не молча), а не поставляем непроверенную реконструкцию сырой
транзакции. Не запускается end-to-end без профинансированного тестнет-кошелька
и такого провайдера — проверяйте там до того, как касаться капитала в mainnet.

ponytail: обработка свопов последовательная (send() каждой возможности
завершается прежде, чем следующей вообще назначается nonce) — намеренно, не
недосмотр. Nonce каждый раз запрашивается заново: бандл, отправленный только
через Flashbots и не попавший в блок, не оставляет следа в собственном
представлении мемпула у ноды, так что свежий get_transaction_count()
естественным образом переиспользует тот же nonce при повторной попытке и
продвигается вперёд только когда бандл реально приземлился. Параллельная
обработка позволила бы двум одновременным бандлам схватить один и тот же
nonce и войти в гонку — чинить это нужно через пул nonce'ов или блокировку,
а это не стоит сложности для бота, который (по задумке) не колоцирован и так
не выигрывает гонки за задержку; последовательная обработка меняет небольшой
шанс пропустить пересекающуюся возможность в течение одного блока на
доказуемое отсутствие гонок по nonce.
"""

import asyncio
import logging
import os
import time

from eth_account import Account
from web3 import AsyncWeb3, Web3, WebsocketProviderV2

from wakefinder.chains.eth.abi import ERC20_ABI, ROUTER_ABI
from wakefinder.chains.eth.sender import FlashbotsBundleSender
from wakefinder.chains.eth.simulator import GAS_LIMIT, TwoPoolArbSimulator
from wakefinder.chains.eth.watcher import UniswapV2Watcher
from wakefinder.common import heartbeat, killswitch, trade_log
from wakefinder.common.adaptive_tip import AdaptiveTipController
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.drawdown import check_drawdown
from wakefinder.common.allowlist import validate_token_allowlist
from wakefinder.common.config import get_settings
from wakefinder.common.interfaces import Bundle, PendingSwap, SimResult
from wakefinder.common.reconnect import with_reconnect

SLIPPAGE_BPS = 100  # допуск 1% между симуляцией и исполнением в сети
BASE_PRIORITY_FEE_WEI = Web3.to_wei(2, "gwei")  # минимальный tip даже если доля от прибыли округляется почти до 0
_ENCODER = Web3()  # без провайдера: чистое офлайн-кодирование ABI, никогда не делает RPC-вызов

logger = logging.getLogger("wakefinder.eth")


def _to_0x_hex(raw: bytes) -> str:
    hex_str = bytes(raw).hex()
    return hex_str if hex_str.startswith("0x") else "0x" + hex_str


def _sign_leg(router_address: str, account, chain_id: int, nonce: int, max_fee: int, priority_fee: int, path: list[str], amount_in: int, amount_out_min: int) -> bytes:
    router = _ENCODER.eth.contract(address=router_address, abi=ROUTER_ABI)
    tx = router.functions.swapExactTokensForTokens(
        amount_in,
        amount_out_min,
        path,
        account.address,
        int(time.time()) + 60,
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
    return account.sign_transaction(tx).raw_transaction


def _compute_fees(base_fee: int, max_gas_gwei: float, expected_profit_wei: int, profit_share_bps: int) -> tuple[int, int]:
    """maxFeePerGas/maxPriorityFeePerGas, с tip'ом, поднятым до доли захваченной
    прибыли — билдеры сортируют бандлы по суммарной ценности (комиссии +
    переводы), так что плоский минимальный tip обычно проигрывает аукцион за
    включение бандлу, который предлагает цену ближе к своей реальной выгоде."""
    tip_pool = expected_profit_wei * profit_share_bps // 10_000
    priority_fee = max(BASE_PRIORITY_FEE_WEI, tip_pool // (2 * GAS_LIMIT))
    max_fee = min(base_fee * 2 + priority_fee, Web3.to_wei(max_gas_gwei, "gwei"))
    return max_fee, priority_fee


async def _gas_fees(w3: AsyncWeb3, max_gas_gwei: float, expected_profit_wei: int, profit_share_bps: int) -> tuple[int, int]:
    latest = await w3.eth.get_block("latest")
    return _compute_fees(latest["baseFeePerGas"], max_gas_gwei, expected_profit_wei, profit_share_bps)


def _build_bundle_legs(account, chain_id: int, nonce: int, max_fee: int, priority_fee: int, swap: PendingSwap, sim: SimResult) -> list[bytes]:
    buy_amount_out_min = sim.bought_amount * (10_000 - SLIPPAGE_BPS) // 10_000
    buy_leg = _sign_leg(
        sim.buy_router, account, chain_id, nonce, max_fee, priority_fee,
        [swap.token_in, swap.token_out], sim.amount_in, buy_amount_out_min,
    )

    expected_final_out = sim.amount_in + sim.expected_profit_wei
    sell_amount_out_min = expected_final_out * (10_000 - SLIPPAGE_BPS) // 10_000
    sell_leg = _sign_leg(
        sim.sell_router, account, chain_id, nonce + 1, max_fee, priority_fee,
        [swap.token_out, swap.token_in], sim.bought_amount, sell_amount_out_min,
    )
    return [buy_leg, sell_leg]


async def _has_sufficient_balance(w3: AsyncWeb3, account_address: str, token_in: str, amount_in: int, gas_reserve_wei: int) -> bool:
    eth_balance = await w3.eth.get_balance(account_address)
    if eth_balance < gas_reserve_wei:
        logger.warning("недостаточно ETH на газ: есть=%d нужно=%d", eth_balance, gas_reserve_wei)
        return False

    token = w3.eth.contract(address=token_in, abi=ERC20_ABI)
    token_balance = await token.functions.balanceOf(account_address).call()
    if token_balance < amount_in:
        logger.warning("недостаточно баланса token_in: есть=%d нужно=%d токен=%s", token_balance, amount_in, token_in)
        return False
    return True


def _all_configured_tokens(
    pool_registry: dict[tuple[str, str], str], reference_pools: dict[str, dict[str, str]]
) -> set[str]:
    tokens = {t for pair in pool_registry for t in pair}
    for ref in reference_pools.values():
        tokens.update({v for k, v in ref.items() if k != "pool" and k != "router"})
    return tokens


async def run(
    pool_registry: dict[tuple[str, str], str],
    reference_pools: dict[str, dict[str, str]],
    min_amount_in: int,
    watched_wallets: frozenset[str] = frozenset(),
    token_allowlist: frozenset[str] = frozenset(),
):
    validate_token_allowlist(_all_configured_tokens(pool_registry, reference_pools), token_allowlist)

    settings = get_settings()
    account = Account.from_key(settings.eth_private_key.get_secret_value())
    fb_signer = Account.from_key(settings.flashbots_signer_key.get_secret_value())
    tip = AdaptiveTipController(initial_bps=settings.profit_share_bps)
    consecutive_failures = 0
    last_drawdown_check = 0.0
    heartbeat_path = os.path.join(settings.heartbeat_dir, "eth_arb.heartbeat")
    heartbeat_task = asyncio.create_task(heartbeat.loop(heartbeat_path, settings.heartbeat_interval_seconds))

    provider = WebsocketProviderV2(settings.eth_rpc_ws_url.get_secret_value())
    async with AsyncWeb3.persistent_websocket(provider) as w3:
        chain_id = await w3.eth.chain_id
        watcher = UniswapV2Watcher(w3, settings.eth_router_address, pool_registry, min_amount_in, watched_wallets)
        simulator = TwoPoolArbSimulator(w3, settings.eth_router_address, reference_pools)
        sender = FlashbotsBundleSender(
            rpc_url=settings.eth_rpc_http_url.get_secret_value(),
            signer_account=fb_signer,
        )

        async for swap in with_reconnect(watcher.watch):
            if killswitch.is_engaged(settings.kill_switch_file):
                logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                send_telegram_alert(settings.telegram_bot_token, settings.telegram_chat_id, "[wakefinder/eth arb] kill switch присутствует — бот остановлен")
                heartbeat_task.cancel()
                return

            now = time.time()
            if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                last_drawdown_check = now
                status = check_drawdown(settings.trade_log_file, "eth", settings.drawdown_window_seconds, int(settings.max_drawdown_eth * 10**18))
                if status.breached:
                    logger.critical("просадка за окно %d wei превысила лимит — включаю kill switch", status.realized_pnl)
                    send_telegram_alert(
                        settings.telegram_bot_token, settings.telegram_chat_id,
                        f"[wakefinder/eth arb] просадка {status.realized_pnl} wei превысила лимит — kill switch",
                    )
                    killswitch.engage(settings.kill_switch_file, "drawdown breach: eth arb")
                    heartbeat_task.cancel()
                    return

            sim = await simulator.simulate(swap)
            if not sim.profitable:
                continue

            try:
                victim_raw = await w3.eth.get_raw_transaction(swap.tx_hash)
            except Exception as exc:
                logger.error(
                    "get_raw_transaction не удался для %s (%s) — ваш RPC-провайдер, вероятно, не "
                    "поддерживает eth_getRawTransactionByHash; без этого бот не может собрать бандл.",
                    swap.tx_hash, type(exc).__name__,
                )
                continue

            max_fee, priority_fee = await _gas_fees(w3, settings.max_gas_gwei, sim.expected_profit_wei, tip.current_bps)
            gas_reserve_wei = 2 * GAS_LIMIT * max_fee
            if not await _has_sufficient_balance(w3, account.address, swap.token_in, sim.amount_in, gas_reserve_wei):
                continue

            block_number = await w3.eth.block_number
            nonce = await w3.eth.get_transaction_count(account.address, "pending")

            buy_leg, sell_leg = _build_bundle_legs(account, chain_id, nonce, max_fee, priority_fee, swap, sim)

            bundle = Bundle(
                raw_txs=[_to_0x_hex(victim_raw), _to_0x_hex(buy_leg), _to_0x_hex(sell_leg)],
                target_block=block_number + 1,
            )
            included = await sender.send(bundle)
            logger.info("swap=%s profit_wei=%d included=%s", swap.tx_hash, sim.expected_profit_wei, included)

            tip.record_outcome(included)
            trade_log.log_attempt(settings.trade_log_file, "eth", swap.pool_address, sim.expected_profit_wei, included, [swap.tx_hash])

            consecutive_failures = 0 if included else consecutive_failures + 1
            if consecutive_failures >= settings.max_consecutive_failures:
                logger.critical(
                    "%d бандлов подряд не попали в блок — включаю kill switch, проверьте бота вручную",
                    consecutive_failures,
                )
                send_telegram_alert(
                    settings.telegram_bot_token, settings.telegram_chat_id,
                    f"[wakefinder/eth arb] {consecutive_failures} бандлов подряд не попали в блок — авто-kill switch",
                )
                killswitch.engage(settings.kill_switch_file, "consecutive failures: eth arb")
                heartbeat_task.cancel()
                return


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(pool_registry={}, reference_pools={}, min_amount_in=10**18))
