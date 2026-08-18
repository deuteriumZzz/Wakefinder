"""Тесты run_liquidation_backtest — фейковые pool/oracle/data_provider,
_estimate_profit переиспользуется НАПРЯМУЮ из liquidate.py (не подделан) —
проверяем интеграцию бэктеста с живой профит-формулой, не переизобретаем
test_liquidate.py."""

import asyncio

from wakefinder.chains.eth.liquidate_backtest import run_liquidation_backtest

POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
COLLATERAL = "0x1111111111111111111111111111111111111111"
OTHER_DEBT_ASSET = "0x2222222222222222222222222222222222222222"
USER = "0x3333333333333333333333333333333333333333"
LIQUIDATOR = "0x4444444444444444444444444444444444444444"


def _liquidation_log(block_number, debt_asset, debt_to_cover):
    return {
        "blockNumber": block_number,
        "args": {
            "collateralAsset": COLLATERAL, "debtAsset": debt_asset, "user": USER,
            "debtToCover": debt_to_cover, "liquidatedCollateralAmount": 5000 * 10**18,
            "liquidator": LIQUIDATOR, "receiveAToken": False,
        },
    }


class _Call:
    def __init__(self, value):
        self._value = value

    async def call(self, block_identifier=None):
        return self._value


class _OracleFunctions:
    def __init__(self, prices):
        self._prices = prices

    def getAssetPrice(self, asset):
        return _Call(self._prices[asset])


class _FakeOracle:
    def __init__(self, prices):
        self.functions = _OracleFunctions(prices)


def _config(decimals, liquidation_bonus_bps):
    return (decimals, 8000, 8500, liquidation_bonus_bps, 1000, True, True, False, True, False)


class _DataProviderFunctions:
    def __init__(self, configs):
        self._configs = configs

    def getReserveConfigurationData(self, asset):
        return _Call(self._configs[asset])


class _FakeDataProvider:
    def __init__(self, configs):
        self.functions = _DataProviderFunctions(configs)


class _FakeEventLiquidationCall:
    def __init__(self, logs):
        self._logs = logs

    async def get_logs(self, fromBlock, toBlock):
        return [log for log in self._logs if fromBlock <= log["blockNumber"] <= toBlock]


class _FakeEvents:
    def __init__(self, logs):
        self.LiquidationCall = _FakeEventLiquidationCall(logs)


class _FakePool:
    def __init__(self, logs):
        self.events = _FakeEvents(logs)


class _FakeEth:
    def __init__(self, pool, block_data):
        self._pool = pool
        self._block_data = block_data

    def contract(self, address, abi):
        return self._pool

    async def get_block(self, block_number):
        return self._block_data


class _FakeW3:
    def __init__(self, pool, block_data=None):
        self.eth = _FakeEth(pool, block_data or {"baseFeePerGas": 20 * 10**9})


def test_scans_and_filters_by_debt_asset():
    logs = [
        _liquidation_log(100, USDC, 1000 * 10**6),
        _liquidation_log(101, OTHER_DEBT_ASSET, 1000 * 10**6),  # не наш debt-актив
    ]
    pool = _FakePool(logs)
    w3 = _FakeW3(pool)
    oracle = _FakeOracle({USDC: 1 * 10**8, WETH: 3000 * 10**8})
    dp = _FakeDataProvider({USDC: _config(6, 10500), COLLATERAL: _config(18, 10500)})

    result = asyncio.run(run_liquidation_backtest(
        w3, POOL, oracle, dp, WETH, debt_assets={USDC.lower()}, min_profit_usd=5.0,
        gas_limit=400_000, from_block=100, to_block=101,
    ))

    assert result.events_scanned == 2
    assert result.matching_debt_asset == 1
    assert result.profitable_count == 1  # $1000 * 5% - газ ~= $26 > $5 порога


def test_below_min_profit_not_counted():
    logs = [_liquidation_log(100, USDC, 10 * 10**6)]  # мелкая сумма
    pool = _FakePool(logs)
    w3 = _FakeW3(pool)
    oracle = _FakeOracle({USDC: 1 * 10**8, WETH: 3000 * 10**8})
    dp = _FakeDataProvider({USDC: _config(6, 10500), COLLATERAL: _config(18, 10500)})

    result = asyncio.run(run_liquidation_backtest(
        w3, POOL, oracle, dp, WETH, debt_assets={USDC.lower()}, min_profit_usd=5.0,
        gas_limit=400_000, from_block=100, to_block=100,
    ))

    assert result.matching_debt_asset == 1
    assert result.profitable_count == 0


def test_no_matching_debt_assets_scans_but_finds_nothing():
    logs = [_liquidation_log(100, OTHER_DEBT_ASSET, 1000 * 10**6)]
    pool = _FakePool(logs)
    w3 = _FakeW3(pool)
    oracle = _FakeOracle({})
    dp = _FakeDataProvider({})

    result = asyncio.run(run_liquidation_backtest(
        w3, POOL, oracle, dp, WETH, debt_assets={USDC.lower()}, min_profit_usd=5.0,
        gas_limit=400_000, from_block=100, to_block=100,
    ))

    assert result.events_scanned == 1
    assert result.matching_debt_asset == 0
    assert result.profitable_count == 0


if __name__ == "__main__":
    test_scans_and_filters_by_debt_asset()
    test_below_min_profit_not_counted()
    test_no_matching_debt_assets_scans_but_finds_nothing()
    print("ok")
