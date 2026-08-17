"""Минимальная математика Uniswap V3 concentrated liquidity — только то, что
нужно chains/eth/jit_liquidity.py для КОНСЕРВАТИВНОГО (широкий фиксированный
диапазон, не точный расчёт под конкретный своп) первого прохода JIT-
ликвидности.

ЧЕСТНО: формулы конверсии tick<->sqrtPriceX96 и liquidity-from-amounts — из
документированного публичного whitepaper/исходников Uniswap V3
(TickMath.sol/LiquidityAmounts.sol), реализованы через float (Python `**`/
`log`), а НЕ побитово-точный алгоритм Solidity (TickMath использует бинарный
поиск по фиксированной точке, не floating-point). Для НАШЕГО использования
(off-chain выбор ПРИМЕРНОГО tick-диапазона и ПРИМЕРНЫХ amount0Desired/
amount1Desired перед mint()) точности float достаточно — контракт сам
защищён через amount0Min/amount1Min (slippage-допуск в MintParams), а
итоговые tick'и округляются до ближайшего валидного tickSpacing. НЕ
подходит для сценариев, где нужна точность день-в-день с Solidity."""

import math
from dataclasses import dataclass

Q96 = 2**96


def tick_to_sqrt_price_x96(tick: int) -> int:
    """price = 1.0001^tick (token1 за token0, в сырых, не decimals-скорректированных единицах)."""
    return int((1.0001 ** (tick / 2)) * Q96)


def sqrt_price_x96_to_tick(sqrt_price_x96: int) -> int:
    price = (sqrt_price_x96 / Q96) ** 2
    return math.floor(math.log(price) / math.log(1.0001))


def nearest_usable_tick(tick: int, tick_spacing: int) -> int:
    return round(tick / tick_spacing) * tick_spacing


def wide_range_around_tick(current_tick: int, tick_spacing: int, half_width_spacings: int) -> tuple[int, int]:
    """Широкий диапазон вокруг текущего тика — половина ширины задаётся в
    количестве tick_spacing с каждой стороны (JIT_TICK_RANGE_HALF_WIDTH),
    не точный расчёт под размер конкретного свопа жертвы (см. docstring
    модуля и README "JIT-ликвидность" про этот компромисс)."""
    lower = nearest_usable_tick(current_tick - half_width_spacings * tick_spacing, tick_spacing)
    upper = nearest_usable_tick(current_tick + half_width_spacings * tick_spacing, tick_spacing)
    if lower == upper:
        upper = lower + tick_spacing
    return lower, upper


@dataclass
class LiquidityAmounts:
    liquidity: int
    amount0: int
    amount1: int


def liquidity_for_amounts(
    current_tick: int, tick_lower: int, tick_upper: int, amount0_desired: int, amount1_desired: int,
) -> LiquidityAmounts:
    """Сколько ликвидности L можно получить из желаемых amount0/amount1 в
    заданном диапазоне (LiquidityAmounts.getLiquidityForAmounts) — и сколько
    из них РЕАЛЬНО будет использовано (одна из сторон почти всегда
    оказывается лимитирующей, если текущая цена не строго в центре
    диапазона)."""
    sqrt_p = tick_to_sqrt_price_x96(current_tick) / Q96
    sqrt_pa = tick_to_sqrt_price_x96(tick_lower) / Q96
    sqrt_pb = tick_to_sqrt_price_x96(tick_upper) / Q96
    if sqrt_pa > sqrt_pb:
        sqrt_pa, sqrt_pb = sqrt_pb, sqrt_pa

    if sqrt_p <= sqrt_pa:
        # Цена ниже диапазона — вся ликвидность обеспечивается token0
        liquidity = amount0_desired * (sqrt_pa * sqrt_pb) / (sqrt_pb - sqrt_pa)
        amount0_used, amount1_used = amount0_desired, 0
    elif sqrt_p >= sqrt_pb:
        # Цена выше диапазона — вся ликвидность обеспечивается token1
        liquidity = amount1_desired / (sqrt_pb - sqrt_pa)
        amount0_used, amount1_used = 0, amount1_desired
    else:
        l0 = amount0_desired * (sqrt_p * sqrt_pb) / (sqrt_pb - sqrt_p)
        l1 = amount1_desired / (sqrt_p - sqrt_pa)
        liquidity = min(l0, l1)
        amount0_used = int(liquidity * (sqrt_pb - sqrt_p) / (sqrt_p * sqrt_pb))
        amount1_used = int(liquidity * (sqrt_p - sqrt_pa))

    return LiquidityAmounts(liquidity=int(liquidity), amount0=amount0_used, amount1=amount1_used)


def demo() -> None:
    # Круговой прогон: тик -> sqrtPriceX96 -> тик должен вернуть исходное
    # значение (или очень близкое, floor из-за плавающей точки).
    for tick in (-100_000, -1000, 0, 1000, 100_000):
        sqrt_price = tick_to_sqrt_price_x96(tick)
        recovered = sqrt_price_x96_to_tick(sqrt_price)
        assert abs(recovered - tick) <= 1, (tick, recovered)

    assert nearest_usable_tick(103, 60) == 120
    assert nearest_usable_tick(89, 60) == 60

    lower, upper = wide_range_around_tick(0, 60, half_width_spacings=100)
    assert lower < 0 < upper

    result = liquidity_for_amounts(current_tick=0, tick_lower=lower, tick_upper=upper, amount0_desired=10**18, amount1_desired=10**18)
    assert result.liquidity > 0
    assert 0 <= result.amount0 <= 10**18
    assert 0 <= result.amount1 <= 10**18
    print("OK")


if __name__ == "__main__":
    demo()
