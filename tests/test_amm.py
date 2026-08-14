from mevbot.common.amm import arb_profit, get_amount_out, optimal_arb


def test_get_amount_out_applies_fee():
    # 1000 in, 1:1 pool -> out is slightly less than 1000 due to the 0.3% fee
    out = get_amount_out(1000, 1_000_000, 1_000_000)
    assert 0 < out < 1000


def test_get_amount_out_empty_reserves_is_zero():
    assert get_amount_out(1000, 0, 1_000_000) == 0
    assert get_amount_out(0, 1_000_000, 1_000_000) == 0


def test_no_arb_when_pools_balanced_identically():
    # same ratio on both sides -> no profitable trade size
    _, profit = optimal_arb(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert profit == 0


def test_arb_profit_when_buy_pool_is_cheap():
    # buy pool has more `out` relative to `in` than sell pool -> buying there,
    # selling in the sell pool, is profitable
    amount, profit = optimal_arb(
        buy_reserve_in=1_000_000,
        buy_reserve_out=1_200_000,
        sell_reserve_out=1_000_000,
        sell_reserve_in=1_000_000,
    )
    assert amount > 0
    assert profit > 0
    # optimal_arb's own accounting agrees with arb_profit for the amount it picked
    assert arb_profit(amount, 1_000_000, 1_200_000, 1_000_000, 1_000_000) == profit


def test_gas_cost_can_erase_an_otherwise_profitable_arb():
    # a small, real gross-profitable opportunity...
    amount, gross_profit = optimal_arb(
        buy_reserve_in=1_000_000,
        buy_reserve_out=1_010_000,
        sell_reserve_out=1_000_000,
        sell_reserve_in=1_000_000,
    )
    assert gross_profit > 0
    # ...must not look profitable once gas costs more than the edge
    _, net_profit = optimal_arb(
        buy_reserve_in=1_000_000,
        buy_reserve_out=1_010_000,
        sell_reserve_out=1_000_000,
        sell_reserve_in=1_000_000,
        gas_cost_wei=gross_profit * 10,
    )
    assert net_profit == 0


if __name__ == "__main__":
    test_get_amount_out_applies_fee()
    test_get_amount_out_empty_reserves_is_zero()
    test_no_arb_when_pools_balanced_identically()
    test_arb_profit_when_buy_pool_is_cheap()
    test_gas_cost_can_erase_an_otherwise_profitable_arb()
    print("ok")
