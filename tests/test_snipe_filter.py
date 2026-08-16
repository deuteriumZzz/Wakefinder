import asyncio

from wakefinder.chains.eth.snipe_filter import (
    NO_QUOTE,
    ROUND_TRIP_SIM_FAILED,
    THIN_LIQUIDITY,
    WETH_PATH_UNSUPPORTED,
    check_new_pool,
    check_round_trip_sellable,
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


# --- check_round_trip_sellable: реальные хекс-адреса нужны, потому что
# функция строит транзакции через настоящий Web3().eth.contract() -
# ABI-кодирование проверяет формат адреса, в отличие от check_new_pool выше,
# который работает только с "сырыми" getAmountsOut-строками.
RT_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
RT_TOKEN = "0x1111111111111111111111111111111111111111"


class _RTRouterFunctions:
    def __init__(self, buy_out):
        self._buy_out = buy_out

    def getAmountsOut(self, amount_in, path):
        return _Callable([amount_in, self._buy_out])


class FakeRTW3:
    def __init__(self, buy_out, block_number=100, base_fee=10**9):
        self._router = type("R", (), {"functions": _RTRouterFunctions(buy_out)})()
        self._block = {"baseFeePerGas": base_fee, "number": block_number}

    @property
    def eth(self):
        return self

    def contract(self, address, abi):
        return self._router

    def get_transaction_count(self, address, block_identifier=None):
        return _Awaitable(1)

    def get_block(self, identifier):
        return _Awaitable(self._block)


class FakeSignedTx:
    rawTransaction = b"raw"


class FakeAccount:
    address = "0x000000000000000000000000000000000000dEaD"

    def sign_transaction(self, tx):
        return FakeSignedTx()


class FakeSender:
    def __init__(self, simulation):
        self._simulation = simulation
        self.calls = []

    async def simulate(self, raw_txs, target_block):
        self.calls.append((raw_txs, target_block))
        return self._simulation


def test_round_trip_fails_on_missing_quote():
    class _RaisingRouter:
        def getAmountsOut(self, amount_in, path):
            return _RaisingCallable()

    w3 = FakeRTW3(buy_out=0)
    w3._router = type("R", (), {"functions": _RaisingRouter()})()
    sender = FakeSender({"results": []})
    result = asyncio.run(check_round_trip_sellable(w3, sender, FakeAccount(), RT_ROUTER, WETH, RT_TOKEN, chain_id=1, test_amount_wei=10**16))
    assert result.passed is False
    assert result.reason == NO_QUOTE


def test_round_trip_fails_on_sell_leg_error():
    w3 = FakeRTW3(buy_out=5 * 10**18)
    sender = FakeSender({"results": [{"error": None}, {"error": None}, {"error": "execution reverted"}]})
    result = asyncio.run(check_round_trip_sellable(w3, sender, FakeAccount(), RT_ROUTER, WETH, RT_TOKEN, chain_id=1, test_amount_wei=10**16))
    assert result.passed is False
    assert ROUND_TRIP_SIM_FAILED in result.reason
    assert "sell" in result.reason


def test_round_trip_fails_on_simulation_level_error():
    w3 = FakeRTW3(buy_out=5 * 10**18)
    sender = FakeSender({"error": "simulation failed entirely"})
    result = asyncio.run(check_round_trip_sellable(w3, sender, FakeAccount(), RT_ROUTER, WETH, RT_TOKEN, chain_id=1, test_amount_wei=10**16))
    assert result.passed is False
    assert result.reason == ROUND_TRIP_SIM_FAILED


def test_round_trip_passes_when_all_legs_succeed():
    w3 = FakeRTW3(buy_out=5 * 10**18)
    sender = FakeSender({"results": [{"error": None}, {"error": None}, {"error": None}]})
    result = asyncio.run(check_round_trip_sellable(w3, sender, FakeAccount(), RT_ROUTER, WETH, RT_TOKEN, chain_id=1, test_amount_wei=10**16))
    assert result.passed is True
    assert result.quoted_buy_amount == 5 * 10**18
    # три ноги [buy, approve, sell] с последовательными nonce
    raw_txs, target_block = sender.calls[0]
    assert len(raw_txs) == 3
    assert target_block == 101  # latest["number"] + 1


if __name__ == "__main__":
    test_rejects_pair_without_weth()
    test_rejects_thin_liquidity()
    test_rejects_when_sell_quote_unavailable()
    test_passes_healthy_pool()
    test_round_trip_fails_on_missing_quote()
    test_round_trip_fails_on_sell_leg_error()
    test_round_trip_fails_on_simulation_level_error()
    test_round_trip_passes_when_all_legs_succeed()
    print("ok")
