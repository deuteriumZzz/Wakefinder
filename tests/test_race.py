import asyncio

from wakefinder.common.interfaces import PendingSwap
from wakefinder.common.race import race_watchers


class FakeWatcher:
    def __init__(self, swaps, delay=0.0):
        self._swaps = swaps
        self._delay = delay

    async def watch(self):
        for swap in self._swaps:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield swap


def _swap(tx_hash):
    return PendingSwap(tx_hash=tx_hash, pool_address="0xPOOL", token_in="0xIN", token_out="0xOUT", amount_in=1)


def test_single_watcher_passthrough():
    watcher = FakeWatcher([_swap("0x1"), _swap("0x2")])

    async def collect():
        out = []
        async for swap in race_watchers([watcher.watch]):
            out.append(swap.tx_hash)
            if len(out) == 2:
                break
        return out

    assert asyncio.run(collect()) == ["0x1", "0x2"]


def test_dedupes_same_tx_seen_by_multiple_watchers():
    # оба watcher'а "видят" один и тот же tx (slow — с задержкой) — должен
    # выйти ровно один раз, дубликат от slow должен быть молча отброшен.
    fast = FakeWatcher([_swap("0xSAME")], delay=0.0)
    slow = FakeWatcher([_swap("0xSAME")], delay=0.02)

    async def collect():
        gen = race_watchers([fast.watch, slow.watch])
        first = await anext(gen)
        try:
            second = await asyncio.wait_for(anext(gen), timeout=0.1)
            return first.tx_hash, second.tx_hash
        except (TimeoutError, StopAsyncIteration):
            return first.tx_hash, None

    first, second = asyncio.run(collect())
    assert first == "0xSAME"
    assert second is None  # дубликат от slow не должен был дойти до yield


def test_merges_distinct_swaps_from_multiple_watchers():
    a = FakeWatcher([_swap("0xA")])
    b = FakeWatcher([_swap("0xB")])

    async def collect():
        out = set()
        async for swap in race_watchers([a.watch, b.watch]):
            out.add(swap.tx_hash)
            if len(out) == 2:
                break
        return out

    assert asyncio.run(collect()) == {"0xA", "0xB"}


if __name__ == "__main__":
    test_single_watcher_passthrough()
    test_dedupes_same_tx_seen_by_multiple_watchers()
    test_merges_distinct_swaps_from_multiple_watchers()
    print("ok")
