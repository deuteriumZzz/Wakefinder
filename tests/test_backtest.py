"""Тест wakefinder.backtest.run_backtest с фейковым AsyncWeb3 — проверяет
склейку (реконструкция PendingSwap из Swap-логов -> simulate()), не сеть."""

import asyncio
import os

os.environ.setdefault("ETH_RPC_WS_URL", "wss://example/ws")
os.environ.setdefault("ETH_RPC_HTTP_URL", "https://example/http")
os.environ.setdefault("ETH_PRIVATE_KEY", "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")
os.environ.setdefault("FLASHBOTS_SIGNER_KEY", "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99")
os.environ.setdefault("MAX_GAS_GWEI", "1")

from wakefinder.backtest import run_backtest  # noqa: E402
from wakefinder.common.config import get_settings  # noqa: E402


class _Awaitable:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _coro():
            return self.value
        return _coro().__await__()


class _Callable:
    def __init__(self, value):
        self.value = value

    def call(self, block_identifier=None):
        return _Awaitable(self.value)


class _Functions:
    def __init__(self, reserves, token0, token1):
        self._reserves = reserves
        self._token0 = token0
        self._token1 = token1

    def getReserves(self):
        return _Callable(self._reserves)

    def token0(self):
        return _Callable(self._token0)

    def token1(self):
        return _Callable(self._token1)


class _SwapEvents:
    def __init__(self, logs):
        self._logs = logs

    async def get_logs(self, fromBlock, toBlock):  # имена как в реальном web3.py AsyncContractEvent.get_logs
        return [log for log in self._logs if fromBlock <= log["blockNumber"] <= toBlock]


class _Events:
    def __init__(self, logs):
        self.Swap = _SwapEvents(logs)


class FakePoolContract:
    def __init__(self, address, reserve0, reserve1, token0, token1, logs):
        self.address = address
        self.functions = _Functions((reserve0, reserve1, 0), token0, token1)
        self.events = _Events(logs)


class FakeEth:
    def __init__(self, contracts):
        self._contracts = contracts

    def contract(self, address, abi):
        return self._contracts[address.lower()]


class FakeW3:
    def __init__(self, eth):
        self.eth = eth


def test_run_backtest_finds_opportunity_from_historical_swap_log():
    weth = get_settings().eth_weth_address  # совпадает с тем, что run_backtest резолвит сам -> быстрый путь без роутера
    token = "0xTOKEN"
    target_pool = "0xTARGET"
    ref_pool = "0xREF"

    scale = 10**18
    target = FakePoolContract(
        target_pool, reserve0=1_000 * scale, reserve1=800 * scale, token0=weth, token1=token,
        logs=[{
            "transactionHash": type("H", (), {"hex": lambda self: "0xabc"})(),
            "blockNumber": 105,
            "args": {"amount0In": 10 * scale, "amount1In": 0, "amount0Out": 0, "amount1Out": 0},
        }],
    )
    ref = FakePoolContract(ref_pool, reserve0=1_000 * scale, reserve1=1_000 * scale, token0=weth, token1=token, logs=[])

    w3 = FakeW3(FakeEth({target_pool.lower(): target, ref_pool.lower(): ref}))

    result = asyncio.run(run_backtest(
        w3, target_router="0xTargetRouter",
        reference_pools={target_pool.lower(): {"pool": ref_pool, "router": "0xRefRouter"}},
        from_block=100, to_block=110,
    ))

    assert result.swaps_scanned == 1
    assert result.opportunities_found == 1
    assert result.total_simulated_profit_wei > 0


def test_run_backtest_respects_block_range():
    weth = get_settings().eth_weth_address  # совпадает с тем, что run_backtest резолвит сам -> быстрый путь без роутера
    token = "0xTOKEN"
    target_pool = "0xTARGET"
    ref_pool = "0xREF"

    scale = 10**18
    target = FakePoolContract(
        target_pool, reserve0=1_000 * scale, reserve1=800 * scale, token0=weth, token1=token,
        logs=[{
            "transactionHash": type("H", (), {"hex": lambda self: "0xabc"})(),
            "blockNumber": 200,  # вне запрошенного диапазона
            "args": {"amount0In": 10 * scale, "amount1In": 0, "amount0Out": 0, "amount1Out": 0},
        }],
    )
    ref = FakePoolContract(ref_pool, reserve0=1_000 * scale, reserve1=1_000 * scale, token0=weth, token1=token, logs=[])

    w3 = FakeW3(FakeEth({target_pool.lower(): target, ref_pool.lower(): ref}))

    result = asyncio.run(run_backtest(
        w3, target_router="0xTargetRouter",
        reference_pools={target_pool.lower(): {"pool": ref_pool, "router": "0xRefRouter"}},
        from_block=100, to_block=110,
    ))

    assert result.swaps_scanned == 0


if __name__ == "__main__":
    test_run_backtest_finds_opportunity_from_historical_swap_log()
    test_run_backtest_respects_block_range()
    print("ok")
