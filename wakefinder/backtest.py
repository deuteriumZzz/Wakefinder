"""Бэктест ETH backrun-арбитражной стратегии на исторических Swap-логах
целевых пулов — не новый источник данных, тот же RPC (`ETH_RPC_HTTP_URL`),
что и live-режим. Переиспользует ЖИВОЙ `TwoPoolArbSimulator.simulate()` (тот
же код, что реально торгует), не отдельную копию логики — иначе бэктест
доказывал бы прибыльность кода, который не совпадает с тем, что исполняется
в проде.

Честные ограничения (не тихие недосмотры):
- Публичные RPC-провайдеры ограничивают диапазон/глубину `eth_getLogs`
  (часто 1-10к блоков за запрос, иногда меньше исторической глубины) —
  `chunk_size` разбивает диапазон, но архивный узел для старой истории
  может всё равно понадобиться; это решение инфраструктуры, не библиотеки.
- НЕ учитывает конкуренцию с другими searcher-ботами за тот же блок —
  верхняя граница достижимой прибыли ("если бы этот бандл был единственной
  заявкой"), не гарантированный исторический результат.
  `contested_opportunities` (см. BacktestResult) — ГРУБЫЙ прокси интенсивности
  конкуренции: сколько прибыльных возможностей приходятся на блок, где
  ЦЕЛЕВОЙ пул тронула больше чем одна Swap-транзакция. Это НЕ значит "N ботов
  боролись за эту же возможность" — несколько независимых свопов в одном
  пуле в одном блоке случаются и без всякой MEV-конкуренции. Честная нижняя
  граница "здесь было оживлённо", не число реальных конкурентов — платных
  исторических данных мемпула (кто ещё бидил) у публичных RPC нет.
- Использует ТЕКУЩИЕ `MAX_GAS_GWEI`/`MIN_REFERENCE_LIQUIDITY_ETH`/т.п. из
  Settings, не исторические условия сети на момент каждого блока.

Использование — импортируйте и вызовите с реальным reference_pools (та же
структура, что и в chains/eth/main.py), CLI ниже — только smoke-test заглушка
с пустым reference_pools, как и у остальных entrypoint'ов.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass

from web3 import AsyncWeb3, WebsocketProviderV2

from wakefinder.chains.eth.abi import PAIR_ABI
from wakefinder.chains.eth.simulator import TwoPoolArbSimulator
from wakefinder.common.config import get_settings
from wakefinder.common.interfaces import PendingSwap

logger = logging.getLogger("wakefinder.backtest")


@dataclass
class BacktestResult:
    swaps_scanned: int
    opportunities_found: int
    total_simulated_profit_wei: int
    contested_opportunities: int = 0  # см. docstring модуля — грубый прокси, не число реальных конкурентов


async def run_backtest(
    w3: AsyncWeb3,
    target_router: str,
    reference_pools: dict[str, dict[str, str]],
    from_block: int,
    to_block: int,
    chunk_size: int = 2000,
) -> BacktestResult:
    simulator = TwoPoolArbSimulator(w3, target_router, reference_pools, get_settings().eth_weth_address)
    scanned = 0
    found = 0
    total_profit = 0
    contested = 0

    for pool_address in reference_pools:
        pool = w3.eth.contract(address=pool_address, abi=PAIR_ABI)
        token0 = await pool.functions.token0().call()
        token1 = await pool.functions.token1().call()

        block = from_block
        while block <= to_block:
            chunk_end = min(block + chunk_size - 1, to_block)
            logs = await pool.events.Swap.get_logs(fromBlock=block, toBlock=chunk_end)

            # Сколько Swap-логов этого пула пришлось на каждый блок — прокси
            # "оживлённости" блока для этого пула, см. docstring модуля.
            swaps_per_block: dict[int, int] = {}
            for log in logs:
                swaps_per_block[log["blockNumber"]] = swaps_per_block.get(log["blockNumber"], 0) + 1

            for log in logs:
                scanned += 1
                amount0_in = log["args"]["amount0In"]
                amount1_in = log["args"]["amount1In"]
                if amount0_in > 0:
                    token_in, token_out, amount_in = token0, token1, amount0_in
                elif amount1_in > 0:
                    token_in, token_out, amount_in = token1, token0, amount1_in
                else:
                    continue  # оба нулевые — не должно происходить у валидного Swap, пропускаем защитно

                swap = PendingSwap(
                    tx_hash=log["transactionHash"].hex(),
                    pool_address=pool_address,
                    token_in=token_in,
                    token_out=token_out,
                    amount_in=amount_in,
                )
                # реcурсы ДО этого свопа = состояние на предыдущем блоке
                sim = await simulator.simulate(swap, block_number=log["blockNumber"] - 1)
                if sim.profitable:
                    found += 1
                    total_profit += sim.expected_profit_wei
                    if swaps_per_block[log["blockNumber"]] > 1:
                        contested += 1

            block = chunk_end + 1

    return BacktestResult(
        swaps_scanned=scanned, opportunities_found=found, total_simulated_profit_wei=total_profit,
        contested_opportunities=contested,
    )


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Бэктест ETH-арбитража на исторических Swap-логах (заглушка с пустым reference_pools — см. docstring модуля)")
    parser.add_argument("--from-block", type=int, required=True)
    parser.add_argument("--to-block", type=int, required=True)
    parser.add_argument("--chunk-size", type=int, default=2000)
    args = parser.parse_args()

    settings = get_settings()
    provider = WebsocketProviderV2(settings.eth_rpc_ws_url.get_secret_value())
    async with AsyncWeb3.persistent_websocket(provider) as w3:
        result = await run_backtest(
            w3, settings.eth_router_address, reference_pools={},
            from_block=args.from_block, to_block=args.to_block, chunk_size=args.chunk_size,
        )
    print(f"Просканировано свопов: {result.swaps_scanned}")
    print(f"Найдено возможностей: {result.opportunities_found}")
    print(f"Суммарная симулированная прибыль: {result.total_simulated_profit_wei} wei")
    if result.opportunities_found:
        pct = result.contested_opportunities / result.opportunities_found * 100
        print(f"Из них с признаком конкуренции (несколько свопов в блоке): {result.contested_opportunities} ({pct:.0f}%)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
