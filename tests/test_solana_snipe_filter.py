import asyncio

from wakefinder.chains.solana.snipe_filter import NO_ROUTE, THIN_LIQUIDITY, check_mint_tradeable

WSOL = "So11111111111111111111111111111111111111112"
MINT = "TokenMintAddress11111111111111111111111111"


class FakeJupiter:
    def __init__(self, buy_out=None, sell_out=None, buy_raises=False, sell_raises=False):
        self._buy_out = buy_out
        self._sell_out = sell_out
        self._buy_raises = buy_raises
        self._sell_raises = sell_raises

    async def quote(self, input_mint, output_mint, amount, slippage_bps, only_direct_routes):
        if input_mint == WSOL:
            if self._buy_raises:
                raise RuntimeError("no route")
            return {"outAmount": str(self._buy_out)}
        if self._sell_raises:
            raise RuntimeError("no route")
        return {"outAmount": str(self._sell_out)}


def test_rejects_when_no_buy_route():
    jupiter = FakeJupiter(buy_raises=True)
    result = asyncio.run(check_mint_tradeable(jupiter, MINT, WSOL, test_amount_lamports=10**7, min_output_lamports=10**6))
    assert result.passed is False
    assert result.reason == NO_ROUTE


def test_rejects_when_no_sell_route():
    jupiter = FakeJupiter(buy_out=1000, sell_raises=True)
    result = asyncio.run(check_mint_tradeable(jupiter, MINT, WSOL, test_amount_lamports=10**7, min_output_lamports=10**6))
    assert result.passed is False
    assert result.reason == NO_ROUTE


def test_rejects_thin_liquidity():
    jupiter = FakeJupiter(buy_out=1000, sell_out=100)  # sell_out ниже min_output_lamports
    result = asyncio.run(check_mint_tradeable(jupiter, MINT, WSOL, test_amount_lamports=10**7, min_output_lamports=10**6))
    assert result.passed is False
    assert result.reason == THIN_LIQUIDITY


def test_passes_when_both_routes_healthy():
    jupiter = FakeJupiter(buy_out=5000, sell_out=9 * 10**6)
    result = asyncio.run(check_mint_tradeable(jupiter, MINT, WSOL, test_amount_lamports=10**7, min_output_lamports=10**6))
    assert result.passed is True
    assert result.mint == MINT
    assert result.quoted_buy_amount == 5000


if __name__ == "__main__":
    test_rejects_when_no_buy_route()
    test_rejects_when_no_sell_route()
    test_rejects_thin_liquidity()
    test_passes_when_both_routes_healthy()
    print("ok")
