"""Обёртка над watch()-подписками с автопереподключением. Долгоживущий процесс
на удалённом WS RPC рано или поздно поймает обрыв соединения (провайдер
рестартует ноду, сетевой блип) — без переподключения одна такая осечка
останавливает бота насовсем, если снаружи нет процесс-супервизора (см.
deploy/systemd/), а даже с супервизором это лишний полный рестарт процесса
вместо просто новой подписки поверх уже созданного клиента.

make_iterator — 0-арность (обычно bound method типа watcher.watch), вызывается
заново при каждом переподключении: это пересоздаёт саму подписку, но
экземпляр watcher'а (и его внутреннее состояние вроде _seen/_last_balance)
переживает разрыв, так что дедупликация не сбрасывается."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

logger = logging.getLogger("wakefinder.reconnect")


async def with_reconnect(make_iterator: Callable[[], AsyncIterator], max_backoff_seconds: float = 60.0) -> AsyncIterator:
    backoff = 1.0
    while True:
        try:
            async for item in make_iterator():
                backoff = 1.0  # соединение живо и отдаёт данные — сброс backoff
                yield item
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("watch() оборвался (%s) — переподключение через %.0fs", type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_seconds)
