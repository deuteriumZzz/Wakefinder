import asyncio

from wakefinder.chains.eth.liquidate import _estimate_profit

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
COLLATERAL = "0x1111111111111111111111111111111111111111"


class _Call:
    def __init__(self, value):
        self._value = value

    async def call(self):
        return self._value


class _Functions:
    def __init__(self, prices, configs):
        self._prices = prices
        self._configs = configs

    def getAssetPrice(self, asset):
        return _Call(self._prices[asset])

    def getReserveConfigurationData(self, asset):
        return _Call(self._configs[asset])


class _FakeContract:
    def __init__(self, prices=None, configs=None):
        self.functions = _Functions(prices or {}, configs or {})


def _config(decimals, liquidation_bonus_bps):
    # (decimals, ltv, liquidationThreshold, liquidationBonus, reserveFactor, ...)
    return (decimals, 8000, 8500, liquidation_bonus_bps, 1000, True, True, False, True, False)


def test_profitable_liquidation_positive_profit():
    # debt: 1000 USDC (6 decimals) @ $1, collateral bonus 5% (10500 bps)
    oracle = _FakeContract(prices={USDC: 1 * 10**8, WETH: 3000 * 10**8})
    data_provider = _FakeContract(configs={USDC: _config(6, 10500), COLLATERAL: _config(18, 10500)})

    estimate = asyncio.run(_estimate_profit(
        oracle, data_provider, WETH, USDC, COLLATERAL,
        debt_to_cover=1000 * 10**6, gas_price=20 * 10**9, gas_limit=400_000,
    ))

    # gross = $1000 * 5% = $50; gas = 20e9 * 400000 / 1e18 * 3000 = $24
    assert 20 < estimate.profit_usd < 30
    assert estimate.eth_price_usd_e8 == 3000 * 10**8


def test_unprofitable_liquidation_negative_profit():
    # tiny debt amount -> bonus revenue smaller than gas cost
    oracle = _FakeContract(prices={USDC: 1 * 10**8, WETH: 3000 * 10**8})
    data_provider = _FakeContract(configs={USDC: _config(6, 10500), COLLATERAL: _config(18, 10500)})

    estimate = asyncio.run(_estimate_profit(
        oracle, data_provider, WETH, USDC, COLLATERAL,
        debt_to_cover=10 * 10**6, gas_price=20 * 10**9, gas_limit=400_000,
    ))

    # gross = $10 * 5% = $0.50; gas ~ $24 -> negative
    assert estimate.profit_usd < 0


def test_no_bonus_means_no_profit():
    oracle = _FakeContract(prices={USDC: 1 * 10**8, WETH: 3000 * 10**8})
    data_provider = _FakeContract(configs={USDC: _config(6, 10_000), COLLATERAL: _config(18, 10_000)})  # 0% bonus

    estimate = asyncio.run(_estimate_profit(
        oracle, data_provider, WETH, USDC, COLLATERAL,
        debt_to_cover=1000 * 10**6, gas_price=20 * 10**9, gas_limit=400_000,
    ))

    # gross = 0, only gas cost as negative profit
    assert estimate.profit_usd < 0


if __name__ == "__main__":
    test_profitable_liquidation_positive_profit()
    test_unprofitable_liquidation_negative_profit()
    test_no_bonus_means_no_profit()
    print("ok")
