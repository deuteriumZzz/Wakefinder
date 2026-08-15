import asyncio

from wakefinder.common.reconnect import with_reconnect


def test_reconnects_after_failure_and_keeps_yielding():
    attempts = {"n": 0}

    async def make_iterator():
        attempts["n"] += 1
        if attempts["n"] == 1:
            yield "a"
            raise ConnectionError("dropped")
        yield "b"
        yield "c"

    async def collect():
        out = []
        async for item in with_reconnect(make_iterator, max_backoff_seconds=0.01):
            out.append(item)
            if len(out) == 3:
                break
        return out

    result = asyncio.run(collect())
    assert result == ["a", "b", "c"]
    assert attempts["n"] == 2  # первый вызов оборвался, второй — переподключение


def test_cancelled_error_propagates_not_swallowed():
    async def make_iterator():
        raise asyncio.CancelledError()
        yield  # pragma: no cover - делает функцию генератором

    async def run():
        async for _ in with_reconnect(make_iterator):
            pass

    try:
        asyncio.run(run())
        raised = False
    except asyncio.CancelledError:
        raised = True
    assert raised


if __name__ == "__main__":
    test_reconnects_after_failure_and_keeps_yielding()
    test_cancelled_error_propagates_not_swallowed()
    print("ok")
