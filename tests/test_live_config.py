import os
from types import SimpleNamespace

from wakefinder.live_config import (
    apply_risk_overrides_live,
    load_live_config,
    pool_registry_to_json,
    save_live_config,
    seed_if_missing,
    sync_dict,
    sync_pool_registry,
    sync_set,
)


def test_load_missing_file_returns_defaults(tmp_path):
    result = load_live_config(str(tmp_path / "nope.json"))
    assert result["watched_wallets"] == []
    assert result["risk"] == {}


def test_load_corrupt_file_returns_defaults_not_crash(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    result = load_live_config(str(path))
    assert result["watched_wallets"] == []


def test_save_then_load_roundtrips(tmp_path):
    path = str(tmp_path / "live.json")
    save_live_config(path, {"watched_wallets": ["0xABC"], "token_allowlist": [], "token_denylist": [], "risk": {"copytrade_size_pct": 3.0}})
    result = load_live_config(path)
    assert result["watched_wallets"] == ["0xABC"]
    assert result["risk"]["copytrade_size_pct"] == 3.0


def test_save_is_atomic_no_leftover_tmp_file(tmp_path):
    path = str(tmp_path / "live.json")
    save_live_config(path, {"watched_wallets": [], "token_allowlist": [], "token_denylist": [], "risk": {}})
    assert os.path.exists(path)
    assert not os.path.exists(f"{path}.tmp")


def test_seed_if_missing_writes_initial_values(tmp_path):
    path = str(tmp_path / "live.json")
    seed_if_missing(path, {"0xAAA", "0xBBB"}, {"0xCCC"}, set())
    result = load_live_config(path)
    assert sorted(result["watched_wallets"]) == ["0xAAA", "0xBBB"]
    assert result["token_allowlist"] == ["0xCCC"]


def test_seed_if_missing_does_not_overwrite_existing(tmp_path):
    path = str(tmp_path / "live.json")
    save_live_config(path, {"watched_wallets": ["0xEXISTING"], "token_allowlist": [], "token_denylist": [], "risk": {}})
    seed_if_missing(path, {"0xNEW"}, set(), set())
    result = load_live_config(path)
    assert result["watched_wallets"] == ["0xEXISTING"]


def test_apply_risk_overrides_live_only_applies_whitelisted_keys():
    settings = SimpleNamespace(copytrade_size_pct=2.0, snipe_size_pct=1.0)
    applied = apply_risk_overrides_live(settings, {"copytrade_size_pct": 5.0, "not_a_real_setting": 999})
    assert settings.copytrade_size_pct == 5.0
    assert applied == ["copytrade_size_pct"]
    assert not hasattr(settings, "not_a_real_setting")


def test_apply_risk_overrides_live_ignores_unknown_attribute_even_if_risk_key():
    # RISK_KEYS содержит ключи, актуальные для cli.py в целом — на КОНКРЕТНОМ
    # settings-объекте не все обязаны существовать (snipe_* нерелевантны
    # процессу без снайпинга), hasattr-проверка должна тихо это пропускать.
    settings = SimpleNamespace(copytrade_size_pct=2.0)
    applied = apply_risk_overrides_live(settings, {"snipe_size_pct": 5.0})
    assert applied == []


def test_sync_set_mutates_in_place_and_reports_change():
    target = {"0xold"}
    changed = sync_set(target, ["0xNEW", "0xnew2"])
    assert changed is True
    assert target == {"0xnew", "0xnew2"}  # нормализовано в lowercase


def test_sync_set_reports_no_change_when_identical():
    target = {"0xabc"}
    changed = sync_set(target, ["0xABC"])  # тот же адрес, другой регистр
    assert changed is False
    assert target == {"0xabc"}


def test_sync_set_same_object_identity_preserved():
    """Критично для того, как это используется в chains/*/*.py: watcher
    держит ссылку на ЭТОТ ЖЕ объект, пересоздание сломало бы обновление."""
    target = {"0xold"}
    target_id = id(target)
    sync_set(target, ["0xnew"])
    assert id(target) == target_id


def test_sync_dict_mutates_in_place_and_reports_change():
    target = {"0xOLD": {"pool": "0xREF_OLD"}}
    target_id = id(target)
    changed = sync_dict(target, {"0xNEW": {"pool": "0xREF_NEW"}})
    assert changed is True
    assert target == {"0xNEW": {"pool": "0xREF_NEW"}}
    assert id(target) == target_id


def test_sync_dict_reports_no_change_when_identical():
    target = {"0xPOOL": {"pool": "0xREF"}}
    changed = sync_dict(target, {"0xPOOL": {"pool": "0xREF"}})
    assert changed is False


def test_pool_registry_to_json_converts_tuple_keys():
    registry = {("0xAAA", "0xBBB"): "0xPOOL1", ("0xCCC", "0xDDD"): "0xPOOL2"}
    result = pool_registry_to_json(registry)
    assert {"token0": "0xAAA", "token1": "0xBBB", "pool": "0xPOOL1"} in result
    assert {"token0": "0xCCC", "token1": "0xDDD", "pool": "0xPOOL2"} in result


def test_sync_pool_registry_roundtrips_through_json_shape():
    target = {("0xaaa", "0xbbb"): "0xoldpool"}
    target_id = id(target)
    desired = pool_registry_to_json({("0xCCC", "0xDDD"): "0xNEWPOOL"})
    changed = sync_pool_registry(target, desired)
    assert changed is True
    assert target == {("0xccc", "0xddd"): "0xNEWPOOL"}  # token-пара нормализована в lowercase, адрес пула — нет
    assert id(target) == target_id


def test_sync_pool_registry_reports_no_change_when_identical():
    target = {("0xaaa", "0xbbb"): "0xpool"}
    desired = [{"token0": "0xAAA", "token1": "0xBBB", "pool": "0xpool"}]
    changed = sync_pool_registry(target, desired)
    assert changed is False


def test_seed_if_missing_writes_reference_pools_and_pool_registry(tmp_path):
    path = str(tmp_path / "live.json")
    seed_if_missing(
        path, set(), set(), set(),
        reference_pools={"0xPOOL": {"pool": "0xREF", "router": "0xROUTER"}},
        pool_registry={("0xAAA", "0xBBB"): "0xPOOL"},
    )
    result = load_live_config(path)
    assert result["reference_pools"] == {"0xPOOL": {"pool": "0xREF", "router": "0xROUTER"}}
    assert {"token0": "0xAAA", "token1": "0xBBB", "pool": "0xPOOL"} in result["pool_registry"]


def test_seed_if_missing_defaults_pools_to_empty_when_omitted(tmp_path):
    path = str(tmp_path / "live.json")
    seed_if_missing(path, set(), set(), set())
    result = load_live_config(path)
    assert result["reference_pools"] == {}
    assert result["pool_registry"] == []
    assert result["solana_pools"] == {}


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tmp_path_tests = [
        test_load_missing_file_returns_defaults,
        test_load_corrupt_file_returns_defaults_not_crash,
        test_save_then_load_roundtrips,
        test_save_is_atomic_no_leftover_tmp_file,
        test_seed_if_missing_writes_initial_values,
        test_seed_if_missing_does_not_overwrite_existing,
        test_seed_if_missing_writes_reference_pools_and_pool_registry,
        test_seed_if_missing_defaults_pools_to_empty_when_omitted,
    ]
    for test_fn in tmp_path_tests:
        with tempfile.TemporaryDirectory() as d:
            test_fn(Path(d))
    test_apply_risk_overrides_live_only_applies_whitelisted_keys()
    test_apply_risk_overrides_live_ignores_unknown_attribute_even_if_risk_key()
    test_sync_set_mutates_in_place_and_reports_change()
    test_sync_set_reports_no_change_when_identical()
    test_sync_set_same_object_identity_preserved()
    test_sync_dict_mutates_in_place_and_reports_change()
    test_sync_dict_reports_no_change_when_identical()
    test_pool_registry_to_json_converts_tuple_keys()
    test_sync_pool_registry_roundtrips_through_json_shape()
    test_sync_pool_registry_reports_no_change_when_identical()
    print("ok")
