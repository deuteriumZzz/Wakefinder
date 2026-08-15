from wakefinder.common.reconciliation import gas_cost_wei_from_receipt, realized_profit_from_balances


def test_realized_profit_positive():
    assert realized_profit_from_balances(balance_before=1000, balance_after=1150) == 150


def test_realized_profit_negative():
    assert realized_profit_from_balances(balance_before=1000, balance_after=900) == -100


def test_realized_profit_zero():
    assert realized_profit_from_balances(balance_before=1000, balance_after=1000) == 0


def test_gas_cost_from_receipt():
    receipt = {"gasUsed": 150_000, "effectiveGasPrice": 20_000_000_000}
    assert gas_cost_wei_from_receipt(receipt) == 150_000 * 20_000_000_000


def test_gas_cost_from_receipt_missing_fields_defaults_to_zero():
    assert gas_cost_wei_from_receipt({}) == 0


if __name__ == "__main__":
    test_realized_profit_positive()
    test_realized_profit_negative()
    test_realized_profit_zero()
    test_gas_cost_from_receipt()
    test_gas_cost_from_receipt_missing_fields_defaults_to_zero()
    print("ok")
