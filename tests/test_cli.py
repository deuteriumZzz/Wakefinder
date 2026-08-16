import os

import pytest

from wakefinder.cli import (
    _pool_registry_from_profile,
    _reference_pools_from_profile,
    _solana_pools_from_profile,
    apply_kill_switch_override,
    apply_risk_overrides,
    load_profile,
)


def _write_toml(path, content):
    with open(path, "w") as f:
        f.write(content)
    return str(path)


def test_load_profile_valid(tmp_path):
    path = _write_toml(tmp_path / "p.toml", 'chain = "eth"\nstrategy = "arb"\n')
    profile = load_profile(path)
    assert profile["chain"] == "eth"
    assert profile["strategy"] == "arb"


def test_load_profile_rejects_unknown_chain(tmp_path):
    path = _write_toml(tmp_path / "p.toml", 'chain = "bsc"\nstrategy = "arb"\n')
    with pytest.raises(ValueError, match="chain"):
        load_profile(path)


def test_load_profile_rejects_unknown_strategy(tmp_path):
    path = _write_toml(tmp_path / "p.toml", 'chain = "eth"\nstrategy = "snipe"\n')
    with pytest.raises(ValueError, match="strategy"):
        load_profile(path)


def test_apply_risk_overrides_sets_env(monkeypatch):
    monkeypatch.delenv("COPYTRADE_SIZE_PCT", raising=False)
    apply_risk_overrides({"risk": {"copytrade_size_pct": 0.5}})
    assert os.environ["COPYTRADE_SIZE_PCT"] == "0.5"


def test_apply_risk_overrides_rejects_unknown_key():
    with pytest.raises(ValueError, match="неизвестный risk-параметр"):
        apply_risk_overrides({"risk": {"not_a_real_setting": 1}})


def test_apply_risk_overrides_empty_profile_is_noop():
    apply_risk_overrides({})  # не должно бросать


def test_apply_kill_switch_override_sets_env(monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_FILE", raising=False)
    apply_kill_switch_override({"kill_switch_file": "/tmp/my-profile.kill"})
    assert os.environ["KILL_SWITCH_FILE"] == "/tmp/my-profile.kill"


def test_apply_kill_switch_override_absent_is_noop(monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_FILE", raising=False)
    apply_kill_switch_override({})
    assert "KILL_SWITCH_FILE" not in os.environ


def test_pool_registry_from_profile():
    profile = {"pools": [{"token_in": "0xA", "token_out": "0xB", "pool": "0xPOOL"}]}
    assert _pool_registry_from_profile(profile) == {("0xA", "0xB"): "0xPOOL"}


def test_pool_registry_from_profile_empty():
    assert _pool_registry_from_profile({}) == {}


def test_reference_pools_from_profile():
    profile = {"reference_pools": [{"target_pool": "0xTARGET", "pool": "0xREF", "router": "0xROUTER"}]}
    result = _reference_pools_from_profile(profile)
    assert result == {"0xTARGET": {"pool": "0xREF", "router": "0xROUTER"}}


def test_solana_pools_from_profile():
    profile = {"solana_pools": [{"pool_id": "P1", "base_vault": "B1", "quote_vault": "Q1", "base_mint": "M1", "quote_mint": "M2"}]}
    result = _solana_pools_from_profile(profile)
    assert result == {"P1": {"base_vault": "B1", "quote_vault": "Q1", "base_mint": "M1", "quote_mint": "M2"}}


if __name__ == "__main__":
    print("run via pytest (uses tmp_path/monkeypatch fixtures)")
