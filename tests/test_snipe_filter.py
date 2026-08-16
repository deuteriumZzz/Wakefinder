import asyncio

from wakefinder.chains.eth.snipe_filter import (
    NO_QUOTE,
    THIN_LIQUIDITY,
    WETH_PATH_UNSUPPORTED,
    check_new_pool,
)

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TOKEN = "0xTOKEN000000000000000000000000000000001"
ROUTER = "0xROUTER"
POOL = "0xPOOL"


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

    def call(self):
        return _Awaitable(self.value)


class _RaisingCallable:
    def call(self):
        raise RuntimeError("нет ликвидности для этого пути")


class _PairFunctions:
    def __init__(self, reserves):
        self._reserves = reserves

    def getReserves(self):
        return _Callable(self._reserves)


class _RouterFunctions:
    def __init__(self, buy_out, sell_ok=True):
        self._buy_out = buy_out
        self._sell_ok = sell_ok

    def getAmountsOut(self, amount_in, path):
        if path[0].lower() == WETH.lower():  # buy leg: WETH -> token
            return _Callable([amount_in, self._buy_out])
        if self._sell_ok:  # sell leg: token -> WETH
            return _Callable([amount_in, amount_in // 2])
        return _RaisingCallable()


class FakeW3:
    def __init__(self, reserves, buy_out, sell_ok=True):
        self._pair = type("P", (), {"functions": _PairFunctions(reserves)})()
        self._router = type("R", (), {"functions": _RouterFunctions(buy_out, sell_ok)})()

    @property
    def eth(self):
        return self

    def contract(self, address, abi):
        return self._pair if address == POOL else self._router


def test_rejects_pair_without_weth():
    w3 = FakeW3(reserves=(0, 0, 0), buy_out=0)
    result = asyncio.run(
        check_new_pool(w3, ROUTER, POOL, "0xTOKENA", "0xTOKENB", WETH, test_amount_wei=10**16, min_liquidity_weth=10**18)
    )
    assert result.passed is False
    assert result.reason == WETH_PATH_UNSUPPORTED


def test_rejects_thin_liquidity():
    w3 = FakeW3(reserves=(5 * 10**17, 1000 * 10**18, 0), buy_out=10**18)  # WETH-сторона (token0) = 0.5 ETH
    result = asyncio.run(
        check_new_pool(w3, ROUTER, POOL, WETH, TOKEN, WETH, test_amount_wei=10**16, min_liquidity_weth=10**18)
    )
    assert result.passed is False
    assert result.reason == THIN_LIQUIDITY


def test_rejects_when_sell_quote_unavailable():
    w3 = FakeW3(reserves=(10 * 10**18, 1000 * 10**18, 0), buy_out=10**18, sell_ok=False)
    result = asyncio.run(
        check_new_pool(w3, ROUTER, POOL, WETH, TOKEN, WETH, test_amount_wei=10**16, min_liquidity_weth=10**18)
    )
    assert result.passed is False
    assert result.reason == NO_QUOTE


def test_passes_healthy_pool():
    w3 = FakeW3(reserves=(10 * 10**18, 1000 * 10**18, 0), buy_out=5 * 10**18)
    result = asyncio.run(
        check_new_pool(w3, ROUTER, POOL, WETH, TOKEN, WETH, test_amount_wei=10**16, min_liquidity_weth=10**18)
    )
    assert result.passed is True
    assert result.token == TOKEN
    assert result.quoted_buy_amount == 5 * 10**18


if __name__ == "__main__":
    test_rejects_pair_without_weth()
    test_rejects_thin_liquidity()
    test_rejects_when_sell_quote_unavailable()
    test_passes_healthy_pool()
    print("ok")
