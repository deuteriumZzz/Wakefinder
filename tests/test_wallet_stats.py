import json

from wakefinder.common.wallet_stats import compute_wallet_stats


def _write_log(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_missing_file_returns_empty(tmp_path):
    assert compute_wallet_stats(str(tmp_path / "nope.jsonl")) == {}


def test_profitable_wallet(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"strategy": "copytrade_entry", "wallet": "0xA", "expected_profit": 100, "included": True},
        {"strategy": "copytrade_exit", "wallet": "0xA", "expected_profit": 150, "included": True},
    ])
    stats = compute_wallet_stats(str(path))
    assert stats["0xa"].net_pnl_estimate == 50  # -100 + 150
    assert stats["0xa"].entries == 1
    assert stats["0xa"].exits == 1
    assert stats["0xa"].win_rate == 1.0


def test_arb_records_ignored_no_wallet(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"strategy": "arb", "wallet": "", "expected_profit": 1000, "included": True},
    ])
    assert compute_wallet_stats(str(path)) == {}


def test_not_included_records_ignored(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"strategy": "copytrade_entry", "wallet": "0xB", "expected_profit": 100, "included": False},
    ])
    assert compute_wallet_stats(str(path)) == {}


def test_malformed_lines_skipped(tmp_path):
    path = tmp_path / "trades.jsonl"
    with open(path, "w") as f:
        f.write("not json\n")
        f.write(json.dumps({"strategy": "copytrade_entry", "wallet": "0xC", "expected_profit": 10, "included": True}) + "\n")
    stats = compute_wallet_stats(str(path))
    assert "0xc" in stats


if __name__ == "__main__":
    print("run via pytest (uses tmp_path fixture)")
