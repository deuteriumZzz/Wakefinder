from wakefinder.common.stuck_position import StuckPositionTracker


def test_not_stuck_below_threshold():
    t = StuckPositionTracker(threshold=3)
    assert t.record_failure("0xAAA") is False
    assert t.record_failure("0xAAA") is False
    assert t.is_stuck("0xaaa") is False


def test_becomes_stuck_exactly_at_threshold():
    t = StuckPositionTracker(threshold=3)
    t.record_failure("0xAAA")
    t.record_failure("0xAAA")
    assert t.record_failure("0xAAA") is True
    assert t.is_stuck("0xaaa") is True


def test_stuck_alert_fires_only_once():
    t = StuckPositionTracker(threshold=2)
    t.record_failure("0xAAA")
    assert t.record_failure("0xAAA") is True
    assert t.record_failure("0xAAA") is False
    assert t.record_failure("0xAAA") is False


def test_token_is_case_insensitive():
    t = StuckPositionTracker(threshold=1)
    t.record_failure("0xAAA")
    assert t.is_stuck("0xaaa") is True


def test_success_resets_failure_count():
    t = StuckPositionTracker(threshold=3)
    t.record_failure("0xAAA")
    t.record_failure("0xAAA")
    t.record_success("0xAAA")
    assert t.record_failure("0xAAA") is False
    assert t.record_failure("0xAAA") is False


def test_success_reports_recovery_only_when_previously_stuck():
    t = StuckPositionTracker(threshold=1)
    assert t.record_success("0xAAA") is False  # никогда не была зависшей
    t.record_failure("0xAAA")
    assert t.record_success("0xAAA") is True  # только что восстановилась
    assert t.record_success("0xAAA") is False  # уже не зависшая — не сигнализируем повторно


def test_independent_tokens():
    t = StuckPositionTracker(threshold=2)
    t.record_failure("0xAAA")
    t.record_failure("0xAAA")
    assert t.is_stuck("0xaaa") is True
    assert t.is_stuck("0xbbb") is False
