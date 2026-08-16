from wakefinder.common.price_history import log_snapshot, read_history


def test_read_history_missing_file_returns_empty(tmp_path):
    assert read_history(str(tmp_path / "nope.jsonl"), "0xTOKEN") == []


def test_log_then_read_roundtrips(tmp_path):
    path = str(tmp_path / "history.jsonl")
    log_snapshot(path, "0xTOKEN", 1.5)
    log_snapshot(path, "0xTOKEN", 1.7)
    points = read_history(path, "0xTOKEN")
    assert [p["value"] for p in points] == [1.5, 1.7]


def test_read_history_filters_by_token(tmp_path):
    path = str(tmp_path / "history.jsonl")
    log_snapshot(path, "0xAAA", 1.0)
    log_snapshot(path, "0xBBB", 2.0)
    points = read_history(path, "0xAAA")
    assert len(points) == 1
    assert points[0]["value"] == 1.0


def test_read_history_normalizes_token_case(tmp_path):
    path = str(tmp_path / "history.jsonl")
    log_snapshot(path, "0xABCDEF", 1.0)
    points = read_history(path, "0xabcdef")
    assert len(points) == 1


def test_read_history_respects_limit(tmp_path):
    path = str(tmp_path / "history.jsonl")
    for i in range(10):
        log_snapshot(path, "0xTOKEN", float(i))
    points = read_history(path, "0xTOKEN", limit=3)
    assert [p["value"] for p in points] == [7.0, 8.0, 9.0]


def test_read_history_skips_corrupt_lines(tmp_path):
    path = tmp_path / "history.jsonl"
    log_snapshot(str(path), "0xTOKEN", 1.0)
    with open(path, "a") as f:
        f.write("not valid json\n")
    log_snapshot(str(path), "0xTOKEN", 2.0)
    points = read_history(str(path), "0xTOKEN")
    assert [p["value"] for p in points] == [1.0, 2.0]


if __name__ == "__main__":
    test_read_history_missing_file_returns_empty()
    test_log_then_read_roundtrips()
    test_read_history_filters_by_token()
    test_read_history_normalizes_token_case()
    test_read_history_respects_limit()
    test_read_history_skips_corrupt_lines()
    print("ok")
