import asyncio

from wakefinder.common.social_signal import check_twitter_mentions


def test_no_token_skips_check():
    result = asyncio.run(check_twitter_mentions("0xTOKEN", bearer_token="", min_mentions=3))
    assert result.passed is True
    assert result.mention_count == 0


class _FakeResponse:
    def __init__(self, result_count):
        self._result_count = result_count

    def raise_for_status(self):
        pass

    def json(self):
        return {"meta": {"result_count": self._result_count}}


def test_enough_mentions_passes(monkeypatch):
    import wakefinder.common.social_signal as mod

    monkeypatch.setattr(mod.requests, "get", lambda url, headers, params, timeout: _FakeResponse(10))
    result = asyncio.run(check_twitter_mentions("0xTOKEN", bearer_token="fake-token", min_mentions=3))
    assert result.passed is True
    assert result.mention_count == 10


def test_too_few_mentions_fails(monkeypatch):
    import wakefinder.common.social_signal as mod

    monkeypatch.setattr(mod.requests, "get", lambda url, headers, params, timeout: _FakeResponse(1))
    result = asyncio.run(check_twitter_mentions("0xTOKEN", bearer_token="fake-token", min_mentions=3))
    assert result.passed is False
    assert "1" in result.reason and "3" in result.reason


def test_api_error_fails_open_not_closed(monkeypatch):
    import wakefinder.common.social_signal as mod

    def fake_get(url, headers, params, timeout):
        raise ConnectionError("rate limited")

    monkeypatch.setattr(mod.requests, "get", fake_get)
    result = asyncio.run(check_twitter_mentions("0xTOKEN", bearer_token="fake-token", min_mentions=3))
    assert result.passed is True  # намеренно fail-open, см. docstring модуля
    assert result.mention_count == 0


def test_exact_threshold_passes(monkeypatch):
    import wakefinder.common.social_signal as mod

    monkeypatch.setattr(mod.requests, "get", lambda url, headers, params, timeout: _FakeResponse(3))
    result = asyncio.run(check_twitter_mentions("0xTOKEN", bearer_token="fake-token", min_mentions=3))
    assert result.passed is True
