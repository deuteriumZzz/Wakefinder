"""Опциональный социальный сигнал для снайпинга — мониторинг Twitter/X
упоминаний адреса токена перед входом (ОПЦИОНАЛЬНО, по умолчанию выключено).

ЧЕСТНАЯ ГРАНИЦА (прочитайте перед включением):

1. Twitter API v2 `tweets/search/recent` требует ПЛАТНОГО тарифа (Basic/Pro,
   с 2023) — бесплатный тариф этот endpoint не поддерживает. Внешняя
   стоимость, не входит в проект — не проверено и не может быть проверено
   без вашего реального токена (песочница разработки не имеет доступа к
   Twitter API).
2. Telegram сигнал ЗДЕСЬ НЕ РЕАЛИЗОВАН. Простой Bot API не может искать
   упоминания в произвольном публичном канале — бот должен быть заранее
   ВСТУПИВШИМ администратором/участником КАЖДОГО конкретного канала, что не
   работает для только что созданных токенов с неизвестным заранее каналом.
   Настоящий поиск потребовал бы MTProto-клиента (telethon) с
   пользовательской авторизацией по номеру телефона — принципиально другой,
   более тяжёлый и рискованный по ToS класс зависимости, чем всё остальное
   в этом проекте. Честно отложено, не тихий недосмотр (тот же принцип, что
   у Solscan-заметки в wallet_scanner.py).
3. Упоминания в Twitter — САМЫЙ СЛАБЫЙ и САМЫЙ ЛЕГКО НАКРУЧИВАЕМЫЙ (боты,
   paid shill-кампании) сигнал из всех в проекте — в отличие от on-chain
   сигналов (momentum-подтверждение, репутация деплойера), которые нельзя
   подделать без реальных денег в пуле/истории кошелька. Используйте как
   дополнительный фильтр, не как основной."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger("wakefinder.social_signal")

TWITTER_RECENT_SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"


@dataclass
class SocialSignalResult:
    passed: bool
    mention_count: int
    reason: str = ""


def _search_recent_tweets_sync(query: str, bearer_token: str, window_minutes: int) -> int:
    start_time = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = requests.get(
        TWITTER_RECENT_SEARCH_URL,
        headers={"Authorization": f"Bearer {bearer_token}"},
        params={"query": query, "start_time": start_time, "max_results": "100"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("meta", {}).get("result_count", 0)


async def check_twitter_mentions(query: str, bearer_token: str, min_mentions: int, window_minutes: int = 15) -> SocialSignalResult:
    """query — обычно адрес контракта токена (уникальнее символа: символы
    мемкоинов массово переиспользуются, поиск по ним даёт шум от чужих
    токенов). requests.get — синхронный, оборачиваем в asyncio.to_thread
    (тот же приём, что и в wallet_scanner.py/sender.py).

    Без bearer_token: passed=True (проверка пропущена, мягкое выключение).
    При ОШИБКЕ API (rate limit/сеть): ТОЖЕ passed=True, сознательно НЕ
    fail-closed — в отличие от репутации деплойера (более редкая, более
    надёжная проверка), этот сигнал и так самый слабый в проекте; трактовать
    временный сбой Twitter API как "недостаточно упоминаний" означало бы,
    что случайный rate-limit молча блокирует ВЕСЬ снайпинг."""
    if not bearer_token:
        return SocialSignalResult(passed=True, mention_count=0)
    try:
        count = await asyncio.to_thread(_search_recent_tweets_sync, query, bearer_token, window_minutes)
    except Exception as exc:
        logger.warning("не удалось проверить Twitter-упоминания для %s (%s) — проверка пропущена", query, type(exc).__name__)
        return SocialSignalResult(passed=True, mention_count=0, reason=f"Twitter API недоступен: {type(exc).__name__}")
    if count >= min_mentions:
        return SocialSignalResult(passed=True, mention_count=count)
    return SocialSignalResult(
        passed=False, mention_count=count,
        reason=f"недостаточно упоминаний в Twitter за {window_minutes} мин: {count} < {min_mentions}",
    )


def demo() -> None:
    result = asyncio.run(check_twitter_mentions("0xTOKEN", bearer_token="", min_mentions=3))
    assert result.passed is True  # без токена — пропущено
    print("OK")


if __name__ == "__main__":
    demo()
