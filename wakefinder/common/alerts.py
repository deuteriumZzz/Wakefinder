"""Алерты в Telegram на критичные события (kill switch, стоп-лосс, серия
неудач). Пустые TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID = алерты выключены —
никаких обязательных внешних зависимостей для тех, кто их не настроил.

ponytail: сырой HTTP-запрос к Bot API, а не SDK-библиотека (`requests` уже
транзитивная зависимость через jito_py_rpc) — для одного метода (sendMessage)
целая SDK не нужна.
"""

import logging

import requests

logger = logging.getLogger("wakefinder.alerts")

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_alert(bot_token: str, chat_id: str, message: str) -> None:
    if not bot_token or not chat_id:
        return  # алерты не настроены — тихо ничего не делаем, это не ошибка
    try:
        requests.post(
            TELEGRAM_API_URL.format(token=bot_token),
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception as exc:
        # алерт — вспомогательная функция; сбой отправки не должен ронять бота
        logger.warning("не удалось отправить алерт в Telegram (%s)", type(exc).__name__)
