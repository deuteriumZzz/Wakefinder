import asyncio

from eth_account import Account

from wakefinder.chains.eth.sender import DEFAULT_RELAY_URLS, FlashbotsBundleSender, _AuthedFlashbotProvider
from wakefinder.common.interfaces import Bundle

KEY_A = "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004"


def test_default_relay_creates_one_client():
    signer = Account.from_key(KEY_A)
    sender = FlashbotsBundleSender(rpc_url="https://example/http", signer_account=signer)
    assert len(sender._clients) == len(DEFAULT_RELAY_URLS)


def test_multiple_relays_create_one_client_each():
    signer = Account.from_key(KEY_A)
    relay_urls = ["https://relay-a.example", "https://relay-b.example", "https://relay-c.example"]
    sender = FlashbotsBundleSender(rpc_url="https://example/http", signer_account=signer, relay_urls=relay_urls)
    assert len(sender._clients) == 3


def test_relay_api_keys_shorter_than_urls_leaves_rest_unauthenticated():
    # Не должно упасть/рассинхронизироваться, если ключей меньше, чем relay
    signer = Account.from_key(KEY_A)
    relay_urls = ["https://relay-a.example", "https://relay-b.example"]
    sender = FlashbotsBundleSender(rpc_url="https://example/http", signer_account=signer, relay_urls=relay_urls, relay_api_keys=["only-first-key"])
    assert len(sender._clients) == 2


def test_authed_provider_adds_authorization_header_without_dropping_flashbots_header():
    signer = Account.from_key(KEY_A)
    provider = _AuthedFlashbotProvider(signer, "https://relay.example", "my-secret-key")
    headers = provider.get_request_headers()
    assert headers["Authorization"] == "my-secret-key"
    assert "Content-Type" in headers  # унаследованные заголовки FlashbotProvider не потеряны


def test_dry_run_skips_real_send_but_still_simulates():
    signer = Account.from_key(KEY_A)
    sender = FlashbotsBundleSender(rpc_url="https://example/http", signer_account=signer, dry_run=True)
    calls = {"simulate": 0, "send_bundle": 0}
    sender._simulate_sync = lambda raw_txs, target_block: (calls.__setitem__("simulate", calls["simulate"] + 1) or {"results": []})
    for client in sender._clients:
        client.flashbots = type("F", (), {"send_bundle": lambda *a, **k: calls.__setitem__("send_bundle", calls["send_bundle"] + 1)})()
    result = asyncio.run(sender.send(Bundle(raw_txs=["0xdead"], target_block=1)))
    assert result is True
    assert calls["simulate"] == 1  # симуляция всё ещё происходит — реалистичная проверка
    assert calls["send_bundle"] == 0  # реальная отправка НЕ происходит


def test_dry_run_still_returns_false_on_failed_simulation():
    signer = Account.from_key(KEY_A)
    sender = FlashbotsBundleSender(rpc_url="https://example/http", signer_account=signer, dry_run=True)
    sender._simulate_sync = lambda raw_txs, target_block: {"error": "would revert"}
    result = asyncio.run(sender.send(Bundle(raw_txs=["0xdead"], target_block=1)))
    assert result is False


if __name__ == "__main__":
    test_default_relay_creates_one_client()
    test_multiple_relays_create_one_client_each()
    test_relay_api_keys_shorter_than_urls_leaves_rest_unauthenticated()
    test_authed_provider_adds_authorization_header_without_dropping_flashbots_header()
    test_dry_run_skips_real_send_but_still_simulates()
    test_dry_run_still_returns_false_on_failed_simulation()
    print("ok")
