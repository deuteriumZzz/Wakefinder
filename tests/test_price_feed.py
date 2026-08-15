from wakefinder.common import price_feed


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_fetch_usd_prices_success(monkeypatch):
    def fake_get(url, params, timeout):
        assert params["ids"] == "ethereum,solana"
        return _FakeResponse({"ethereum": {"usd": 3000.5}, "solana": {"usd": 150.25}})

    monkeypatch.setattr(price_feed.requests, "get", fake_get)
    prices = price_feed.fetch_usd_prices()
    assert prices == {"eth": 3000.5, "sol": 150.25}


def test_fetch_usd_prices_missing_coin_omitted(monkeypatch):
    def fake_get(url, params, timeout):
        return _FakeResponse({"ethereum": {"usd": 3000.0}})  # solana отсутствует в ответе

    monkeypatch.setattr(price_feed.requests, "get", fake_get)
    prices = price_feed.fetch_usd_prices()
    assert prices == {"eth": 3000.0}
    assert "sol" not in prices


def test_fetch_usd_prices_request_failure_returns_empty(monkeypatch):
    def fake_get(url, params, timeout):
        raise ConnectionError("timeout")

    monkeypatch.setattr(price_feed.requests, "get", fake_get)
    assert price_feed.fetch_usd_prices() == {}


def test_fetch_usd_prices_empty_chains_skips_request(monkeypatch):
    def fake_get(*args, **kwargs):
        raise AssertionError("не должен вызываться для пустого списка сетей")

    monkeypatch.setattr(price_feed.requests, "get", fake_get)
    assert price_feed.fetch_usd_prices(chains=()) == {}


if __name__ == "__main__":
    print("run via pytest (uses monkeypatch fixture)")
