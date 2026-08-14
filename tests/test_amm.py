from wakefinder.common.amm import arb_profit, get_amount_out, optimal_arb


def test_get_amount_out_applies_fee():
    # 1000 на входе, пул 1:1 -> на выходе чуть меньше 1000 из-за комиссии 0.3%
    out = get_amount_out(1000, 1_000_000, 1_000_000)
    assert 0 < out < 1000


def test_get_amount_out_empty_reserves_is_zero():
    assert get_amount_out(1000, 0, 1_000_000) == 0
    assert get_amount_out(0, 1_000_000, 1_000_000) == 0


def test_no_arb_when_pools_balanced_identically():
    # одинаковое соотношение в обоих пулах -> нет прибыльного размера сделки
    _, profit = optimal_arb(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert profit == 0


def test_arb_profit_when_buy_pool_is_cheap():
    # в buy-пуле больше `out` относительно `in`, чем в sell-пуле -> покупка там,
    # продажа в sell-пуле — прибыльна
    amount, profit = optimal_arb(
        buy_reserve_in=1_000_000,
        buy_reserve_out=1_200_000,
        sell_reserve_out=1_000_000,
        sell_reserve_in=1_000_000,
    )
    assert amount > 0
    assert profit > 0
    # собственный расчёт optimal_arb совпадает с arb_profit для выбранной суммы
    assert arb_profit(amount, 1_000_000, 1_200_000, 1_000_000, 1_000_000) == profit


def test_gas_cost_can_erase_an_otherwise_profitable_arb():
    # небольшая, реально прибыльная по gross возможность...
    amount, gross_profit = optimal_arb(
        buy_reserve_in=1_000_000,
        buy_reserve_out=1_010_000,
        sell_reserve_out=1_000_000,
        sell_reserve_in=1_000_000,
    )
    assert gross_profit > 0
    # ...не должна выглядеть прибыльной, если газ стоит дороже выгоды
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
