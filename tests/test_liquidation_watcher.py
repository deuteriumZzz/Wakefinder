"""Тест LiquidationWatcher — реальный Web3().eth.contract() для построения
calldata liquidationCall (тот же принцип, что test_liquidity_watcher.py:
ABI-кодирование должно проверяться настоящим энкодером, не заглушкой, после
критических ABI-багов этой сессии, см. tests/test_tx_signing.py)."""

import asyncio

from hexbytes import HexBytes
from web3 import Web3

from wakefinder.chains.eth.aave_abi import AAVE_POOL_ABI
from wakefinder.chains.eth.liquidation_watcher import LiquidationWatcher

POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
COLLATERAL = "0x1111111111111111111111111111111111111111"
DEBT = "0x2222222222222222222222222222222222222222"
USER = "0x3333333333333333333333333333333333333333"
LIQUIDATOR = "0x4444444444444444444444444444444444444444"

_ENCODER = Web3()


def _liquidation_call_tx(debt_to_cover: int) -> dict:
    pool = _ENCODER.eth.contract(address=POOL, abi=AAVE_POOL_ABI)
    built = pool.functions.liquidationCall(
        collateralAsset=COLLATERAL, debtAsset=DEBT, user=USER, debtToCover=debt_to_cover, receiveAToken=False,
    ).build_transaction({
        "from": LIQUIDATOR, "gas": 400_000, "nonce": 0,
        "maxFeePerGas": 1, "maxPriorityFeePerGas": 1, "chainId": 1,
    })
    return {"to": POOL, "input": built["data"]}


def _other_pool_call_tx() -> dict:
    pool = _ENCODER.eth.contract(address=POOL, abi=AAVE_POOL_ABI)
    # ABI с одной функцией — используем ту же функцию, но на ДРУГОМ адресе,
    # чтобы watcher отфильтровал по "to" != pool_address.
    built = pool.functions.liquidationCall(
        collateralAsset=COLLATERAL, debtAsset=DEBT, user=USER, debtToCover=100, receiveAToken=False,
    ).build_transaction({
        "from": LIQUIDATOR, "gas": 400_000, "nonce": 0,
        "maxFeePerGas": 1, "maxPriorityFeePerGas": 1, "chainId": 1,
    })
    return {"to": "0x9999999999999999999999999999999999999999", "input": built["data"]}


class _Awaitable:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _coro():
            return self.value
        return _coro().__await__()


class FakeEth:
    def __init__(self, pending_hashes, tx_by_hash):
        self._pending_hashes = pending_hashes
        self._tx_by_hash = tx_by_hash

    def subscribe(self, kind):
        assert kind == "newPendingTransactions"
        return _Awaitable("sub1")

    def get_transaction(self, tx_hash):
        return _Awaitable(self._tx_by_hash[tx_hash])

    def contract(self, address, abi):
        return _ENCODER.eth.contract(address=address, abi=abi)


class FakeWs:
    def __init__(self, pending_hashes):
        self._pending_hashes = pending_hashes

    async def process_subscriptions(self):
        for tx_hash in self._pending_hashes:
            yield {"subscription": "sub1", "result": tx_hash}


class FakeW3:
    def __init__(self, pending_hashes, tx_by_hash):
        self.eth = FakeEth(pending_hashes, tx_by_hash)
        self.ws = FakeWs(pending_hashes)


def test_watch_yields_pending_liquidation_for_liquidation_call():
    tx = _liquidation_call_tx(debt_to_cover=1000 * 10**6)
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = LiquidationWatcher(w3, POOL)

    async def _collect():
        return [item async for item in watcher.watch()]

    results = asyncio.run(_collect())
    assert len(results) == 1
    assert results[0].collateral_asset == COLLATERAL
    assert results[0].debt_asset == DEBT
    assert results[0].user == USER
    assert results[0].debt_to_cover == 1000 * 10**6


def test_watch_ignores_calls_to_other_contracts():
    tx = _other_pool_call_tx()
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = LiquidationWatcher(w3, POOL)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert asyncio.run(_collect()) == []


def test_watch_deduplicates_same_tx_hash():
    tx = _liquidation_call_tx(debt_to_cover=500)
    w3 = FakeW3(
        pending_hashes=[HexBytes("0x" + "11" * 32), HexBytes("0x" + "11" * 32)],
        tx_by_hash={HexBytes("0x" + "11" * 32): tx},
    )
    watcher = LiquidationWatcher(w3, POOL)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert len(asyncio.run(_collect())) == 1


if __name__ == "__main__":
    test_watch_yields_pending_liquidation_for_liquidation_call()
    test_watch_ignores_calls_to_other_contracts()
    test_watch_deduplicates_same_tx_hash()
    print("ok")
