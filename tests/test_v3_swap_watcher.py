"""Тест V3LargeSwapWatcher — реальный Web3().eth.contract() для построения
calldata exactInputSingle (тот же принцип, что test_liquidity_watcher.py/
test_liquidation_watcher.py: ABI-кодирование проверяется настоящим
энкодером, не заглушкой, после критических ABI-багов этой сессии)."""

import asyncio

from hexbytes import HexBytes
from web3 import Web3

from wakefinder.chains.eth.univ3_abi import SWAP_ROUTER_02_ABI
from wakefinder.chains.eth.v3_swap_watcher import V3LargeSwapWatcher

ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
TOKEN0 = "0x1111111111111111111111111111111111111111"
TOKEN1 = "0x2222222222222222222222222222222222222222"
OTHER_TOKEN = "0x3333333333333333333333333333333333333333"
TRADER = "0x4444444444444444444444444444444444444444"
FEE = 3000

_ENCODER = Web3()


def _exact_input_single_tx(token_in: str, token_out: str, fee: int, amount_in: int) -> dict:
    router = _ENCODER.eth.contract(address=ROUTER, abi=SWAP_ROUTER_02_ABI)
    built = router.functions.exactInputSingle(
        (token_in, token_out, fee, TRADER, amount_in, 0, 0),
    ).build_transaction({
        "from": TRADER, "gas": 300_000, "nonce": 0,
        "maxFeePerGas": 1, "maxPriorityFeePerGas": 1, "chainId": 1,
    })
    return {"to": ROUTER, "input": built["data"]}


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


def test_watch_yields_large_swap_matching_configured_pair():
    tx = _exact_input_single_tx(TOKEN0, TOKEN1, FEE, amount_in=100 * 10**18)
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = V3LargeSwapWatcher(w3, ROUTER, TOKEN0, TOKEN1, FEE, min_amount_in=10 * 10**18)

    async def _collect():
        return [item async for item in watcher.watch()]

    results = asyncio.run(_collect())
    assert len(results) == 1
    assert results[0].token_in == TOKEN0
    assert results[0].token_out == TOKEN1
    assert results[0].fee == FEE
    assert results[0].amount_in == 100 * 10**18


def test_watch_ignores_swap_below_min_amount():
    tx = _exact_input_single_tx(TOKEN0, TOKEN1, FEE, amount_in=1 * 10**18)
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = V3LargeSwapWatcher(w3, ROUTER, TOKEN0, TOKEN1, FEE, min_amount_in=10 * 10**18)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert asyncio.run(_collect()) == []


def test_watch_ignores_different_fee_tier():
    tx = _exact_input_single_tx(TOKEN0, TOKEN1, fee=500, amount_in=100 * 10**18)
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = V3LargeSwapWatcher(w3, ROUTER, TOKEN0, TOKEN1, FEE, min_amount_in=10 * 10**18)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert asyncio.run(_collect()) == []


def test_watch_ignores_different_pair():
    tx = _exact_input_single_tx(TOKEN0, OTHER_TOKEN, FEE, amount_in=100 * 10**18)
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = V3LargeSwapWatcher(w3, ROUTER, TOKEN0, TOKEN1, FEE, min_amount_in=10 * 10**18)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert asyncio.run(_collect()) == []


def test_watch_matches_pair_in_either_direction():
    # tokenIn/tokenOut в обратном порядке относительно (token0, token1) — та же пара
    tx = _exact_input_single_tx(TOKEN1, TOKEN0, FEE, amount_in=100 * 10**18)
    w3 = FakeW3(pending_hashes=[HexBytes("0x" + "11" * 32)], tx_by_hash={HexBytes("0x" + "11" * 32): tx})
    watcher = V3LargeSwapWatcher(w3, ROUTER, TOKEN0, TOKEN1, FEE, min_amount_in=10 * 10**18)

    async def _collect():
        return [item async for item in watcher.watch()]

    results = asyncio.run(_collect())
    assert len(results) == 1


def test_watch_deduplicates_same_tx_hash():
    tx = _exact_input_single_tx(TOKEN0, TOKEN1, FEE, amount_in=100 * 10**18)
    w3 = FakeW3(
        pending_hashes=[HexBytes("0x" + "11" * 32), HexBytes("0x" + "11" * 32)],
        tx_by_hash={HexBytes("0x" + "11" * 32): tx},
    )
    watcher = V3LargeSwapWatcher(w3, ROUTER, TOKEN0, TOKEN1, FEE, min_amount_in=10 * 10**18)

    async def _collect():
        return [item async for item in watcher.watch()]

    assert len(asyncio.run(_collect())) == 1


if __name__ == "__main__":
    test_watch_yields_large_swap_matching_configured_pair()
    test_watch_ignores_swap_below_min_amount()
    test_watch_ignores_different_fee_tier()
    test_watch_ignores_different_pair()
    test_watch_matches_pair_in_either_direction()
    test_watch_deduplicates_same_tx_hash()
    print("ok")
