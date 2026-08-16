"""Живой конфиг: JSON-файл, который дашборд/Telegram MiniApp правят, а
торговые процессы периодически перечитывают — тот же принцип, что kill
switch (common/killswitch.py) и CanaryController (common/canary.py):
дашборд и торговые процессы — РАЗНЫЕ ОС-процессы без общей памяти, файл на
диске — единственный работающий канал между ними. "Динамически" здесь
значит "на следующем опросе" (LIVE_CONFIG_CHECK_INTERVAL_SECONDS), не
мгновенно в тот же тик — честное ограничение архитектуры, не баг.

Атомарная запись (temp-файл + os.replace) — единственное место в проекте,
где один процесс регулярно ПИШЕТ файл, который другой процесс регулярно
ЧИТАЕТ с сопоставимой частотой (kill switch/позиции пишутся/читаются
асимметрично реже) — риск словить частично записанный JSON выше, чем
оправдывает "как везде в проекте не атомарно".

Область действия (v1, честно ограничено, не всё из Settings): watched_wallets
(только ETH — см. docstring применения в chains/eth/*.py; на Solana подписка
идёт per-wallet при старте watch(), новый кошелёк подхватится только при
следующем реконнекте, не проактивно), token_allowlist/denylist (обе сети),
и risk-параметры из того же набора, что уже поддерживает [risk] в cli.py
(RISK_KEYS ниже — тот же список, не дублирование смысла, просто общий с
cli.py набор). Пулы/reference_pools арбитража НЕ входят в v1 — они
используются для ПОСТРОЕНИЯ watcher'а/симулятора при старте, живое
изменение потребовало бы их пересборки на лету, это отдельный кусок работы.
"""

import json
import os

from wakefinder.cli import _RISK_ENV_MAP

RISK_KEYS = frozenset(_RISK_ENV_MAP.keys())

DEFAULT_LIVE_CONFIG = {
    "watched_wallets": [],
    "token_allowlist": [],
    "token_denylist": [],
    "risk": {},
}


def load_live_config(path: str) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_LIVE_CONFIG)
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_LIVE_CONFIG)  # битый/недописанный файл — не валим бота, просто без live-переопределений на этом опросе
    return {**DEFAULT_LIVE_CONFIG, **data}


def save_live_config(path: str, config: dict) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, path)  # атомарно на POSIX и Windows — читающий процесс никогда не увидит частичную запись


def seed_if_missing(path: str, watched_wallets, token_allowlist, token_denylist) -> None:
    """Первый запуск профиля — переносим стартовые значения из TOML/CLI в
    live_config.json один раз, дальше файл — единственный источник правды
    (дашборд правит его, бот читает его), профиль больше не участвует."""
    if os.path.exists(path):
        return
    save_live_config(path, {
        "watched_wallets": sorted(watched_wallets), "token_allowlist": sorted(token_allowlist),
        "token_denylist": sorted(token_denylist), "risk": {},
    })


def apply_risk_overrides_live(settings, risk: dict) -> list[str]:
    applied = []
    for key, value in risk.items():
        if key in RISK_KEYS and hasattr(settings, key):
            setattr(settings, key, value)
            applied.append(key)
    return applied


def sync_set(target: set, desired: list) -> bool:
    """Мутирует target IN PLACE (не пересоздаёт объект) — вызывающий код
    (watcher.watched_wallets, локальный token_allowlist/denylist в run())
    держит ссылку на ЭТОТ ЖЕ объект, так что обновление подхватывается без
    пересоздания watcher'а. Возвращает True, если состав реально изменился
    (для логирования, не для логики)."""
    normalized = {str(x).lower() for x in desired}
    if normalized == target:
        return False
    target.clear()
    target.update(normalized)
    return True
