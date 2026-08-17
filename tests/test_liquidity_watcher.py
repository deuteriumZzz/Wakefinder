"""Тест LiquidityAddWatcher — реальный Web3().eth.contract() для построения
calldata addLiquidityETH (тот же принцип, что test_snipe_filter.py:
check_round_trip_sellable — ABI-кодирование должно проверяться настоящим
энкодером, не заглушкой, после критических ABI-багов этой сессии, см.
tests/test_tx_signing.py)."""

import asyncio

from hexbytes import HexBytes
from web3 import Web3

from wakefinder.chains.eth.abi import ROUTER_ABI
from wakefinder.chains.eth.liquidity_watcher import LiquidityAddWatcher

ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
TOKEN = "0x1111111111111111111111111111111111111111"
CREATOR = "0x2222222222222222222222222222222222222222"

_ENCODER = Web3()


def _add_liquidity_eth_tx(amount_token_desired: int, amount_eth: int) -> dict:
    router = _ENCODER.eth.contract(address=ROUTER, abi=ROUTER_ABI)
    built = router.functions.addLiquidityETH(
        token=TOKEN, amountTokenDesired=amount_token_desired, amountTokenMin=0, amountETHMin=0,
        to=CREATOR, deadline=9999999999,
    ).build_transaction({
        "from": CREATOR, "value": amount_eth, "gas": 300_000, "nonce": 0,
        "maxFeePerGas": 1, "maxPriorityFeePerGas": 1, "chainId": 1,
    })
    return {"to": ROUTER, "input": built["data"], "value": amount_eth}


def _swap_tx() -> dict:
    router = _ENCODER.eth.contract(address=ROUTER, abi=ROUTER_ABI)
    built = router.functions.swapExactETHForTokens(
        amountOutMin=0, path=[TOKEN, TOKEN], to=CREATOR, deadline=9999999999,
    ).build_transaction({
        "from": CREATOR, "value": 10**18, "gas": 300_000, "nonce": 0,
        "maxFeePerGas": 1, "maxPriorityFeePerGas": 1, "chainId": 1,
    })
    return {"to": ROUTER, "input": built["data"], "value": 10**18}


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
        self.subscribed = False

    def subscribe(self, kind):
        assert kind == "newPendingTransactions"
        self.subscribed = True
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


def test_watch_yields_pending_liquidity_add_for_add_liquidity_eth():
    tx = _add_liquidity_eth_tx(amount_token_desired=5 * 10**21, amount_eth=2 * 10**18)
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = LiquidityAddWatcher(w3, ROUTER, min_amount_eth=10**17)

    async def _collect():
        return [item async for item in watcher.watch()]

    results = asyncio.run(_collect())
    assert len(results) == 1
    assert results[0].token == TOKEN
    assert results[0].amount_token_desired == 5 * 10**21
    assert results[0].amount_eth == 2 * 10**18


def test_watch_ignores_other_router_functions():
    tx = _swap_tx()
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = LiquidityAddWatcher(w3, ROUTER, min_amount_eth=0)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert asyncio.run(_collect()) == []


def test_watch_filters_below_min_amount_eth():
    tx = _add_liquidity_eth_tx(amount_token_desired=5 * 10**21, amount_eth=10**16)  # ниже порога
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = LiquidityAddWatcher(w3, ROUTER, min_amount_eth=10**17)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert asyncio.run(_collect()) == []


def test_watch_deduplicates_same_tx_hash():
    tx = _add_liquidity_eth_tx(amount_token_desired=5 * 10**21, amount_eth=2 * 10**18)
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32), HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = LiquidityAddWatcher(w3, ROUTER, min_amount_eth=0)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert len(asyncio.run(_collect())) == 1


if __name__ == "__main__":
    test_watch_yields_pending_liquidity_add_for_add_liquidity_eth()
    test_watch_ignores_other_router_functions()
    test_watch_filters_below_min_amount_eth()
    test_watch_deduplicates_same_tx_hash()
    print("ok")
