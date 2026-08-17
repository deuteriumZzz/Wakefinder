"""Живое состояние для веб-дашборда — единственное место в presentation-слое,
которое реально обращается к RPC (в отличие от остального wakefinder/web.py,
который был чисто файловым: баланс кошелька и текущая оценка открытых позиций
не были доступны нигде, кроме как через торговые процессы напрямую).
Вынесено в отдельный модуль — тестируется Fake-клиентами без поднятия FastAPI,
тот же принцип, что и остальная RPC-логика в проекте.

Оценка позиции — котировка `getAmountsOut`/Jupiter `quote()` ПРЯМО СЕЙЧАС, не
исполненная сделка — тот же честный компромисс, что и everywhere в проекте:
показывает то, что можно было бы получить при выходе в этот момент, не
гарантию. Суммы переводятся в ETH/SOL (float) на СЕРВЕРНОЙ стороне перед
JSON-сериализацией: JS-числа теряют точность выше 2^53, а wei/lamports легко
её превышают — для отображения этого достаточно, дальше не считаем."""

import json
import os
import time

from web3 import AsyncHTTPProvider, AsyncWeb3

from wakefinder.chains.eth.abi import ROUTER_ABI
from wakefinder.common import heartbeat, killswitch
from wakefinder.common.metrics import compute_chain_metrics
from wakefinder.common.price_feed import fetch_usd_prices
from wakefinder.common.price_history import log_snapshot
from wakefinder.common.wallet_stats import compute_wallet_stats

HEARTBEAT_STALE_SECONDS = 90
HEARTBEAT_FILES = {
    "eth_arb": "eth_arb.heartbeat",
    "eth_copytrade": "eth_copytrade.heartbeat",
    "eth_snipe": "eth_snipe.heartbeat",
    "solana_arb": "solana_arb.heartbeat",
    "solana_copytrade": "solana_copytrade.heartbeat",
}


def _load_positions(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _pnl_pct(entry: int, current: int | None) -> float | None:
    if current is None or not entry:
        return None
    return (current - entry) / entry * 100


def _positions_without_live_value(raw: dict, decimals: int, wei_field: str = "entry_amount_in", extra_fields: tuple = ()) -> list[dict]:
    """Fallback-вид позиций, когда живая RPC-оценка недоступна/упала — то,
    что можно показать из ФАЙЛА без единого сетевого запроса (тот же уровень
    данных, что был у дашборда до этого модуля)."""
    out = []
    for token, pos in raw.items():
        row = {"token": token, "entry_amount_in": pos.get(wei_field, 0) / decimals, "current_value": None, "pnl_pct": None}
        for field in extra_fields:
            row[field] = pos.get(field, "")
        out.append(row)
    return out


async def eth_copytrade_positions_live(w3: AsyncWeb3, router_address: str, positions: dict, decimals: int = 10**18, history_path: str | None = None) -> list[dict]:
    router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
    out = []
    for token, pos in positions.items():
        entry = pos.get("entry_amount_in", 0)
        amount_held = pos.get("amount_held", 0)
        token_in = pos.get("token_in")
        current = None
        if amount_held and token_in:
            try:
                quote = await router.functions.getAmountsOut(amount_held, [token, token_in]).call()
                current = quote[-1]
            except Exception:
                pass  # ликвидность высохла/rug — не можем оценить, показываем как "нет данных", не нулём
        current_scaled = current / decimals if current is not None else None
        if history_path and current_scaled is not None:
            log_snapshot(history_path, token, current_scaled)
        out.append({
            "token": token, "entry_amount_in": entry / decimals, "current_value": current_scaled,
            "pnl_pct": _pnl_pct(entry, current), "watched_wallet": pos.get("watched_wallet", ""),
        })
    return out


async def eth_snipe_positions_live(w3: AsyncWeb3, router_address: str, weth_address: str, positions: dict, decimals: int = 10**18, history_path: str | None = None) -> list[dict]:
    router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
    out = []
    for token, pos in positions.items():
        entry = pos.get("entry_amount_in_wei", 0)
        amount_held = pos.get("amount_held", 0)
        current = None
        if amount_held:
            try:
                quote = await router.functions.getAmountsOut(amount_held, [token, weth_address]).call()
                current = quote[-1]
            except Exception:
                pass
        current_scaled = current / decimals if current is not None else None
        if history_path and current_scaled is not None:
            log_snapshot(history_path, token, current_scaled)
        out.append({
            "token": token, "entry_amount_in": entry / decimals, "current_value": current_scaled,
            "pnl_pct": _pnl_pct(entry, current),
        })
    return out


async def solana_copytrade_positions_live(jupiter, positions: dict, decimals: int = 10**9, history_path: str | None = None) -> list[dict]:
    out = []
    for token, pos in positions.items():
        entry = pos.get("entry_amount_in", 0)
        amount_held = pos.get("amount_held", 0)
        token_in = pos.get("token_in")
        current = None
        if amount_held and token_in:
            try:
                quote = await jupiter.quote(input_mint=token, output_mint=token_in, amount=amount_held, slippage_bps=100, only_direct_routes=True)
                current = int(quote["outAmount"])
            except Exception:
                pass
        current_scaled = current / decimals if current is not None else None
        if history_path and current_scaled is not None:
            log_snapshot(history_path, token, current_scaled)
        out.append({
            "token": token, "entry_amount_in": entry / decimals, "current_value": current_scaled,
            "pnl_pct": _pnl_pct(entry, current), "watched_wallet": pos.get("watched_wallet", ""),
        })
    return out


def heartbeats_view(heartbeat_dir: str) -> list[dict]:
    now = time.time()
    out = []
    for label, filename in HEARTBEAT_FILES.items():
        beat = heartbeat.last_beat(os.path.join(heartbeat_dir, filename))
        age = (now - beat) if beat is not None else None
        stale = age is None or age > HEARTBEAT_STALE_SECONDS
        out.append({"process": label, "age_seconds": age, "stale": stale})
    return out


def metrics_view(trade_log_file: str) -> dict:
    metrics = compute_chain_metrics(trade_log_file)
    return {
        chain: {
            "total_attempts": m.total_attempts, "included": m.included, "fill_rate": m.fill_rate,
            "avg_expected_profit": m.avg_expected_profit, "avg_realized_profit": m.avg_realized_profit,
            "simulation_accuracy": m.simulation_accuracy,
        }
        for chain, m in metrics.items()
    }


def wallet_stats_view(trade_log_file: str, prices: dict) -> list[dict]:
    stats = compute_wallet_stats(trade_log_file)
    decimals = {"eth": 10**18, "sol": 10**9}
    out = []
    for s in sorted(stats.values(), key=lambda s: s.net_pnl_estimate, reverse=True):
        d = decimals.get(s.chain)
        price = prices.get(s.chain)
        usd = s.net_pnl_estimate / d * price if d and price else None
        out.append({
            "wallet": s.wallet, "chain": s.chain, "entries": s.entries, "exits": s.exits,
            "net_pnl_estimate": s.net_pnl_estimate / d if d else s.net_pnl_estimate, "net_pnl_usd": usd, "win_rate": s.win_rate,
        })
    return out


async def gather_state(settings) -> dict:
    """Единая точка сбора всего, что показывает дашборд — вызывается и
    начальным рендером страницы, и periodic /api/state опросом с фронта,
    чтобы не было двух разных путей формирования одних и тех же данных."""
    state: dict = {
        "kill_switch_engaged": killswitch.is_engaged(settings.kill_switch_file),
        "heartbeats": heartbeats_view(settings.heartbeat_dir),
        "metrics": metrics_view(settings.trade_log_file),
    }

    # Загрузка позиций из файла НЕ зависит от RPC и не должна падать вместе с
    # ним — иначе сбой RPC (баланс или оценка позиции) скрывал бы вообще все
    # данные по позициям, хотя entry_amount_in из файла всё ещё доступен.
    # w3, один раз сконструированный, переиспользуется для live-оценки даже
    # если сам запрос баланса упал — это два независимых RPC-вызова, не одна
    # операция.
    eth_copytrade_raw = _load_positions(settings.copytrade_positions_file)
    eth_snipe_raw = _load_positions(settings.snipe_positions_file)

    eth_address = None
    eth_balance = None
    w3 = None
    try:
        from eth_account import Account
        eth_address = Account.from_key(settings.resolved_eth_private_key()).address
        w3 = AsyncWeb3(AsyncHTTPProvider(settings.eth_rpc_http_url.get_secret_value()))
        eth_balance = (await w3.eth.get_balance(eth_address)) / 10**18
    except Exception as exc:
        state["eth_error"] = f"{type(exc).__name__}: не удалось получить живые ETH-данные"

    try:
        eth_copytrade_positions = (
            await eth_copytrade_positions_live(w3, settings.eth_router_address, eth_copytrade_raw, history_path=settings.price_history_file) if w3 is not None
            else _positions_without_live_value(eth_copytrade_raw, 10**18, extra_fields=("watched_wallet",))
        )
    except Exception:
        eth_copytrade_positions = _positions_without_live_value(eth_copytrade_raw, 10**18, extra_fields=("watched_wallet",))

    try:
        eth_snipe_positions = (
            await eth_snipe_positions_live(w3, settings.eth_router_address, settings.eth_weth_address, eth_snipe_raw, history_path=settings.price_history_file) if w3 is not None
            else _positions_without_live_value(eth_snipe_raw, 10**18, wei_field="entry_amount_in_wei")
        )
    except Exception:
        eth_snipe_positions = _positions_without_live_value(eth_snipe_raw, 10**18, wei_field="entry_amount_in_wei")

    state["eth"] = {
        "address": eth_address, "balance": eth_balance,
        "copytrade_positions": eth_copytrade_positions, "snipe_positions": eth_snipe_positions,
    }

    sol_copytrade_raw = _load_positions(settings.solana_copytrade_positions_file)
    sol_address = None
    sol_balance = None
    sol_copytrade_positions: list[dict] = []
    if settings.solana_rpc_http_url and settings.resolved_solana_private_key():
        try:
            from jupiter_python_sdk.jupiter import Jupiter
            from solana.rpc.async_api import AsyncClient
            from solders.keypair import Keypair

            keypair = Keypair.from_base58_string(settings.resolved_solana_private_key())
            sol_address = str(keypair.pubkey())
            client = AsyncClient(settings.solana_rpc_http_url.get_secret_value())
            sol_balance = (await client.get_balance(keypair.pubkey())).value / 10**9
            jupiter = Jupiter(client, keypair)
            sol_copytrade_positions = await solana_copytrade_positions_live(jupiter, sol_copytrade_raw, history_path=settings.price_history_file)
        except Exception as exc:
            state["solana_error"] = f"{type(exc).__name__}: не удалось получить живые Solana-данные"
            sol_copytrade_positions = _positions_without_live_value(sol_copytrade_raw, 10**9, extra_fields=("watched_wallet",))
    elif sol_copytrade_raw:
        sol_copytrade_positions = _positions_without_live_value(sol_copytrade_raw, 10**9, extra_fields=("watched_wallet",))

    state["solana"] = {"address": sol_address, "balance": sol_balance, "copytrade_positions": sol_copytrade_positions}

    prices = fetch_usd_prices()
    state["wallet_stats"] = wallet_stats_view(settings.trade_log_file, prices)
    state["prices"] = prices
    return state


def render_prometheus(state: dict) -> str:
    """Тот же state, что и /api/state (см. gather_state выше) — переиспользует
    уже вычисленное, не отдельный источник правды. Формат — обычный
    Prometheus text exposition (https://prometheus.io/docs/instrumenting/exposition_formats/),
    руками, без пакета prometheus_client — для горстки gauge-метрик стандартный
    формат проще, чем тянуть библиотеку под него.

    `# HELP`/`# TYPE` — РОВНО один раз на имя метрики, ПЕРЕД всеми её
    сэмплами (несколько таких блоков на одно имя — невалидный exposition
    format, Prometheus/promtool это отклонит) — поэтому сначала собираем
    сэмплы по имени метрики, потом рендерим каждую группу целиком."""
    families: dict[str, tuple[str, list[tuple[str, object]]]] = {}

    def gauge(name: str, help_text: str, value, labels: str = "") -> None:
        if value is None:
            return  # Prometheus не поддерживает null — просто не публикуем сэмпл
        families.setdefault(name, (help_text, []))[1].append((labels, value))

    gauge("wakefinder_kill_switch_engaged", "1 если kill switch включён, иначе 0", int(state["kill_switch_engaged"]))
    gauge("wakefinder_eth_balance", "Живой баланс ETH-кошелька", state["eth"]["balance"])
    gauge("wakefinder_sol_balance", "Живой баланс SOL-кошелька", state["solana"]["balance"])

    for hb in state["heartbeats"]:
        labels = f'{{process="{hb["process"]}"}}'
        gauge("wakefinder_heartbeat_age_seconds", "Секунд с последнего heartbeat процесса", hb["age_seconds"], labels)
        gauge("wakefinder_heartbeat_stale", "1 если heartbeat устарел, иначе 0", int(hb["stale"]), labels)

    for chain, m in state["metrics"].items():
        labels = f'{{chain="{chain}"}}'
        gauge("wakefinder_trade_attempts_total", "Всего попыток сделок из trade_log", m["total_attempts"], labels)
        gauge("wakefinder_trade_included_total", "Сколько попыток попало в блок", m["included"], labels)
        gauge("wakefinder_fill_rate", "Included / total_attempts", m["fill_rate"], labels)
        gauge("wakefinder_avg_expected_profit", "Средняя ожидаемая прибыль на попытку, нативные единицы", m["avg_expected_profit"], labels)
        gauge("wakefinder_avg_realized_profit", "Средняя реализованная прибыль на included-сделку, нативные единицы", m["avg_realized_profit"], labels)
        gauge("wakefinder_simulation_accuracy", "Среднее отношение realized/expected", m["simulation_accuracy"], labels)

    lines: list[str] = []
    for name, (help_text, samples) in families.items():
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            lines.append(f"{name}{labels} {value}")

    return "\n".join(lines) + "\n"
