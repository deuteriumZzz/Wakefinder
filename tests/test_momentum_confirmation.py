import asyncio

from wakefinder.common.momentum_confirmation import (
    check_eth_pool_momentum,
    check_solana_mint_momentum,
    evaluate_buy_count,
)


def test_passes_when_buy_count_meets_threshold():
    result = evaluate_buy_count(3, 2)
    assert result.passed is True
    assert result.buy_count == 3


def test_passes_at_exact_threshold():
    assert evaluate_buy_count(2, 2).passed is True


def test_fails_below_threshold():
    result = evaluate_buy_count(1, 2)
    assert result.passed is False
    assert "1" in result.reason and "2" in result.reason


def test_zero_buys_fails():
    assert evaluate_buy_count(0, 1).passed is False


class _FakeSwapEvents:
    def __init__(self, logs):
        self._logs = logs

    async def get_logs(self, fromBlock, toBlock):
        return self._logs


class _FakeEvents:
    def __init__(self, logs):
        self.Swap = _FakeSwapEvents(logs)


class _FakePair:
    def __init__(self, logs):
        self.events = _FakeEvents(logs)


class _AwaitableInt:
    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _get():
            return self._value

        return _get().__await__()


class _FakeEth:
    def __init__(self, logs, block_number):
        self._logs = logs
        self.block_number = _AwaitableInt(block_number)

    def contract(self, address, abi):
        return _FakePair(self._logs)


class _FakeW3:
    def __init__(self, logs, block_number=100):
        self.eth = _FakeEth(logs, block_number)


def test_check_eth_pool_momentum_counts_swap_logs():
    w3 = _FakeW3(logs=["swap1", "swap2", "swap3"])
    result = asyncio.run(check_eth_pool_momentum(w3, "0xPOOL", from_block=90, min_buys=2))
    assert result.passed is True
    assert result.buy_count == 3


def test_check_eth_pool_momentum_fails_with_no_swaps():
    w3 = _FakeW3(logs=[])
    result = asyncio.run(check_eth_pool_momentum(w3, "0xPOOL", from_block=90, min_buys=1))
    assert result.passed is False


class _FakeSigsResponse:
    def __init__(self, value):
        self.value = value


class _FakeSolanaClient:
    def __init__(self, sig_count):
        self._sig_count = sig_count

    async def get_signatures_for_address(self, pubkey, limit=50):
        return _FakeSigsResponse(list(range(self._sig_count)))


def test_check_solana_mint_momentum_counts_signatures():
    client = _FakeSolanaClient(sig_count=5)
    result = asyncio.run(check_solana_mint_momentum(client, "So11111111111111111111111111111111111111112", min_buys=3))
    assert result.passed is True
    assert result.buy_count == 5


def test_check_solana_mint_momentum_fails_with_few_signatures():
    client = _FakeSolanaClient(sig_count=1)
    result = asyncio.run(check_solana_mint_momentum(client, "So11111111111111111111111111111111111111112", min_buys=3))
    assert result.passed is False
