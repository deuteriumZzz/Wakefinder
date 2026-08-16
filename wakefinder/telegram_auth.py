"""Верификация Telegram MiniApp `initData` — по алгоритму из официальной
документации (https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app).

Единственный способ доверять тому, что запрос действительно пришёл из
Telegram-клиента конкретного пользователя, а не подделан произвольным HTTP-
клиентом: `initData` подписан HMAC-SHA256 с ключом, производным от токена
бота — подделать подпись без токена невозможно. Проверка подписи здесь НЕ
заменяет собой allowlist конкретного пользователя (см. web.py) — initData
доказывает "этот запрос от Telegram для ЭТОГО пользователя", не "этому
пользователю разрешено управлять ботом"."""

import hashlib
import hmac
import time
from urllib.parse import parse_qsl

MAX_AUTH_AGE_SECONDS = 24 * 3600


def verify_init_data(init_data: str, bot_token: str, max_age_seconds: float = MAX_AUTH_AGE_SECONDS) -> dict | None:
    """Возвращает распарсенные поля initData (включая 'user' как raw JSON-
    строку) при успешной проверке подписи и свежести auth_date, иначе None.
    Не бросает исключения на некорректном вводе — вызывающий код трактует
    None как "не авторизован", тот же принцип, что и HTTP Basic в web.py."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if auth_date is None:
        return None
    try:
        age = time.time() - int(auth_date)
    except ValueError:
        return None
    if age > max_age_seconds or age < -60:  # <0 допуск на небольшой рассинхрон часов, не запрос "из будущего" на часы
        return None

    return pairs
