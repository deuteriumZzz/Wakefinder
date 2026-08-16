from eth_account import Account

from wakefinder.chains.eth.sender import DEFAULT_RELAY_URLS, FlashbotsBundleSender, _AuthedFlashbotProvider

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


if __name__ == "__main__":
    test_default_relay_creates_one_client()
    test_multiple_relays_create_one_client_each()
    test_relay_api_keys_shorter_than_urls_leaves_rest_unauthenticated()
    test_authed_provider_adds_authorization_header_without_dropping_flashbots_header()
    print("ok")
