from wakefinder.common.trailing_stop import TrailingStopTracker


def test_tracks_new_peak_and_does_not_trigger():
    t = TrailingStopTracker(trail_pct=30)
    assert t.update(100) is False
    assert t.peak == 100


def test_rising_value_keeps_raising_peak():
    t = TrailingStopTracker(trail_pct=30)
    t.update(100)
    assert t.update(150) is False
    assert t.peak == 150


def test_small_pullback_within_trail_does_not_trigger():
    t = TrailingStopTracker(trail_pct=30)
    t.update(150)
    assert t.update(120) is False  # 120 >= 150*0.7=105


def test_pullback_past_trail_triggers():
    t = TrailingStopTracker(trail_pct=30)
    t.update(150)
    assert t.update(100) is True  # 100 < 105


def test_no_peak_yet_never_triggers():
    t = TrailingStopTracker(trail_pct=30)
    assert t.update(0) is False


if __name__ == "__main__":
    test_tracks_new_peak_and_does_not_trigger()
    test_rising_value_keeps_raising_peak()
    test_small_pullback_within_trail_does_not_trigger()
    test_pullback_past_trail_triggers()
    test_no_peak_yet_never_triggers()
    print("ok")
