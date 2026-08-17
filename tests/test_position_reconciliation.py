from wakefinder.common.position_reconciliation import find_mismatches


def test_no_mismatch_when_balance_matches():
    assert find_mismatches({"TOKEN": 1000}, {"TOKEN": 1000}) == []


def test_no_mismatch_within_tolerance():
    assert find_mismatches({"TOKEN": 1000}, {"TOKEN": 995}) == []  # 0.5% ниже — в пределах допуска 1%


def test_mismatch_when_balance_missing_entirely():
    mismatches = find_mismatches({"TOKEN": 1000}, {})
    assert len(mismatches) == 1
    assert mismatches[0].token == "TOKEN"
    assert mismatches[0].recorded_amount == 1000
    assert mismatches[0].actual_balance == 0


def test_mismatch_when_balance_below_tolerance():
    mismatches = find_mismatches({"TOKEN": 1000}, {"TOKEN": 500}, tolerance_pct=1.0)
    assert len(mismatches) == 1


def test_zero_or_negative_recorded_amount_skipped():
    assert find_mismatches({"TOKEN": 0}, {}) == []


def test_multiple_tokens_only_mismatched_reported():
    mismatches = find_mismatches(
        {"TOKEN_A": 1000, "TOKEN_B": 500},
        {"TOKEN_A": 1000, "TOKEN_B": 0},
    )
    assert [m.token for m in mismatches] == ["TOKEN_B"]
