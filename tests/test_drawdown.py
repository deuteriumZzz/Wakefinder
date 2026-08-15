import json

from wakefinder.common.drawdown import check_drawdown, compute_realized_pnl


def _write_log(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_no_file_means_zero_pnl(tmp_path):
    assert compute_realized_pnl(str(tmp_path / "nope.jsonl"), "eth", 3600) == 0


def test_arb_profit_accumulates(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "strategy": "arb", "included": True, "expected_profit": 100, "ts": 1000},
        {"chain": "eth", "strategy": "arb", "included": True, "expected_profit": 50, "ts": 1001},
    ])
    assert compute_realized_pnl(str(path), "eth", 3600, now=1002) == 150


def test_copytrade_entry_is_a_cost_exit_is_a_gain(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "strategy": "copytrade_entry", "included": True, "expected_profit": 100, "ts": 1000},
        {"chain": "eth", "strategy": "copytrade_exit", "included": True, "expected_profit": 60, "ts": 1001},
    ])
    assert compute_realized_pnl(str(path), "eth", 3600, now=1002) == -40


def test_not_included_ignored(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [{"chain": "eth", "strategy": "arb", "included": False, "expected_profit": 1000, "ts": 1000}])
    assert compute_realized_pnl(str(path), "eth", 3600, now=1001) == 0


def test_other_chain_ignored(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [{"chain": "solana", "strategy": "arb", "included": True, "expected_profit": 1000, "ts": 1000}])
    assert compute_realized_pnl(str(path), "eth", 3600, now=1001) == 0


def test_outside_window_ignored(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [{"chain": "eth", "strategy": "arb", "included": True, "expected_profit": 1000, "ts": 1000}])
    assert compute_realized_pnl(str(path), "eth", window_seconds=10, now=2000) == 0


def test_check_drawdown_breach(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "strategy": "copytrade_entry", "included": True, "expected_profit": 1000, "ts": 1000},
    ])
    status = check_drawdown(str(path), "eth", window_seconds=3600, max_loss=500, now=1002)
    assert status.realized_pnl == -1000
    assert status.breached is True


def test_check_drawdown_no_breach(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "strategy": "copytrade_entry", "included": True, "expected_profit": 100, "ts": 1000},
    ])
    status = check_drawdown(str(path), "eth", window_seconds=3600, max_loss=500, now=1002)
    assert status.breached is False


def test_unrealized_pnl_pushes_into_breach(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "strategy": "arb", "included": True, "expected_profit": 100, "ts": 1000},
    ])
    # реализованный PnL сам по себе не пробивает лимит, но открытая позиция
    # сейчас глубоко в минусе — суммарная просадка должна это учитывать.
    status = check_drawdown(str(path), "eth", window_seconds=3600, max_loss=500, now=1002, unrealized_pnl=-700)
    assert status.realized_pnl == 100
    assert status.unrealized_pnl == -700
    assert status.breached is True


def test_unrealized_pnl_defaults_to_zero(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "strategy": "arb", "included": True, "expected_profit": 100, "ts": 1000},
    ])
    status = check_drawdown(str(path), "eth", window_seconds=3600, max_loss=500, now=1002)
    assert status.unrealized_pnl == 0
    assert status.breached is False


if __name__ == "__main__":
    print("run via pytest (uses tmp_path fixture)")
