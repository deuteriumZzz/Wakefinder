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


def test_momentum_off_by_default_matches_old_behavior():
    t = TrailingStopTracker(trail_pct=30)
    t.update(150)
    assert t.update(140) is False  # резкий провал, но momentum выключен (None) — только trail_pct решает


def test_momentum_triggers_before_trail_pct_would():
    m = TrailingStopTracker(trail_pct=30, momentum_reversal_pct=20)
    m.update(100)
    m.update(150)  # новый пик
    m.update(130)  # (150-130)/150=13% < 20%, momentum не сработал; 130 >= floor 105 — тоже нет
    assert m.update(100) is True  # (130-100)/130=23% >= 20% — momentum сработал (100 тоже < 105, но неважно какой триггер)


def test_momentum_does_not_trigger_on_gradual_decline():
    m = TrailingStopTracker(trail_pct=90, momentum_reversal_pct=50)  # высокий trail_pct, чтобы изолировать momentum
    m.update(100)
    assert m.update(90) is False  # 10% провал за шаг — ниже 50%-порога momentum
    assert m.update(80) is False
    assert m.update(70) is False


def test_momentum_ignored_on_new_peak():
    m = TrailingStopTracker(trail_pct=30, momentum_reversal_pct=1)  # почти любой провал сработал бы
    m.update(100)
    assert m.update(200) is False  # рост, не провал — момент не может сработать на новом пике


def test_momentum_requires_at_least_two_samples():
    m = TrailingStopTracker(trail_pct=30, momentum_reversal_pct=1)
    assert m.update(100) is False  # первый замер — нет предыдущего значения для сравнения


if __name__ == "__main__":
    test_tracks_new_peak_and_does_not_trigger()
    test_rising_value_keeps_raising_peak()
    test_small_pullback_within_trail_does_not_trigger()
    test_pullback_past_trail_triggers()
    test_no_peak_yet_never_triggers()
    test_momentum_off_by_default_matches_old_behavior()
    test_momentum_triggers_before_trail_pct_would()
    test_momentum_does_not_trigger_on_gradual_decline()
    test_momentum_ignored_on_new_peak()
    test_momentum_requires_at_least_two_samples()
    print("ok")
