import json
from types import SimpleNamespace

from wakefinder.common.canary import CanaryController, compute_canary_fraction


def _write_log(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_no_ramp_returns_full_size():
    assert compute_canary_fraction("nope.jsonl", "eth", start_fraction=0.1, ramp_trades=0) == 1.0


def test_no_file_returns_start_fraction(tmp_path):
    path = str(tmp_path / "nope.jsonl")
    assert compute_canary_fraction(path, "eth", start_fraction=0.2, ramp_trades=10) == 0.2


def test_fraction_ramps_linearly_with_included_trades(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [
        {"chain": "eth", "included": True},
        {"chain": "eth", "included": True},
        {"chain": "eth", "included": False},  # не включён — не считается прогрессом
        {"chain": "solana", "included": True},  # другая сеть — не считается
    ])
    # start=0.2, ramp_trades=10, 2 included -> 0.2 + 0.8 * (2/10) = 0.36
    fraction = compute_canary_fraction(str(path), "eth", start_fraction=0.2, ramp_trades=10)
    assert abs(fraction - 0.36) < 1e-9


def test_fraction_caps_at_full_size(tmp_path):
    path = tmp_path / "trades.jsonl"
    _write_log(path, [{"chain": "eth", "included": True} for _ in range(50)])
    assert compute_canary_fraction(str(path), "eth", start_fraction=0.1, ramp_trades=10) == 1.0


def test_controller_scales_from_original_not_from_current(tmp_path):
    path = tmp_path / "trades.jsonl"
    settings = SimpleNamespace(max_capital_per_bundle_eth=1.0, max_capital_per_bundle_sol=5.0, copytrade_size_pct=2.0, snipe_size_pct=1.0)
    controller = CanaryController(settings, start_fraction=0.0, ramp_trades=10)

    _write_log(path, [{"chain": "eth", "included": True}] * 5)
    controller.update(str(path), "eth")
    assert settings.max_capital_per_bundle_eth == 0.5  # 1.0 * (0 + 1*(5/10))

    _write_log(path, [{"chain": "eth", "included": True}] * 10)
    controller.update(str(path), "eth")
    assert settings.max_capital_per_bundle_eth == 1.0  # от ОРИГИНАЛА (1.0), не от уже урезанного 0.5


if __name__ == "__main__":
    test_no_ramp_returns_full_size()
    test_fraction_caps_at_full_size()
    print("run test_*_tmp_path via pytest (uses tmp_path fixture)")
