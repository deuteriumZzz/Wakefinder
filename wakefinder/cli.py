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
STRATEGIES = ("arb", "copytrade", "snipe", "liquidate", "jit")

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
    "snipe_size_pct": "SNIPE_SIZE_PCT",
    "snipe_test_amount_eth": "SNIPE_TEST_AMOUNT_ETH",
    "snipe_min_liquidity_weth": "SNIPE_MIN_LIQUIDITY_WETH",
    "snipe_trailing_stop_pct": "SNIPE_TRAILING_STOP_PCT",
    "snipe_trailing_stop_check_interval_seconds": "SNIPE_TRAILING_STOP_CHECK_INTERVAL_SECONDS",
    "snipe_max_concurrent_positions": "SNIPE_MAX_CONCURRENT_POSITIONS",
    "solana_snipe_test_amount_sol": "SOLANA_SNIPE_TEST_AMOUNT_SOL",
    "solana_snipe_min_liquidity_sol": "SOLANA_SNIPE_MIN_LIQUIDITY_SOL",
    "canary_start_fraction": "CANARY_START_FRACTION",
    "canary_ramp_trades": "CANARY_RAMP_TRADES",
    "liquidation_debt_assets": "LIQUIDATION_DEBT_ASSETS",
    "liquidation_min_profit_usd": "LIQUIDATION_MIN_PROFIT_USD",
    "liquidation_gas_limit": "LIQUIDATION_GAS_LIMIT",
    "jit_pool_address": "JIT_POOL_ADDRESS",
    "jit_pool_token0": "JIT_POOL_TOKEN0",
    "jit_pool_token1": "JIT_POOL_TOKEN1",
    "jit_pool_fee": "JIT_POOL_FEE",
    "jit_tick_range_half_width": "JIT_TICK_RANGE_HALF_WIDTH",
    "jit_slippage_bps": "JIT_SLIPPAGE_BPS",
    "jit_min_swap_amount_in_wei": "JIT_MIN_SWAP_AMOUNT_IN_WEI",
    "jit_capital0_wei": "JIT_CAPITAL0_WEI",
    "jit_capital1_wei": "JIT_CAPITAL1_WEI",
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


def apply_kill_switch_override(profile: dict) -> None:
    """Опционально: свой kill switch на ЭТОТ профиль, отдельно от общего
    аварийного (единый на все 4 процесса по умолчанию, см. common/killswitch.py).
    Если не задано в профиле — поведение как раньше (общий стоп для всех).
    Позволяет выключить именно сегодняшнюю стратегию, не трогая остальные:
    `python -m wakefinder.common.killswitch stop --path <тот же путь>`."""
    if "kill_switch_file" in profile:
        os.environ["KILL_SWITCH_FILE"] = profile["kill_switch_file"]
        logger.info("профильный kill switch: %s", profile["kill_switch_file"])


def apply_live_config_override(profile: dict) -> None:
    """Опционально: свой live_config.json на ЭТОТ профиль (см.
    wakefinder/live_config.py). По умолчанию ОБЩИЙ файл на все процессы —
    то же допущение, что и у kill_switch_file. В отличие от kill switch
    (где "общий на все" — осознанная цель), для watched_wallets/allowlist/
    denylist/risk это обычно НЕ то, что нужно при нескольких одновременно
    запущенных стратегиях — задайте разные live_config_file в профилях,
    если запускаете больше одного профиля одновременно."""
    if "live_config_file" in profile:
        os.environ["LIVE_CONFIG_FILE"] = profile["live_config_file"]
        logger.info("профильный live_config: %s", profile["live_config_file"])


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


async def run_discover(args) -> None:
    """Обёртка над wakefinder/wallet_scanner.py в виде команды: сканирует
    указанные пулы/vault'ы, печатает ранжированных кандидатов и готовый
    TOML-фрагмент для watched_wallets. Не пишет в файл профиля автоматически
    (TOML нет стандартного writer'а в stdlib, а сторонний ради этого — лишняя
    зависимость; ручной copy-paste безопаснее тихой перезаписи чужого файла)."""
    from wakefinder.common.config import get_settings

    settings = get_settings()

    if args.chain == "eth":
        from web3 import AsyncHTTPProvider, AsyncWeb3

        from wakefinder.wallet_scanner import filter_by_etherscan_activity, find_candidate_wallets_eth

        w3 = AsyncWeb3(AsyncHTTPProvider(settings.eth_rpc_http_url.get_secret_value()))
        counts = await find_candidate_wallets_eth(w3, args.pools, args.from_block, args.to_block)
        ranked = counts.most_common()
        if args.etherscan_api_key:
            survivors = set(filter_by_etherscan_activity([a for a, _ in ranked], args.etherscan_api_key))
            ranked = [(a, c) for a, c in ranked if a in survivors]
    else:
        from solana.rpc.async_api import AsyncClient

        from wakefinder.wallet_scanner import find_candidate_wallets_solana

        if settings.solana_rpc_http_url is None:
            raise RuntimeError("SOLANA_RPC_HTTP_URL не задан — discover для Solana не может стартовать без него")
        async with AsyncClient(settings.solana_rpc_http_url.get_secret_value()) as client:
            counts = await find_candidate_wallets_solana(client, args.pools, limit=args.limit)
        ranked = counts.most_common()

    ranked = ranked[: args.top]
    if not ranked:
        print("Кандидатов не найдено — проверьте адреса пулов/диапазон блоков.")
        return

    print(f"Найдено кандидатов: {len(ranked)} (по частоте торговли в указанных пулах)\n")
    for address, count in ranked:
        print(f"  {address}  сделок={count}")

    print("\nВставьте нужные в watched_wallets вашего профиля:")
    print("watched_wallets = [")
    for address, _ in ranked:
        print(f'    "{address}",')
    print("]")


async def run_profile(path: str) -> None:
    profile = load_profile(path)
    apply_risk_overrides(profile)
    apply_kill_switch_override(profile)
    apply_live_config_override(profile)

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
    elif chain == "eth" and strategy == "snipe":
        from wakefinder.chains.eth.snipe import run as eth_snipe_run
        await eth_snipe_run(factory_address=profile.get("factory_address"), token_denylist=token_denylist)
    elif chain == "solana" and strategy == "snipe":
        from wakefinder.chains.solana.snipe import run as solana_snipe_run
        await solana_snipe_run(token_denylist=token_denylist)
    elif chain == "eth" and strategy == "liquidate":
        from wakefinder.chains.eth.liquidate import run as eth_liquidate_run
        await eth_liquidate_run()
    elif chain == "eth" and strategy == "jit":
        from wakefinder.chains.eth.jit_liquidity import run as eth_jit_run
        await eth_jit_run()
    else:
        # chain/strategy сами по себе валидны (см. load_profile), но для ИХ
        # КОМБИНАЦИИ нет run() — например liquidate сейчас только ETH
        # (Aave V3), Solana-эквивалента нет. Явная ошибка вместо тихого
        # "процесс стартовал и ничего не сделал".
        raise ValueError(f"комбинация chain={chain} strategy={strategy} не поддерживается — нет run() для неё в cli.py")


def main() -> None:
    parser = argparse.ArgumentParser(prog="wakefinder", description="Wakefinder — единый CLI поверх конфиг-профилей")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="запустить процесс по TOML-профилю")
    run_parser.add_argument("profile", help="путь к .toml-профилю (см. configs/)")

    discover_parser = subparsers.add_parser("discover", help="найти кандидатов в watched_wallets по активности в указанных пулах")
    discover_parser.add_argument("--chain", choices=CHAINS, required=True)
    discover_parser.add_argument("--pools", nargs="+", required=True, help="ETH: адреса пулов; Solana: адреса vault-аккаунтов")
    discover_parser.add_argument("--from-block", type=int, help="ETH: начало диапазона блоков")
    discover_parser.add_argument("--to-block", type=int, help="ETH: конец диапазона блоков")
    discover_parser.add_argument("--limit", type=int, default=1000, help="Solana: сколько последних подписей на vault сканировать")
    discover_parser.add_argument("--top", type=int, default=20, help="сколько кандидатов показать")
    discover_parser.add_argument("--etherscan-api-key", default="", help="опциональный фильтр по активности кошелька (ETH)")

    verify_parser = subparsers.add_parser("verify-log", help="проверить hash-chain trade_log.jsonl/pnl_ledger.jsonl на признаки подделки задним числом")
    verify_parser.add_argument("path", help="путь к JSONL-файлу (trade_log.jsonl или pnl_ledger.jsonl)")

    args = parser.parse_args()

    if args.command == "run":
        logging.basicConfig(level=logging.INFO)
        asyncio.run(run_profile(args.profile))
    elif args.command == "discover":
        logging.basicConfig(level=logging.WARNING)  # не засорять вывод служебными INFO-логами
        if args.chain == "eth" and (args.from_block is None or args.to_block is None):
            parser.error("discover --chain eth требует --from-block и --to-block")
        asyncio.run(run_discover(args))
    elif args.command == "verify-log":
        from wakefinder.common.hash_chain import verify_chain

        valid, broken_at = verify_chain(args.path)
        if valid:
            print(f"OK: {args.path} — hash-chain цела, признаков подделки задним числом не найдено")
        else:
            print(f"НАРУШЕНА ЦЕПОЧКА: {args.path}, строка {broken_at} — запись отредактирована/удалена/переставлена задним числом")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
