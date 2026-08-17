from wakefinder.common.univ3_math import (
    liquidity_for_amounts,
    nearest_usable_tick,
    sqrt_price_x96_to_tick,
    tick_to_sqrt_price_x96,
    wide_range_around_tick,
)


def test_tick_sqrt_price_round_trip():
    for tick in (-200_000, -50_000, -1000, 0, 1000, 50_000, 200_000):
        sqrt_price = tick_to_sqrt_price_x96(tick)
        recovered = sqrt_price_x96_to_tick(sqrt_price)
        assert abs(recovered - tick) <= 1


def test_tick_to_sqrt_price_is_monotonically_increasing():
    prices = [tick_to_sqrt_price_x96(t) for t in range(-1000, 1000, 100)]
    assert prices == sorted(prices)


def test_nearest_usable_tick_rounds_correctly():
    assert nearest_usable_tick(103, 60) == 120
    assert nearest_usable_tick(89, 60) == 60
    assert nearest_usable_tick(0, 60) == 0
    assert nearest_usable_tick(-89, 60) == -60


def test_wide_range_around_tick_brackets_current_tick():
    lower, upper = wide_range_around_tick(current_tick=1000, tick_spacing=60, half_width_spacings=100)
    assert lower < 1000 < upper
    assert lower % 60 == 0
    assert upper % 60 == 0


def test_wide_range_never_collapses_to_empty():
    lower, upper = wide_range_around_tick(current_tick=0, tick_spacing=60, half_width_spacings=0)
    assert lower < upper


def test_liquidity_for_amounts_price_in_range_splits_between_tokens():
    lower, upper = wide_range_around_tick(0, 60, 100)
    result = liquidity_for_amounts(current_tick=0, tick_lower=lower, tick_upper=upper, amount0_desired=10**18, amount1_desired=10**18)
    assert result.liquidity > 0
    assert 0 <= result.amount0 <= 10**18
    assert 0 <= result.amount1 <= 10**18


def test_liquidity_for_amounts_price_below_range_uses_only_token0():
    # диапазон целиком ВЫШЕ текущей цены -> вся ликвидность из token0
    result = liquidity_for_amounts(current_tick=0, tick_lower=6000, tick_upper=12000, amount0_desired=10**18, amount1_desired=10**18)
    assert result.amount0 == 10**18
    assert result.amount1 == 0
    assert result.liquidity > 0


def test_liquidity_for_amounts_price_above_range_uses_only_token1():
    # диапазон целиком НИЖЕ текущей цены -> вся ликвидность из token1
    result = liquidity_for_amounts(current_tick=0, tick_lower=-12000, tick_upper=-6000, amount0_desired=10**18, amount1_desired=10**18)
    assert result.amount0 == 0
    assert result.amount1 == 10**18
    assert result.liquidity > 0


def test_liquidity_for_amounts_zero_capital_gives_zero_liquidity():
    lower, upper = wide_range_around_tick(0, 60, 100)
    result = liquidity_for_amounts(current_tick=0, tick_lower=lower, tick_upper=upper, amount0_desired=0, amount1_desired=0)
    assert result.liquidity == 0


if __name__ == "__main__":
    test_tick_sqrt_price_round_trip()
    test_tick_to_sqrt_price_is_monotonically_increasing()
    test_nearest_usable_tick_rounds_correctly()
    test_wide_range_around_tick_brackets_current_tick()
    test_wide_range_never_collapses_to_empty()
    test_liquidity_for_amounts_price_in_range_splits_between_tokens()
    test_liquidity_for_amounts_price_below_range_uses_only_token0()
    test_liquidity_for_amounts_price_above_range_uses_only_token1()
    test_liquidity_for_amounts_zero_capital_gives_zero_liquidity()
    print("ok")
