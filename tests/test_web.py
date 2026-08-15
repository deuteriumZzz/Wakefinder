import json
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


def _client(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("TRADE_LOG_FILE", str(tmp_path / "trades.jsonl"))
    monkeypatch.setenv("COPYTRADE_POSITIONS_FILE", str(tmp_path / "positions.json"))
    monkeypatch.setenv("SOLANA_COPYTRADE_POSITIONS_FILE", str(tmp_path / "positions_solana.json"))
    monkeypatch.setenv("HEARTBEAT_DIR", str(tmp_path))
    monkeypatch.setenv("KILL_SWITCH_FILE", str(tmp_path / "kill"))
    monkeypatch.setattr(web, "fetch_usd_prices", lambda: {})  # без реального сетевого запроса к CoinGecko в тестах
    return TestClient(web.app)


def test_health():
    client = TestClient(web.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_index_renders_with_no_data(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Wakefinder dashboard" in resp.text
    assert "kill switch не активен" in resp.text


def test_index_shows_kill_switch_engaged(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with open(tmp_path / "kill", "w") as f:
        f.write("test")
    resp = client.get("/")
    assert "KILL SWITCH ВКЛЮЧЁН" in resp.text


def test_index_shows_positions_and_metrics(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    with open(tmp_path / "positions.json", "w") as f:
        json.dump({"0xTOKEN123456789": {"entry_amount_in": 1000, "watched_wallet": "0xWHALE123456789"}}, f)

    with open(tmp_path / "trades.jsonl", "w") as f:
        f.write(json.dumps({"chain": "eth", "included": True, "expected_profit": 100, "realized_profit": 90}) + "\n")

    resp = client.get("/")
    assert "0xTOKEN12345" in resp.text  # token[:12], см. _render_positions_table
    assert resp.status_code == 200


if __name__ == "__main__":
    test_health()
    print("run remaining tests via pytest (uses tmp_path/monkeypatch fixtures)")
