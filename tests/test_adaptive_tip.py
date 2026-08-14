import pytest

from wakefinder.common.adaptive_tip import AdaptiveTipController


def test_losing_increases_bps():
    c = AdaptiveTipController(initial_bps=5000, increase_step=500, decrease_step=100)
    c.record_outcome(included=False)
    assert c.current_bps == 5500


def test_winning_decreases_bps():
    c = AdaptiveTipController(initial_bps=5000, increase_step=500, decrease_step=100)
    c.record_outcome(included=True)
    assert c.current_bps == 4900


def test_bounded_by_floor_and_ceiling():
    c = AdaptiveTipController(initial_bps=9800, ceiling_bps=9900, increase_step=500)
    for _ in range(5):
        c.record_outcome(included=False)
    assert c.current_bps == 9900

    c2 = AdaptiveTipController(initial_bps=1100, floor_bps=1000, decrease_step=500)
    for _ in range(5):
        c2.record_outcome(included=True)
    assert c2.current_bps == 1000


def test_rejects_initial_out_of_bounds():
    with pytest.raises(ValueError):
        AdaptiveTipController(initial_bps=500, floor_bps=1000, ceiling_bps=9900)


if __name__ == "__main__":
    test_losing_increases_bps()
    test_winning_decreases_bps()
    test_bounded_by_floor_and_ceiling()
    print("ok")
