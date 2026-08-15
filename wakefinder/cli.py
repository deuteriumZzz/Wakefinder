"""Единая точка входа: конфиг-профиль (TOML) -> подходящий run(). Заменяет
захардкоженные launcher-скрипты на каждую комбинацию сеть+стратегия одним
файлом-профилем на "режим торговли" (какие пулы/кошельки/риск-параметры) —
см. README "Конфиг-профили и CLI".

Формат — TOML через stdlib tomllib (Python 3.11+), не YAML/pyyaml: не
добавляем зависимость там, где хватает stdlib.

Риск-параметры профиля ([risk]-секция) применяются через os.environ ДО
первого вызова get_settings() — это lru_cache-singleton НА ПРОЦЕСС
(common/config.py), а не на профиль. Провести Settings-объект параметром
через все 4 run() было бы чище, но потребовало бы менять сигнатуры всех
run() и десятков мест внутри common/*.py, которые сами читают get_settings().
Один процесс = один профиль — то же допущение, что и раньше (раздельные
кошельки/процессы на стратегию, см. docstring chains/*/copytrade.py).
"""

import argparse
import asyncio
import logging
import os
import tomllib

logger = logging.getLogger("wakefinder.cli")

CHAINS = ("eth", "solana")
STRATEGIES = ("arb", "copytrade")

# ключи TOML [risk] -> переменные окружения Settings (common/config.py)
_RISK_ENV_MAP = {
    "max_gas_gwei": "MAX_GAS_GWEI",
    "max_capital_per_bundle_eth": "MAX_CAPITAL_PER_BUNDLE_ETH",
    "max_capital_per_bundle_sol": "MAX_CAPITAL_PER_BUNDLE_SOL",
    "profit_share_bps": "PROFIT_SHARE_BPS",
    "max_consecutive_failures": "MAX_CONSECUTIVE_FAILURES",
    "copytrade_size_pct": "COPYTRADE_SIZE_PCT",
    "copytrade_stop_loss_pct": "COPYTRADE_STOP_LOSS_PCT",
    "copytrade_stop_loss_check_interval_seconds": "COPYTRADE_STOP_LOSS_CHECK_INTERVAL_SECONDS",
    "copytrade_min_consensus_wallets": "COPYTRADE_MIN_CONSENSUS_WALLETS",
    "copytrade_consensus_window_seconds": "COPYTRADE_CONSENSUS_WINDOW_SECONDS",
    "copytrade_max_total_exposure_pct": "COPYTRADE_MAX_TOTAL_EXPOSURE_PCT",
    "min_reference_liquidity_eth": "MIN_REFERENCE_LIQUIDITY_ETH",
    "min_reference_liquidity_sol": "MIN_REFERENCE_LIQUIDITY_SOL",
    "max_drawdown_eth": "MAX_DRAWDOWN_ETH",
    "max_drawdown_sol": "MAX_DRAWDOWN_SOL",
    "drawdown_window_seconds": "DRAWDOWN_WINDOW_SECONDS",
    "drawdown_check_interval_seconds": "DRAWDOWN_CHECK_INTERVAL_SECONDS",
}


def load_profile(path: str) -> dict:
    with open(path, "rb") as f:
        profile = tomllib.load(f)
    if profile.get("chain") not in CHAINS:
        raise ValueError(f"профиль {path}: chain должен быть одним из {CHAINS}")
    if profile.get("strategy") not in STRATEGIES:
        raise ValueError(f"профиль {path}: strategy должен быть одним из {STRATEGIES}")
    return profile


def apply_risk_overrides(profile: dict) -> None:
    for key, value in profile.get("risk", {}).items():
        env_name = _RISK_ENV_MAP.get(key)
        if env_name is None:
            raise ValueError(f"неизвестный risk-параметр в профиле: {key} (см. _RISK_ENV_MAP в cli.py)")
        os.environ[env_name] = str(value)
        logger.info("risk override из профиля: %s=%s", env_name, value)


def _pool_registry_from_profile(profile: dict) -> dict[tuple[str, str], str]:
    """[[pools]] с полями token_in/token_out/pool -> {(token_in, token_out): pool}
    (формат, который ожидает chains/eth/main.py:run())."""
    return {(p["token_in"], p["token_out"]): p["pool"] for p in profile.get("pools", [])}


def _reference_pools_from_profile(profile: dict) -> dict:
    """[[reference_pools]] с полем target_pool + остальными полями as-is ->
    {target_pool: {остальные поля}}. Общий парсер для ETH- и Solana-схем
    reference_pools (разные наборы полей, но обе — "target_pool + словарь")."""
    result = {}
    for entry in profile.get("reference_pools", []):
        entry = dict(entry)
        target = entry.pop("target_pool")
        result[target] = entry
    return result


def _solana_pools_from_profile(profile: dict) -> dict[str, dict[str, str]]:
    """[[solana_pools]] с полем pool_id + остальными полями as-is ->
    {pool_id: {остальные поля}} (формат chains/solana/main.py:run(pools=...))."""
    result = {}
    for entry in profile.get("solana_pools", []):
        entry = dict(entry)
        pool_id = entry.pop("pool_id")
        result[pool_id] = entry
    return result


async def run_profile(path: str) -> None:
    profile = load_profile(path)
    apply_risk_overrides(profile)

    chain = profile["chain"]
    strategy = profile["strategy"]
    watched_wallets = frozenset(profile.get("watched_wallets", []))
    token_allowlist = frozenset(profile.get("token_allowlist", []))
    token_denylist = frozenset(profile.get("token_denylist", []))

    if chain == "eth" and strategy == "arb":
        from wakefinder.chains.eth.main import run as eth_arb_run
        await eth_arb_run(
            pool_registry=_pool_registry_from_profile(profile),
            reference_pools=_reference_pools_from_profile(profile),
            min_amount_in=int(profile.get("min_amount_in", 10**18)),
            watched_wallets=watched_wallets,
            token_allowlist=token_allowlist,
            token_denylist=token_denylist,
        )
    elif chain == "eth" and strategy == "copytrade":
        from wakefinder.chains.eth.copytrade import run as eth_copytrade_run
        await eth_copytrade_run(watched_wallets=watched_wallets, token_allowlist=token_allowlist, token_denylist=token_denylist)
    elif chain == "solana" and strategy == "arb":
        from wakefinder.chains.solana.main import run as solana_arb_run
        await solana_arb_run(
            pools=_solana_pools_from_profile(profile),
            reference_pools=_reference_pools_from_profile(profile),
            min_amount_in=int(profile.get("min_amount_in", 10**9)),
            token_allowlist=token_allowlist,
            token_denylist=token_denylist,
        )
    elif chain == "solana" and strategy == "copytrade":
        from wakefinder.chains.solana.copytrade import run as solana_copytrade_run
        await solana_copytrade_run(watched_wallets=watched_wallets, token_allowlist=token_allowlist, token_denylist=token_denylist)


def main() -> None:
    parser = argparse.ArgumentParser(prog="wakefinder", description="Wakefinder — единый CLI поверх конфиг-профилей")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="запустить процесс по TOML-профилю")
    run_parser.add_argument("profile", help="путь к .toml-профилю (см. configs/)")
    args = parser.parse_args()

    if args.command == "run":
        logging.basicConfig(level=logging.INFO)
        asyncio.run(run_profile(args.profile))


if __name__ == "__main__":
    main()
