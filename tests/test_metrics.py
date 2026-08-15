import json

from wakefinder.common.metrics import compute_chain_metrics


def _write_log(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_no_file_returns_empty():
    assert compute_chain_metrics("nope.jsonl") == {}


def test_fill_rate_and_average_expected(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "included": True, "expected_profit": 100},
        {"chain": "eth", "included": False, "expected_profit": 50},
        {"chain": "eth", "included": True, "expected_profit": 300},
    ])
    metrics = compute_chain_metrics(str(path))
    m = metrics["eth"]
    assert m.total_attempts == 3
    assert m.included == 2
    assert abs(m.fill_rate - 2 / 3) < 1e-9
    assert abs(m.avg_expected_profit - 150) < 1e-9


def test_realized_profit_and_accuracy(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "included": True, "expected_profit": 100, "realized_profit": 90},
        {"chain": "eth", "included": True, "expected_profit": 200, "realized_profit": 220},
        {"chain": "eth", "included": True, "expected_profit": 50},  # без сверки — не считается в accuracy/realized
    ])
    m = compute_chain_metrics(str(path))["eth"]
    assert abs(m.avg_realized_profit - (90 + 220) / 2) < 1e-9
    # accuracy = среднее(90/100, 220/200) = среднее(0.9, 1.1) = 1.0
    assert abs(m.simulation_accuracy - 1.0) < 1e-9


def test_separates_chains(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "included": True, "expected_profit": 100},
        {"chain": "solana", "included": True, "expected_profit": 5},
    ])
    metrics = compute_chain_metrics(str(path))
    assert set(metrics.keys()) == {"eth", "solana"}
    assert metrics["eth"].avg_realized_profit is None


def test_malformed_lines_skipped(tmp_path):
    path = tmp_path / "trades.jsonl"
    with open(path, "w") as f:
        f.write("not json\n")
        f.write(json.dumps({"chain": "eth", "included": True, "expected_profit": 10}) + "\n")
    metrics = compute_chain_metrics(str(path))
    assert metrics["eth"].total_attempts == 1


if __name__ == "__main__":
    test_no_file_returns_empty()
    print("run remaining tests via pytest (uses tmp_path fixture)")
