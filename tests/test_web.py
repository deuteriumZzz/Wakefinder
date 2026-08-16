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
    monkeypatch.setenv("LIVE_CONFIG_FILE", str(tmp_path / "live_config.json"))
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


def test_telegram_page_serves_without_auth(tmp_path, monkeypatch):
    # Сама HTML-страница не несёт данных — защищены только /api/telegram/*
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/telegram")
    assert resp.status_code == 200
    assert "telegram-web-app.js" in resp.text


def test_api_telegram_state_rejects_when_not_configured(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)  # TELEGRAM_ALLOWED_USER_ID не задан
    resp = client.get("/api/telegram/state")
    assert resp.status_code == 403


def _sign_init_data(fields: dict, bot_token: str) -> str:
    import hashlib
    import hmac
    from urllib.parse import urlencode

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": computed_hash})


def test_api_telegram_state_accepts_valid_allowed_user(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")
    client = _client(tmp_path, monkeypatch)

    init_data = _sign_init_data({"user": '{"id":12345,"first_name":"Owner"}', "auth_date": str(int(time.time()))}, "fake-bot-token")
    resp = client.get("/api/telegram/state", headers={"X-Telegram-Init-Data": init_data})
    assert resp.status_code == 200
    assert resp.json()["eth"]["balance"] == 1.5


def test_api_telegram_state_rejects_non_allowed_user(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")
    client = _client(tmp_path, monkeypatch)

    init_data = _sign_init_data({"user": '{"id":99999,"first_name":"Stranger"}', "auth_date": str(int(time.time()))}, "fake-bot-token")
    resp = client.get("/api/telegram/state", headers={"X-Telegram-Init-Data": init_data})
    assert resp.status_code == 403


def test_api_telegram_killswitch_toggles(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")
    client = _client(tmp_path, monkeypatch)
    init_data = _sign_init_data({"user": '{"id":12345}', "auth_date": str(int(time.time()))}, "fake-bot-token")
    headers = {"X-Telegram-Init-Data": init_data, "Content-Type": "application/json"}

    resp = client.post("/api/telegram/killswitch", headers=headers, json={"action": "engage"})
    assert resp.status_code == 200
    assert resp.json()["kill_switch_engaged"] is True

    resp = client.post("/api/telegram/killswitch", headers=headers, json={"action": "disengage"})
    assert resp.status_code == 200
    assert resp.json()["kill_switch_engaged"] is False


def test_api_config_get_returns_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/config", auth=("admin", "s3cret"))
    assert resp.status_code == 200
    assert resp.json()["watched_wallets"] == []


def test_api_config_post_then_get_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _client(tmp_path, monkeypatch)
    payload = {"watched_wallets": ["0xAAA"], "token_allowlist": [], "token_denylist": [], "risk": {"copytrade_size_pct": 4.0}}

    resp = client.post("/api/config", auth=("admin", "s3cret"), json=payload)
    assert resp.status_code == 200

    resp = client.get("/api/config", auth=("admin", "s3cret"))
    assert resp.json()["watched_wallets"] == ["0xAAA"]
    assert resp.json()["risk"]["copytrade_size_pct"] == 4.0


def test_api_config_post_rejects_malformed_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _client(tmp_path, monkeypatch)
    resp = client.post("/api/config", auth=("admin", "s3cret"), json={"risk": {"copytrade_size_pct": "not-a-number"}})
    assert resp.status_code == 422


def test_api_config_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/config")
    assert resp.status_code == 401


def test_api_telegram_config_roundtrips(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-bot-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")
    client = _client(tmp_path, monkeypatch)
    init_data = _sign_init_data({"user": '{"id":12345}', "auth_date": str(int(time.time()))}, "fake-bot-token")
    headers = {"X-Telegram-Init-Data": init_data, "Content-Type": "application/json"}

    resp = client.post("/api/telegram/config", headers=headers, json={"watched_wallets": ["0xBBB"], "token_allowlist": [], "token_denylist": [], "risk": {}})
    assert resp.status_code == 200

    resp = client.get("/api/telegram/config", headers=headers)
    assert resp.json()["watched_wallets"] == ["0xBBB"]


if __name__ == "__main__":
    test_health()
    print("run remaining tests via pytest (uses tmp_path/monkeypatch fixtures)")
