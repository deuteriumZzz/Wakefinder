"""Ликвидации недообеспеченных позиций на Aave V3 — четвёртая стратегия,
отдельная от арбитража/копитрейдинга/снайпинга. Атомарная, как арбитраж
(нет держания направленного риска между входом и выходом), но РЕАКТИВНАЯ по
discovery (см. docstring chains/eth/liquidation_watcher.py) — конкурирует за
уже найденные другими ликвидаторами позиции (их pending liquidationCall в
публичном мемпуле), а не сканирует рынок самостоятельно.

КАПИТАЛ КОШЕЛЬКА, НЕ FLASH LOAN: Pool.liquidationCall() требует, чтобы
вызывающий УЖЕ держал debtToCover нужного debt-актива на балансе (Aave
списывает его через transferFrom) — принципиально не то же самое, что
"ликвидация без стартового капитала" через flash loan (для этого
потребовался бы отдельный Solidity-контракт с executeOperation()-колбэком,
чего этот Python-проект не делает нигде, см. README "Ликвидации на Aave V3").
Значит: держите на кошельке debt-активы, которыми готовы погашать чужой
долг (LIQUIDATION_DEBT_ASSETS), и стратегия конкурирует ТОЛЬКО за позиции с
этими debt-активами.

Профит считается ЧЕСТНО через сам протокол Aave (IPriceOracle.getAssetPrice +
IPoolDataProvider.getReserveConfigurationData.liquidationBonus) — та же
дисциплина, что у common/amm.py для арбитража, не приближение.

Требует ОТДЕЛЬНОГО процесса/кошелька от остальных 3 стратегий при общем
ETH_PRIVATE_KEY (то же ограничение по nonce, что и везде в проекте).
"""

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass

from eth_account import Account
from web3 import AsyncWeb3, Web3, WebsocketProviderV2

from wakefinder.chains.eth.aave_abi import AAVE_POOL_ABI, AAVE_POOL_DATA_PROVIDER_ABI, AAVE_PRICE_ORACLE_ABI
from wakefinder.chains.eth.abi import ERC20_ABI
from wakefinder.chains.eth.liquidation_watcher import LiquidationWatcher
from wakefinder.chains.eth.sender import FlashbotsBundleSender
from wakefinder.common import heartbeat, killswitch, pnl_ledger, trade_log, wallet_lock
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.config import get_settings
from wakefinder.common.drawdown import check_drawdown
from wakefinder.common.interfaces import Bundle, PendingLiquidation
from wakefinder.common.reconnect import with_reconnect

GAS_LIMIT_APPROVE = 60_000
ORACLE_DECIMALS = 8  # Aave PriceOracle стандартно возвращает цену в USD с 8 знаками (тот же формат, что Chainlink-фиды)
_ENCODER = Web3()

logger = logging.getLogger("wakefinder.eth.liquidate")


def _to_0x_hex(raw: bytes) -> str:
    hex_str = bytes(raw).hex()
    return hex_str if hex_str.startswith("0x") else "0x" + hex_str


@dataclass
class LiquidationProfitEstimate:
    profit_usd: float
    eth_price_usd_e8: int  # цена ETH в той же 8-decimal шкале оракула — нужна, чтобы перевести profit_usd в wei для trade_log/pnl_ledger (там всё в нативных единицах сети, не USD)


async def _estimate_profit(
    oracle, data_provider, weth_address: str,
    debt_asset: str, collateral_asset: str, debt_to_cover: int, gas_price: int, gas_limit: int,
) -> LiquidationProfitEstimate:
    """Профит = discount (liquidationBonus), который протокол платит
    ликвидатору поверх стоимости погашенного долга, минус оценка газа."""
    debt_price = await oracle.functions.getAssetPrice(debt_asset).call()
    debt_config = await data_provider.functions.getReserveConfigurationData(debt_asset).call()
    collateral_config = await data_provider.functions.getReserveConfigurationData(collateral_asset).call()
    debt_decimals = debt_config[0]
    liquidation_bonus_bps = collateral_config[3]

    debt_covered_usd = debt_to_cover * debt_price / 10**debt_decimals / 10**ORACLE_DECIMALS
    gross_profit_usd = debt_covered_usd * (liquidation_bonus_bps - 10_000) / 10_000

    eth_price = await oracle.functions.getAssetPrice(weth_address).call()
    gas_cost_usd = gas_price * gas_limit / 10**18 * eth_price / 10**ORACLE_DECIMALS

    return LiquidationProfitEstimate(profit_usd=gross_profit_usd - gas_cost_usd, eth_price_usd_e8=eth_price)


async def _approve_debt_asset(w3: AsyncWeb3, account, pool_address: str, chain_id: int, token: str) -> bool:
    erc20 = _ENCODER.eth.contract(address=token, abi=ERC20_ABI)
    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    tx = erc20.functions.approve(pool_address, 2**256 - 1).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": GAS_LIMIT_APPROVE,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).rawTransaction
    tx_hash = await w3.eth.send_raw_transaction(raw)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return receipt.get("status") == 1


async def _handle_pending_liquidation(
    w3: AsyncWeb3, account, chain_id: int, settings, sender, pool, oracle, data_provider,
    debt_assets: set[str], pending: PendingLiquidation,
) -> None:
    if pending.debt_asset.lower() not in debt_assets:
        return  # не держим/не одобрили этот debt-актив — не с чем конкурировать

    gas_price = await w3.eth.gas_price
    estimate = await _estimate_profit(
        oracle, data_provider, settings.eth_weth_address,
        pending.debt_asset, pending.collateral_asset, pending.debt_to_cover, gas_price, settings.liquidation_gas_limit,
    )
    if estimate.profit_usd < settings.liquidation_min_profit_usd:
        logger.info(
            "пропуск ликвидации user=%s: ожидаемая прибыль $%.2f < порога $%.2f",
            pending.user, estimate.profit_usd, settings.liquidation_min_profit_usd,
        )
        return

    debt_token = w3.eth.contract(address=Web3.to_checksum_address(pending.debt_asset), abi=ERC20_ABI)
    balance = await debt_token.functions.balanceOf(account.address).call()
    if balance < pending.debt_to_cover:
        logger.info(
            "пропуск ликвидации user=%s: недостаточно баланса debt-актива %s (%d < %d)",
            pending.user, pending.debt_asset, balance, pending.debt_to_cover,
        )
        return

    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    tx = pool.functions.liquidationCall(
        Web3.to_checksum_address(pending.collateral_asset), Web3.to_checksum_address(pending.debt_asset),
        Web3.to_checksum_address(pending.user), pending.debt_to_cover, False,  # receiveAToken=False — сразу базовый актив, не aToken
    ).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": settings.liquidation_gas_limit,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).rawTransaction

    target_block = await w3.eth.block_number + 1
    bundle = Bundle(raw_txs=[_to_0x_hex(raw)], target_block=target_block)
    included = await sender.send(bundle)

    latency_ms = (time.time() - pending.detected_at) * 1000
    logger.info(
        "ликвидация user=%s debt_asset=%s collateral_asset=%s included=%s ожидаемая прибыль=$%.2f",
        pending.user, pending.debt_asset, pending.collateral_asset, included, estimate.profit_usd,
    )
    expected_profit_wei = int(estimate.profit_usd / (estimate.eth_price_usd_e8 / 10**ORACLE_DECIMALS) * 10**18)
    trade_log.log_attempt(
        settings.trade_log_file, "eth", pending.collateral_asset, expected_profit_wei, included, [_to_0x_hex(raw)],
        strategy="liquidate", wallet=pending.user, latency_ms=latency_ms,
    )
    if included:
        pnl_ledger.record_closed_trade(
            settings.pnl_ledger_file, "eth", "liquidate", expected_profit_wei,
            token=pending.collateral_asset, wallet=pending.user,
        )


async def run() -> None:
    settings = get_settings()
    account = Account.from_key(settings.resolved_eth_private_key())
    _wallet_lock_handle = wallet_lock.acquire_wallet_lock(settings.heartbeat_dir, account.address, "eth_liquidate")  # noqa: F841 — держим handle живым весь run(), см. wallet_lock.py
    fb_signer = Account.from_key(settings.resolved_flashbots_signer_key())

    debt_assets = {a.strip().lower() for a in settings.liquidation_debt_assets.split(",") if a.strip()}
    if not debt_assets:
        logger.warning("LIQUIDATION_DEBT_ASSETS пуст — стратегия ничего не будет делать (не с чем конкурировать)")

    last_drawdown_check = 0.0

    async with AsyncExitStack() as stack:
        w3 = await stack.enter_async_context(AsyncWeb3.persistent_websocket(WebsocketProviderV2(settings.eth_rpc_ws_url.get_secret_value())))
        chain_id = await w3.eth.chain_id

        pool = w3.eth.contract(address=Web3.to_checksum_address(settings.aave_pool_address), abi=AAVE_POOL_ABI)
        data_provider = w3.eth.contract(address=Web3.to_checksum_address(settings.aave_pool_data_provider_address), abi=AAVE_POOL_DATA_PROVIDER_ABI)
        oracle = w3.eth.contract(address=Web3.to_checksum_address(settings.aave_price_oracle_address), abi=AAVE_PRICE_ORACLE_ABI)

        if not settings.dry_run:
            for asset in debt_assets:
                ok = await _approve_debt_asset(w3, account, settings.aave_pool_address, chain_id, Web3.to_checksum_address(asset))
                logger.info("approve debt-актива %s для Aave Pool: %s", asset, "OK" if ok else "НЕ УДАЛОСЬ")

        watcher = LiquidationWatcher(w3, settings.aave_pool_address)
        sender = FlashbotsBundleSender(
            rpc_url=settings.eth_rpc_http_url.get_secret_value(),
            signer_account=fb_signer,
            relay_urls=[u.strip() for u in settings.eth_relay_urls.split(",") if u.strip()],
            relay_api_keys=[k.strip() for k in settings.eth_relay_api_keys.split(",")],
            dry_run=settings.dry_run,
        )

        heartbeat_path = os.path.join(settings.heartbeat_dir, "eth_liquidate.heartbeat")
        heartbeat_task = asyncio.create_task(heartbeat.loop(heartbeat_path, settings.heartbeat_interval_seconds))

        try:
            async for pending in with_reconnect(watcher.watch):
                if killswitch.is_engaged(settings.kill_switch_file):
                    logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                    send_telegram_alert(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, "[wakefinder/eth liquidate] kill switch присутствует — бот остановлен")
                    return

                now = time.time()
                if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                    last_drawdown_check = now
                    status = check_drawdown(settings.trade_log_file, "eth", settings.drawdown_window_seconds, int(settings.max_drawdown_eth * 10**18))
                    if status.breached:
                        logger.critical("просадка за окно %d wei превысила лимит — включаю kill switch", status.realized_pnl)
                        send_telegram_alert(
                            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
                            f"[wakefinder/eth liquidate] просадка {status.realized_pnl} wei превысила лимит — kill switch",
                        )
                        killswitch.engage(settings.kill_switch_file, "drawdown breach: eth liquidate")
                        return

                await _handle_pending_liquidation(w3, account, chain_id, settings, sender, pool, oracle, data_provider, debt_assets, pending)
        finally:
            heartbeat_task.cancel()
