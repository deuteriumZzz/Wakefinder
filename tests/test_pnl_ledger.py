from wakefinder.common.pnl_ledger import read_closed_trades, record_closed_trade


def test_read_closed_trades_missing_file_returns_empty(tmp_path):
    assert read_closed_trades(str(tmp_path / "nope.jsonl")) == []


def test_record_then_read_roundtrips(tmp_path):
    path = str(tmp_path / "pnl.jsonl")
    record_closed_trade(path, "eth", "copytrade", 500, token="0xTOKEN", wallet="0xWHALE", opened_at=1000.0)
    rows = read_closed_trades(path)
    assert len(rows) == 1
    assert rows[0]["chain"] == "eth"
    assert rows[0]["strategy"] == "copytrade"
    assert rows[0]["realized_pnl"] == 500
    assert rows[0]["token"] == "0xTOKEN"
    assert rows[0]["wallet"] == "0xWHALE"
    assert rows[0]["holding_seconds"] > 0


def test_record_without_opened_at_leaves_holding_seconds_none(tmp_path):
    path = str(tmp_path / "pnl.jsonl")
    record_closed_trade(path, "eth", "arb", 12345)
    rows = read_closed_trades(path)
    assert rows[0]["holding_seconds"] is None


def test_read_closed_trades_filters_by_chain(tmp_path):
    path = str(tmp_path / "pnl.jsonl")
    record_closed_trade(path, "eth", "arb", 100)
    record_closed_trade(path, "solana", "arb", -50)
    assert len(read_closed_trades(path, chain="eth")) == 1
    assert len(read_closed_trades(path, chain="solana")) == 1
    assert len(read_closed_trades(path)) == 2


def test_read_closed_trades_respects_limit(tmp_path):
    path = str(tmp_path / "pnl.jsonl")
    for i in range(10):
        record_closed_trade(path, "eth", "arb", i)
    rows = read_closed_trades(path, limit=3)
    assert [r["realized_pnl"] for r in rows] == [7, 8, 9]


def test_read_closed_trades_skips_corrupt_lines(tmp_path):
    path = tmp_path / "pnl.jsonl"
    record_closed_trade(str(path), "eth", "arb", 1)
    with open(path, "a") as f:
        f.write("not valid json\n")
    record_closed_trade(str(path), "eth", "arb", 2)
    rows = read_closed_trades(str(path))
    assert [r["realized_pnl"] for r in rows] == [1, 2]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tests = [
        test_read_closed_trades_missing_file_returns_empty,
        test_record_then_read_roundtrips,
        test_record_without_opened_at_leaves_holding_seconds_none,
        test_read_closed_trades_filters_by_chain,
        test_read_closed_trades_respects_limit,
        test_read_closed_trades_skips_corrupt_lines,
    ]
    for test_fn in tests:
        with tempfile.TemporaryDirectory() as d:
            test_fn(Path(d))
    print("ok")
