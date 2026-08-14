"""Интеграционный тест TwoPoolArbSimulator с фейковым AsyncWeb3 — проверяет
реальную склейку (не только чистую математику amm.py, уже покрытую
test_amm.py). Именно такой тест поймал бы автоматически ту находку архитектора
про перепутанное направление арбитража (buy/sell пулы местами)."""

import asyncio
import os

os.environ.setdefault("ETH_RPC_WS_URL", "wss://example/ws")
os.environ.setdefault("ETH_RPC_HTTP_URL", "https://example/http")
os.environ.setdefault("ETH_PRIVATE_KEY", "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")
os.environ.setdefault("FLASHBOTS_SIGNER_KEY", "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99")
# Этот тест проверяет НАПРАВЛЕНИЕ арбитража и склейку, не реалистичность
# газовой экономики (при дефолтном MAX_GAS_GWEI=50 и MAX_CAPITAL_PER_BUNDLE_ETH=0.05
# газ ~0.02 ETH — сопоставимого размера с самим капом, что реалистично, но
# нерелевантно тому, что здесь проверяется).
os.environ.setdefault("MAX_GAS_GWEI", "1")

from wakefinder.chains.eth.simulator import TwoPoolArbSimulator  # noqa: E402
from wakefinder.common.interfaces import PendingSwap  # noqa: E402


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
    def __init__(self, reserves, token0):
        self._reserves = reserves
        self._token0 = token0

    def getReserves(self):
        return _Callable(self._reserves)

    def token0(self):
        return _Callable(self._token0)


class FakeContract:
    def __init__(self, address: str, reserve0: int, reserve1: int, token0: str):
        self.address = address
        self.functions = _Functions((reserve0, reserve1, 0), token0)


class FakeEth:
    def __init__(self, block_number: int, contracts: dict[str, FakeContract]):
        self.block_number = _Awaitable(block_number)
        self._contracts = contracts

    def contract(self, address: str, abi):
        return self._contracts[address.lower()]


class FakeW3:
    def __init__(self, eth: FakeEth):
        self.eth = eth


def test_simulator_buys_in_reference_sells_in_target():
    token_in = "0xIN"
    token_out = "0xOUT"
    target_pool = "0xTARGET"
    ref_pool = "0xREF"

    # Реалистичный wei-масштаб (18 decimals) — иначе прибыль тонет в оценке
    # газа (2 * GAS_LIMIT * MAX_GAS_GWEI ~ 0.02 ETH), которая считается в
    # настоящих wei независимо от масштаба тестовых чисел.
    scale = 10**18
    # target pool: кит только что купил token_out -> его стало меньше относительно token_in
    target = FakeContract(target_pool, reserve0=1_000 * scale, reserve1=800 * scale, token0=token_in)
    # референсный пул: не тронут, соотношение 1:1
    ref = FakeContract(ref_pool, reserve0=1_000 * scale, reserve1=1_000 * scale, token0=token_in)

    w3 = FakeW3(FakeEth(block_number=100, contracts={target_pool.lower(): target, ref_pool.lower(): ref}))

    simulator = TwoPoolArbSimulator(
        w3,
        target_router="0xTargetRouter",
        reference_pools={target_pool.lower(): {"pool": ref_pool, "router": "0xRefRouter"}},
    )

    swap = PendingSwap(tx_hash="0xabc", pool_address=target_pool, token_in=token_in, token_out=token_out, amount_in=10 * scale)

    sim = asyncio.run(simulator.simulate(swap))

    assert sim.profitable
    assert sim.expected_profit_wei > 0
    assert sim.amount_in > 0
    # ключевая проверка направления: покупаем там, где дёшево (референс),
    # продаём там, где кит только что поднял цену (целевой пул)
    assert sim.buy_router == "0xRefRouter"
    assert sim.sell_router == "0xTargetRouter"


def test_simulator_no_opportunity_when_pools_already_balanced():
    token_in = "0xIN"
    pool_a = "0xA"
    pool_b = "0xB"
    a = FakeContract(pool_a, reserve0=1_000_000, reserve1=1_000_000, token0=token_in)
    b = FakeContract(pool_b, reserve0=1_000_000, reserve1=1_000_000, token0=token_in)
    w3 = FakeW3(FakeEth(block_number=1, contracts={pool_a.lower(): a, pool_b.lower(): b}))

    simulator = TwoPoolArbSimulator(
        w3, target_router="0xR1", reference_pools={pool_a.lower(): {"pool": pool_b, "router": "0xR2"}}
    )
    # крошечный своп на сбалансированных пулах -> прибыли после газа быть не должно
    swap = PendingSwap(tx_hash="0xdef", pool_address=pool_a, token_in=token_in, token_out="0xOUT", amount_in=1)

    sim = asyncio.run(simulator.simulate(swap))
    assert not sim.profitable


if __name__ == "__main__":
    test_simulator_buys_in_reference_sells_in_target()
    test_simulator_no_opportunity_when_pools_already_balanced()
    print("ok")
