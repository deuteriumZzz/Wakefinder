"""Тесты run_jit_backtest — фейковый pool (slot0/liquidity/token0/token1/
tickSpacing/events.Swap), реальные liquidity_for_amounts/wide_range_around_tick
из common/univ3_math.py переиспользуются НАПРЯМУЮ (не подделаны) —
проверяем интеграцию бэктеста с живой математикой, не переизобретаем
test_univ3_math.py."""

import asyncio

from wakefinder.chains.eth.jit_backtest import run_jit_backtest

POOL = "0x1111111111111111111111111111111111111111"
TOKEN0 = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"  # WETH
TOKEN1 = "0x2222222222222222222222222222222222222222"
FEE = 3000
TICK_SPACING = 60


def _swap_log(block_number, amount0, amount1):
    return {"blockNumber": block_number, "args": {"amount0": amount0, "amount1": amount1}}


class _Call:
    def __init__(self, value):
        self._value = value

    async def call(self, block_identifier=None):
        return self._value


class _PoolFunctions:
    def __init__(self, tick, pool_liquidity):
        self._tick = tick
        self._pool_liquidity = pool_liquidity

    def token0(self):
        return _Call(TOKEN0)

    def token1(self):
        return _Call(TOKEN1)

    def tickSpacing(self):
        return _Call(TICK_SPACING)

    def slot0(self):
        return _Call((0, self._tick, 0, 0, 0, 0, True))

    def liquidity(self):
        return _Call(self._pool_liquidity)


class _EventSwap:
    def __init__(self, logs):
        self._logs = logs

    async def get_logs(self, fromBlock, toBlock):
        return [log for log in self._logs if fromBlock <= log["blockNumber"] <= toBlock]


class _FakeEvents:
    def __init__(self, logs):
        self.Swap = _EventSwap(logs)


class _FakePool:
    def __init__(self, tick, pool_liquidity, logs):
        self.functions = _PoolFunctions(tick, pool_liquidity)
        self.events = _FakeEvents(logs)


class _FakeEth:
    def __init__(self, pool):
        self._pool = pool

    def contract(self, address, abi):
        return self._pool


class _FakeW3:
    def __init__(self, pool):
        self.eth = _FakeEth(pool)


def test_weth_in_swap_counts_toward_profit():
    # amount0 > 0 -> пул получил token0 (WETH) -> token_in == WETH
    logs = [_swap_log(100, amount0=10 * 10**18, amount1=-5000 * 10**6)]
    pool = _FakePool(tick=0, pool_liquidity=10**12, logs=logs)
    w3 = _FakeW3(pool)

    result = asyncio.run(run_jit_backtest(
        w3, POOL, TOKEN0, FEE, tick_range_half_width=100, capital0_wei=10**18, capital1_wei=10**18,
        min_swap_amount_in_wei=10**18, from_block=100, to_block=100,
    ))

    assert result.swaps_scanned == 1
    assert result.weth_side_swaps == 1
    assert result.total_simulated_profit_wei > 0


def test_non_weth_in_swap_scanned_but_no_profit():
    # amount1 > 0 -> пул получил token1 (НЕ WETH) -> token_in != WETH -> не в profit
    logs = [_swap_log(100, amount0=-10 * 10**18, amount1=5000 * 10**6)]
    pool = _FakePool(tick=0, pool_liquidity=10**12, logs=logs)
    w3 = _FakeW3(pool)

    result = asyncio.run(run_jit_backtest(
        w3, POOL, TOKEN0, FEE, tick_range_half_width=100, capital0_wei=10**18, capital1_wei=10**18,
        min_swap_amount_in_wei=10**6, from_block=100, to_block=100,
    ))

    assert result.swaps_scanned == 1
    assert result.weth_side_swaps == 0
    assert result.total_simulated_profit_wei == 0


def test_swap_below_min_amount_skipped():
    logs = [_swap_log(100, amount0=1, amount1=-1)]  # ниже min_swap_amount_in_wei
    pool = _FakePool(tick=0, pool_liquidity=10**12, logs=logs)
    w3 = _FakeW3(pool)

    result = asyncio.run(run_jit_backtest(
        w3, POOL, TOKEN0, FEE, tick_range_half_width=100, capital0_wei=10**18, capital1_wei=10**18,
        min_swap_amount_in_wei=10**18, from_block=100, to_block=100,
    ))

    assert result.swaps_scanned == 0


def test_wrong_pool_tokens_raises():
    logs = []
    pool = _FakePool(tick=0, pool_liquidity=10**12, logs=logs)
    w3 = _FakeW3(pool)
    other_weth = "0x9999999999999999999999999999999999999999"

    try:
        asyncio.run(run_jit_backtest(
            w3, POOL, other_weth, FEE, tick_range_half_width=100, capital0_wei=10**18, capital1_wei=10**18,
            min_swap_amount_in_wei=10**18, from_block=100, to_block=100,
        ))
        raise AssertionError("должно было упасть с RuntimeError")
    except RuntimeError:
        pass


if __name__ == "__main__":
    test_weth_in_swap_counts_toward_profit()
    test_non_weth_in_swap_scanned_but_no_profit()
    test_swap_below_min_amount_skipped()
    test_wrong_pool_tokens_raises()
    print("ok")
