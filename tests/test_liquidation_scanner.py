"""Тесты liquidation_scanner.py. ABI-валидность Borrow/getUserAccountData/
getUserReserveData проверяется настоящим Web3()-энкодером (тот же принцип,
что test_liquidation_watcher.py — после критических ABI-багов этой сессии
не доверяем ABI-списку без прогона через реальный контракт-объект); бизнес-
логика (healthFactor-фильтр, перебор debt/collateral) — простыми фейками
поверх уже ДЕКОДИРОВАННЫХ возвратов, тут нечего кодировать/декодировать."""

import asyncio

from web3 import Web3

from wakefinder.chains.eth.aave_abi import AAVE_POOL_ABI, AAVE_POOL_DATA_PROVIDER_ABI
from wakefinder.chains.eth.liquidation_scanner import (
    HEALTH_FACTOR_ONE,
    _find_debt_and_collateral,
    discover_borrowers,
    scan_for_liquidatable,
)

POOL_ADDRESS = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
DATA_PROVIDER_ADDRESS = "0x5555555555555555555555555555555555555555"  # dummy — только для ABI-энкодинга, не реальный Aave-адрес (config.py:aave_pool_data_provider_address по умолчанию 39 hex, невалиден — см. test_config.py)
DEBT_ASSET = "0x1111111111111111111111111111111111111111"
COLLATERAL_ASSET = "0x2222222222222222222222222222222222222222"
USER = "0x3333333333333333333333333333333333333333"

_ENCODER = Web3()


def test_borrow_event_abi_is_valid():
    # реальный контракт-объект должен построить event-фильтр без исключений
    pool = _ENCODER.eth.contract(address=POOL_ADDRESS, abi=AAVE_POOL_ABI)
    assert pool.events.Borrow is not None


def test_get_user_account_data_abi_encodes():
    pool = _ENCODER.eth.contract(address=POOL_ADDRESS, abi=AAVE_POOL_ABI)
    calldata = pool.encode_abi("getUserAccountData", args=[USER])
    assert calldata.startswith("0x")


def test_get_user_reserve_data_abi_encodes():
    dp = _ENCODER.eth.contract(address=Web3.to_checksum_address(DATA_PROVIDER_ADDRESS), abi=AAVE_POOL_DATA_PROVIDER_ABI)
    calldata = dp.encode_abi("getUserReserveData", args=[DEBT_ASSET, USER])
    assert calldata.startswith("0x")


class _Call:
    def __init__(self, value):
        self._value = value

    async def call(self):
        return self._value


class _PoolFunctions:
    def __init__(self, health_factors):
        self._health_factors = health_factors

    def getUserAccountData(self, user):
        hf = self._health_factors[user]
        return _Call((0, 0, 0, 0, 0, hf))  # только healthFactor (индекс 5) важен для сканера


class _FakePool:
    def __init__(self, health_factors):
        self.functions = _PoolFunctions(health_factors)


def _reserve_data(a_token_balance=0, stable_debt=0, variable_debt=0, usage_as_collateral=False):
    return (a_token_balance, stable_debt, variable_debt, 0, 0, 0, 0, 0, usage_as_collateral)


class _DataProviderFunctions:
    def __init__(self, reserves):
        self._reserves = reserves  # {(asset, user): tuple}

    def getUserReserveData(self, asset, user):
        return _Call(self._reserves[(asset, user)])


class _FakeDataProvider:
    def __init__(self, reserves):
        self.functions = _DataProviderFunctions(reserves)


class _FakeEventBorrow:
    def __init__(self, logs):
        self._logs = logs

    async def get_logs(self, fromBlock, toBlock):
        return [log for log in self._logs if fromBlock <= log["blockNumber"] <= toBlock]


class _FakeEvents:
    def __init__(self, logs):
        self.Borrow = _FakeEventBorrow(logs)


class _FakePoolWithEvents:
    def __init__(self, logs):
        self.events = _FakeEvents(logs)


def test_discover_borrowers_dedupes_and_filters_by_block_range():
    logs = [
        {"blockNumber": 100, "args": {"onBehalfOf": USER}},
        {"blockNumber": 101, "args": {"onBehalfOf": USER}},  # тот же заёмщик — дедуп
        {"blockNumber": 200, "args": {"onBehalfOf": "0x4444444444444444444444444444444444444444"}},  # вне диапазона
    ]
    pool = _FakePoolWithEvents(logs)
    result = asyncio.run(discover_borrowers(None, pool, from_block=100, to_block=150))
    assert result == {USER}


def test_find_debt_and_collateral_finds_first_match():
    reserves = {
        (DEBT_ASSET, USER): _reserve_data(variable_debt=1000),
        (COLLATERAL_ASSET, USER): _reserve_data(a_token_balance=5000, usage_as_collateral=True),
    }
    dp = _FakeDataProvider(reserves)
    result = asyncio.run(_find_debt_and_collateral(dp, USER, {DEBT_ASSET}, {COLLATERAL_ASSET}))
    assert result == (DEBT_ASSET, COLLATERAL_ASSET, 500)  # 50% close factor от 1000


def test_find_debt_and_collateral_none_when_no_debt():
    reserves = {(DEBT_ASSET, USER): _reserve_data()}  # нулевой долг
    dp = _FakeDataProvider(reserves)
    result = asyncio.run(_find_debt_and_collateral(dp, USER, {DEBT_ASSET}, {COLLATERAL_ASSET}))
    assert result is None


def test_find_debt_and_collateral_none_when_collateral_not_enabled():
    reserves = {
        (DEBT_ASSET, USER): _reserve_data(variable_debt=1000),
        (COLLATERAL_ASSET, USER): _reserve_data(a_token_balance=5000, usage_as_collateral=False),  # не используется как обеспечение
    }
    dp = _FakeDataProvider(reserves)
    result = asyncio.run(_find_debt_and_collateral(dp, USER, {DEBT_ASSET}, {COLLATERAL_ASSET}))
    assert result is None


def test_scan_for_liquidatable_skips_healthy_users():
    pool = _FakePool(health_factors={USER: 2 * HEALTH_FACTOR_ONE})  # здоров
    dp = _FakeDataProvider({})
    result = asyncio.run(scan_for_liquidatable(pool, dp, {USER}, {DEBT_ASSET}, {COLLATERAL_ASSET}))
    assert result == []


def test_scan_for_liquidatable_finds_unhealthy_user_with_matching_reserves():
    pool = _FakePool(health_factors={USER: HEALTH_FACTOR_ONE // 2})  # 0.5 -> ликвидируемый
    reserves = {
        (DEBT_ASSET, USER): _reserve_data(variable_debt=2000),
        (COLLATERAL_ASSET, USER): _reserve_data(a_token_balance=10_000, usage_as_collateral=True),
    }
    dp = _FakeDataProvider(reserves)
    result = asyncio.run(scan_for_liquidatable(pool, dp, {USER}, {DEBT_ASSET}, {COLLATERAL_ASSET}))
    assert len(result) == 1
    assert result[0].user == USER
    assert result[0].debt_asset == DEBT_ASSET
    assert result[0].collateral_asset == COLLATERAL_ASSET
    assert result[0].debt_to_cover == 1000


if __name__ == "__main__":
    test_borrow_event_abi_is_valid()
    test_get_user_account_data_abi_encodes()
    test_get_user_reserve_data_abi_encodes()
    test_discover_borrowers_dedupes_and_filters_by_block_range()
    test_find_debt_and_collateral_finds_first_match()
    test_find_debt_and_collateral_none_when_no_debt()
    test_find_debt_and_collateral_none_when_collateral_not_enabled()
    test_scan_for_liquidatable_skips_healthy_users()
    test_scan_for_liquidatable_finds_unhealthy_user_with_matching_reserves()
    print("ok")
