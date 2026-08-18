"""Бэктест JIT-ликвидности (jit_liquidity.py) на исторических больших
свопах — оценка ЗАХВАЧЕННОЙ ДОЛИ комиссии через реплей ЖИВОЙ математики
(common/univ3_math.py: liquidity_for_amounts/wide_range_around_tick), не
отдельная копия формулы.

ЧЕСТНАЯ ГРАНИЦА — самая грубая аппроксимация из всех бэктестов проекта,
согласована с тем же упрощением, что и в live jit_liquidity.py:

1. Доля захваченной комиссии = our_liquidity / (our_liquidity + pool_liquidity)
   на блоке ДО свопа — предполагает, что своп НЕ выходит за пределы нашего
   диапазона и не пересекает несколько инициализированных тиков с разной
   плотностью. Точный расчёт потребовал бы симуляции прохода через тики
   блок-за-блоком, чего этот проект нигде не делает (см. "консервативный
   первый проход" в docstring jit_liquidity.py — та же математическая
   граница, распространённая теперь и на бэктест).
2. PnL считается ТОЛЬКО для свопов, где входной токен (token_in) — WETH
   (комиссия V3 собирается во ВХОДНОМ токене свопа) — та же дисциплина
   "честно только WETH-сторона", что и у live PnL в jit_liquidity.py.
   Свопы в обратную сторону считаются (weth_side_swaps не растёт), но НЕ
   входят в total_simulated_profit_wei — не гадаем курс конвертации без
   оракула, тот же принцип, что и во всём проекте.
3. НЕ учитывает конкуренцию за то же место в блоке за нашу собственную
   mint-транзакцию (тот же верхняя-граница-не-гарантия принцип, что
   contested_opportunities в backtest.py)."""

import logging
from dataclasses import dataclass

from web3 import AsyncWeb3

from wakefinder.chains.eth.univ3_abi import POOL_ABI
from wakefinder.common.univ3_math import liquidity_for_amounts, wide_range_around_tick

logger = logging.getLogger("wakefinder.eth.jit_backtest")


@dataclass
class JitBacktestResult:
    swaps_scanned: int  # крупные свопы (amount_in >= min_swap_amount_in_wei) с ненулевой нашей ликвидностью на тот момент
    weth_side_swaps: int  # из них — token_in == WETH, только они входят в profit (см. docstring модуля)
    total_simulated_profit_wei: int


async def run_jit_backtest(
    w3: AsyncWeb3,
    pool_address: str,
    weth_address: str,
    fee: int,
    tick_range_half_width: int,
    capital0_wei: int,
    capital1_wei: int,
    min_swap_amount_in_wei: int,
    from_block: int,
    to_block: int,
    chunk_size: int = 2000,
) -> JitBacktestResult:
    pool = w3.eth.contract(address=pool_address, abi=POOL_ABI)
    token0 = await pool.functions.token0().call()
    token1 = await pool.functions.token1().call()
    tick_spacing = await pool.functions.tickSpacing().call()

    if token0.lower() == weth_address.lower():
        weth_is_token0 = True
    elif token1.lower() == weth_address.lower():
        weth_is_token0 = False
    else:
        raise RuntimeError("JIT-бэктест: ни token0, ни token1 пула не совпадает с weth_address — та же проверка, что в live run()")

    scanned = 0
    weth_side_swaps = 0
    total_profit_wei = 0

    block = from_block
    while block <= to_block:
        chunk_end = min(block + chunk_size - 1, to_block)
        logs = await pool.events.Swap.get_logs(fromBlock=block, toBlock=chunk_end)

        for log in logs:
            args = log["args"]
            amount0, amount1 = args["amount0"], args["amount1"]
            # V3 Swap: положительное значение -- пул ПОЛУЧИЛ этот токен (это token_in свопа)
            if amount0 > 0:
                amount_in = amount0
                token_in_is_weth = weth_is_token0
            elif amount1 > 0:
                amount_in = amount1
                token_in_is_weth = not weth_is_token0
            else:
                continue
            if amount_in < min_swap_amount_in_wei:
                continue

            historical_block = log["blockNumber"] - 1
            try:
                slot0 = await pool.functions.slot0().call(block_identifier=historical_block)
                pool_liquidity = await pool.functions.liquidity().call(block_identifier=historical_block)
            except Exception as exc:
                logger.warning("не удалось прочитать состояние пула на блоке %d (%s) — пропуск", historical_block, type(exc).__name__)
                continue
            current_tick = slot0[1]

            tick_lower, tick_upper = wide_range_around_tick(current_tick, tick_spacing, tick_range_half_width)
            amounts = liquidity_for_amounts(current_tick, tick_lower, tick_upper, capital0_wei, capital1_wei)
            if amounts.liquidity <= 0:
                continue  # наша ликвидность вне диапазона при этой цене -- не участвовали бы в этом свопе
            scanned += 1

            total_fee = amount_in * fee // 1_000_000  # fee в millionths (напр. 3000 = 0.3%), тот же формат, что POOL_ABI.fee()
            denom = amounts.liquidity + pool_liquidity
            our_share = amounts.liquidity / denom if denom > 0 else 0.0
            our_fee = int(total_fee * our_share)

            if token_in_is_weth:
                weth_side_swaps += 1
                total_profit_wei += our_fee

        block = chunk_end + 1

    return JitBacktestResult(swaps_scanned=scanned, weth_side_swaps=weth_side_swaps, total_simulated_profit_wei=total_profit_wei)
