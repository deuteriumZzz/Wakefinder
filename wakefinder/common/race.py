"""Гонка нескольких RPC-провайдеров за первенство в обнаружении pending-tx —
несколько узлов видят мемпул с разной задержкой (разное пиринг-соединение),
настоящие MEV-боты не полагаются на единственного провайдера как на
единую точку отказа/задержки.

Принимает список 0-арных фабрик async-итераторов (тот же паттерн, что и
common/reconnect.py:with_reconnect — обычно `watcher.watch` или уже
`lambda: with_reconnect(watcher.watch)`, если каждому провайдеру ещё нужно
своё автопереподключение), не готовые объекты-watcher'ы напрямую — так гонка
и переподключение компонуются независимо, не смешивая ответственность.

Каждый watcher уже дедуплицирует СВОИ собственные повторные уведомления
(внутренний self._seen), но между независимыми watcher'ами на разных
провайдерах такой дедупликации не было бы — этот модуль добавляет её на
уровне слияния потоков: первый провайдер, который увидел конкретный
tx_hash, "выигрывает" гонку, повторное появление того же tx_hash от
другого провайдера отбрасывается, не обрабатывается дважды."""

import asyncio
from collections.abc import AsyncIterator, Callable

from wakefinder.common.interfaces import TxHashEvent

# ponytail: простой guard от неограниченного роста множества, не LRU —
# для процесса, который периодически перезапускают, полная очистка раз в
# max_seen новых tx_hash приемлема; апгрейд — LRU, если станет узким местом.
DEFAULT_MAX_SEEN = 50_000


async def race_watchers(
    make_iterators: list[Callable[[], AsyncIterator[TxHashEvent]]], max_seen: int = DEFAULT_MAX_SEEN,
) -> AsyncIterator[TxHashEvent]:
    if len(make_iterators) == 1:
        # Один провайдер -> гонка не нужна, отдаём поток как есть без
        # накладных расходов на очередь/дедуп-множество.
        async for swap in make_iterators[0]():
            yield swap
        return

    queue: asyncio.Queue[TxHashEvent] = asyncio.Queue()
    seen: set[str] = set()

    async def _pump(make_iterator: Callable[[], AsyncIterator[TxHashEvent]]) -> None:
        async for swap in make_iterator():
            await queue.put(swap)

    tasks = [asyncio.create_task(_pump(mi)) for mi in make_iterators]
    try:
        while True:
            swap = await queue.get()
            if swap.tx_hash in seen:
                continue  # другой провайдер уже увидел этот же tx первым
            seen.add(swap.tx_hash)
            if len(seen) > max_seen:
                seen.clear()
            yield swap
    finally:
        for t in tasks:
            t.cancel()
