import asyncio

from wakefinder.common.protected_rpc import send_raw_via_protected_rpc


class _FakeEth:
    def __init__(self):
        self.sent_raw = None

    async def send_raw_transaction(self, raw):
        self.sent_raw = raw
        return b"\xab" * 32


class _FakeAsyncWeb3:
    def __init__(self, provider):
        self.provider = provider
        self.eth = _FakeEth()


def test_sends_raw_transaction_via_given_provider(monkeypatch):
    import wakefinder.common.protected_rpc as mod

    monkeypatch.setattr(mod, "AsyncWeb3", _FakeAsyncWeb3)
    monkeypatch.setattr(mod, "AsyncHTTPProvider", lambda url: url)

    tx_hash = asyncio.run(send_raw_via_protected_rpc("https://rpc.flashbots.net", b"\x01\x02\x03"))

    assert tx_hash == b"\xab" * 32


def test_constructs_provider_with_given_url(monkeypatch):
    import wakefinder.common.protected_rpc as mod

    seen_urls = []

    def fake_http_provider(url):
        seen_urls.append(url)
        return url

    monkeypatch.setattr(mod, "AsyncWeb3", _FakeAsyncWeb3)
    monkeypatch.setattr(mod, "AsyncHTTPProvider", fake_http_provider)

    asyncio.run(send_raw_via_protected_rpc("https://rpc.mevblocker.io", b"\xff"))

    assert seen_urls == ["https://rpc.mevblocker.io"]
