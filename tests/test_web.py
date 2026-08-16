import os

import pytest

pytest.importorskip("fastapi")

os.environ.setdefault("ETH_RPC_WS_URL", "wss://example/ws")
os.environ.setdefault("ETH_RPC_HTTP_URL", "https://example/http")
os.environ.setdefault("ETH_PRIVATE_KEY", "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")
os.environ.setdefault("FLASHBOTS_SIGNER_KEY", "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99")

from fastapi.testclient import TestClient  # noqa: E402

from wakefinder import web  # noqa: E402
from wakefinder.common.config import get_settings  # noqa: E402

_FAKE_STATE = {
    "kill_switch_engaged": False,
    "heartbeats": [],
    "metrics": {},
    "eth": {"address": "0xABC", "balance": 1.5, "copytrade_positions": [], "snipe_positions": []},
    "solana": {"address": None, "balance": None, "copytrade_positions": []},
    "wallet_stats": [],
    "prices": {},
}


async def _fake_gather_state(settings):
    return dict(_FAKE_STATE)


def _client(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill"))
    # /api/state обычно ходит в реальный RPC через live_state.gather_state — в
    # тестах веб-слоя (auth/роутинг/передача JSON) это не нужно, реальную
    # логику валидации позиций/баланса покрывает tests/test_live_state.py.
    monkeypatch.setattr(web, "gather_state", _fake_gather_state)
    return TestClient(web.app)


def test_health():
    client = TestClient(web.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_index_serves_html_shell(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Wakefinder dashboard" in resp.text
    assert "/api/state" in resp.text  # JS-поллинг ходит именно сюда


def test_api_state_returns_gathered_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["eth"]["balance"] == 1.5
    assert body["kill_switch_engaged"] is False


def test_index_requires_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/")
    assert resp.status_code == 401


def test_api_state_requires_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/state")
    assert resp.status_code == 401


def test_index_rejects_wrong_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/", auth=("admin", "wrong"))
    assert resp.status_code == 401


def test_index_accepts_valid_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/", auth=("admin", "s3cret"))
    assert resp.status_code == 200


if __name__ == "__main__":
    test_health()
    print("run remaining tests via pytest (uses tmp_path/monkeypatch fixtures)")
