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

Область действия: watched_wallets (обе сети — WalletSwapWatcher на Solana
тоже подхватывает live без реконнекта, см. chains/solana/wallet_watcher.py),
token_allowlist/denylist (обе сети), risk-параметры из того же набора, что
уже поддерживает [risk] в cli.py (RISK_KEYS ниже — тот же список, не
дублирование смысла, просто общий с cli.py набор), и пулы арбитража:
`reference_pools` (обе сети, формат симулятора каждой сети — см. docstring
TwoPoolArbSimulator в chains/{eth,solana}/simulator.py), `pool_registry`
(только ETH — dict[(token0,token1), pool], у Solana такой отдельной сущности
нет) и `solana_pools` (только Solana — конфиг ЦЕЛЕВЫХ пулов для
RaydiumVaultWatcher, формат chains/solana/main.py:run(pools=...); та же
информация частично дублируется в reference_pools[pool_id]["target_*"] —
существующее устройство TOML-профилей, [[solana_pools]] и [[reference_pools]]
— две независимые секции, не эта функция это придумала).

Оба симулятора и ETH UniswapV2Watcher читают свои dict'ы СВЕЖИМИ на каждый
вызов (UniswapV2Watcher._pool_for() — lookup по self.pool_registry на каждую
pending-транзакцию; TwoPoolArbSimulator — self.reference_pools.get() на
каждый simulate()) — значит для НИХ живое обновление сводится к мутации ТОГО
ЖЕ dict'а in place, без пересборки. Только Solana RaydiumVaultWatcher
подписывается на vault-аккаунты (accountSubscribe) и поэтому нуждается в
собственном periodic-sync цикле, как WalletSwapWatcher (см. его docstring) —
sync_dict()/sync_pool_registry() ниже лишь готовят данные, актуализацию
подписок делает сам watcher.
"""

import json
import os

from wakefinder.cli import _RISK_ENV_MAP

RISK_KEYS = frozenset(_RISK_ENV_MAP.keys())

DEFAULT_LIVE_CONFIG: dict[str, object] = {
    "watched_wallets": [],
    "token_allowlist": [],
    "token_denylist": [],
    "risk": {},
    "reference_pools": {},
    "pool_registry": [],
    "solana_pools": {},
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


def seed_if_missing(
    path: str, watched_wallets, token_allowlist, token_denylist,
    reference_pools: dict | None = None, pool_registry: dict | None = None, solana_pools: dict | None = None,
) -> None:
    """Первый запуск профиля — переносим стартовые значения из TOML/CLI в
    live_config.json один раз, дальше файл — единственный источник правды
    (дашборд правит его, бот читает его), профиль больше не участвует.
    reference_pools/pool_registry/solana_pools опциональны — вызывающий код
    передаёт их, только если у него есть смысл (copytrade/snipe их не знают)."""
    if os.path.exists(path):
        return
    save_live_config(path, {
        "watched_wallets": sorted(watched_wallets), "token_allowlist": sorted(token_allowlist),
        "token_denylist": sorted(token_denylist), "risk": {},
        "reference_pools": reference_pools or {},
        "pool_registry": pool_registry_to_json(pool_registry) if pool_registry else [],
        "solana_pools": solana_pools or {},
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


def sync_dict(target: dict, desired: dict) -> bool:
    """Тот же принцип, что sync_set(), для reference_pools (dict[str, dict])
    — мутирует target IN PLACE. Ключи уже строки (адреса пулов), нормализация
    регистра НЕ выполняется здесь (в отличие от sync_set) — адреса пулов
    используются как ключи lookup'а из PendingSwap.pool_address, чей регистр
    задаёт сам watcher (см. simulator.py: swap.pool_address.lower() при
    поиске), а не эта функция."""
    if desired == target:
        return False
    target.clear()
    target.update(desired)
    return True


def pool_registry_to_json(pool_registry: dict) -> list[dict]:
    """dict[(token0, token1), pool_address] -> JSON-совместимый список — JSON
    не поддерживает кортежи как ключи объекта."""
    return [{"token0": t0, "token1": t1, "pool": pool} for (t0, t1), pool in pool_registry.items()]


def sync_pool_registry(target: dict, desired: list) -> bool:
    """Обратное преобразование pool_registry_to_json() + sync_set()-принцип
    (мутация target IN PLACE) — для ETH UniswapV2Watcher.pool_registry."""
    normalized = {(str(e["token0"]).lower(), str(e["token1"]).lower()): str(e["pool"]) for e in desired}
    if normalized == target:
        return False
    target.clear()
    target.update(normalized)
    return True
