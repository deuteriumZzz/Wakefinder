"""Бэктест liquidate.py на исторических событиях LiquidationCall — профит-
формула реплеится через ЖИВОЙ _estimate_profit() (chains/eth/liquidate.py,
теперь с опциональным block_identifier), тот же принцип, что backtest.py
для арбитража: тестируем код, который реально считает прибыль в проде, не
отдельную копию формулы.

ЧЕСТНАЯ ГРАНИЦА: сканирует LiquidationCall-события, которые УЖЕ
СЛУЧИЛИСЬ — кто-то другой выиграл эту ликвидацию (та же "верхняя граница
достижимого", не гарантированный результат, что и у contested_opportunities
в backtest.py). Отвечает на "если бы я гнался за эти же позиции — сказала
бы моя профит-формула да", НЕ на "выиграл бы я гонку" (для этого нужны бы
были исторические данные мемпула — их у публичного RPC нет, та же граница,
что у остального проекта). debtToCover берётся из САМОГО события (реальная
сумма, которую заплатил победивший ликвидатор), не из отдельной
реконструкции — честнее, чем гадать через close factor, как это делает
liquidation_scanner.py для ЖИВЫХ (ещё не решённых) позиций.

gas_price — baseFeePerGas исторического блока (лучшее доступное
приближение исторической цены газа), не точная цена реального конкурента —
та же оговорка, что у backtest.py про MAX_GAS_GWEI."""

import logging
from dataclasses import dataclass

from web3 import AsyncWeb3

from wakefinder.chains.eth.aave_abi import AAVE_POOL_ABI
from wakefinder.chains.eth.liquidate import _estimate_profit

logger = logging.getLogger("wakefinder.eth.liquidate_backtest")


@dataclass
class LiquidationBacktestResult:
    events_scanned: int
    matching_debt_asset: int  # сколько из просканированных — по НАШИМ настроенным debt_assets
    profitable_count: int  # из matching_debt_asset — сколько прошли бы min_profit_usd
    total_simulated_profit_usd: float


async def run_liquidation_backtest(
    w3: AsyncWeb3,
    pool_address: str,
    oracle,
    data_provider,
    weth_address: str,
    debt_assets: set[str],
    min_profit_usd: float,
    gas_limit: int,
    from_block: int,
    to_block: int,
    chunk_size: int = 2000,
) -> LiquidationBacktestResult:
    pool = w3.eth.contract(address=pool_address, abi=AAVE_POOL_ABI)
    scanned = 0
    matching = 0
    profitable = 0
    total_profit_usd = 0.0

    block = from_block
    while block <= to_block:
        chunk_end = min(block + chunk_size - 1, to_block)
        logs = await pool.events.LiquidationCall.get_logs(fromBlock=block, toBlock=chunk_end)

        for log in logs:
            scanned += 1
            args = log["args"]
            debt_asset = args["debtAsset"]
            if debt_asset.lower() not in debt_assets:
                continue
            matching += 1

            historical_block = log["blockNumber"] - 1  # состояние ДО этой ликвидации, тот же принцип, что backtest.py (block_number - 1)
            block_data = await w3.eth.get_block(historical_block)
            gas_price = block_data.get("baseFeePerGas", 0)

            try:
                estimate = await _estimate_profit(
                    oracle, data_provider, weth_address, debt_asset, args["collateralAsset"],
                    args["debtToCover"], gas_price, gas_limit, block_identifier=historical_block,
                )
            except Exception as exc:
                logger.warning("_estimate_profit не удался для блока %d (%s) — пропуск", log["blockNumber"], type(exc).__name__)
                continue

            if estimate.profit_usd >= min_profit_usd:
                profitable += 1
                total_profit_usd += estimate.profit_usd

        block = chunk_end + 1

    return LiquidationBacktestResult(
        events_scanned=scanned, matching_debt_asset=matching, profitable_count=profitable,
        total_simulated_profit_usd=total_profit_usd,
    )
