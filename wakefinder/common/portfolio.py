"""Портфельный срез: единый вид капитала/PnL через ВСЕ стратегии, не по
отдельности за процесс. До этого не было единого числа "сколько всего
работает" — веб-дашборд (live_state.py:gather_state) показывает баланс
ТОЛЬКО того одного кошелька, которым запущен САМ dashboard-процесс (общий
Settings-singleton на один ETH_PRIVATE_KEY/SOLANA_PRIVATE_KEY), а
strategy_stats_view даёт только Sharpe/win-rate по стратегии, без
$-суммы вообще.

ЧЕСТНАЯ ГРАНИЦА: архитектура "один процесс = один кошелёк на стратегию"
(см. README "Конфиг-профили и CLI") означает, что dashboard-процесс
физически не имеет приватных ключей остальных стратегий — capital-часть
портфельного вида требует ЯВНОГО перечисления ПУБЛИЧНЫХ адресов
(PORTFOLIO_WALLETS в config.py), балансы читаются read-only через RPC,
приватные ключи для этого не нужны и никогда не запрашиваются. Без них —
портфельный вид ограничивается PnL-агрегатом по pnl_ledger.jsonl (общий
файл на все процессы, если не переопределён per-профиль kill_switch_file-
подобным механизмом — см. README "Выборочное выключение").

USD-конвертация — через common/price_feed.py (тот же дашборд-only источник,
что и у остального дашборда, НЕ участвует в торговой логике)."""

import logging

from wakefinder.common.pnl_ledger import read_closed_trades

logger = logging.getLogger("wakefinder.portfolio")

_DECIMALS = {"eth": 10**18, "solana": 10**9}
_PRICE_KEYS = {"eth": "eth", "solana": "sol"}


def parse_portfolio_wallets(raw: str) -> list[dict]:
    """"label:chain:address,label2:chain2:address2" -> [{"label","chain","address"}, ...].
    Запись без ровно 2 двоеточий пропускается с предупреждением — опечатка
    в конфиге не должна ни тихо терять кошелёк, ни ронять весь дашборд."""
    wallets = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            logger.warning("PORTFOLIO_WALLETS: пропуск некорректной записи %r (ожидается label:chain:address)", entry)
            continue
        label, chain, address = parts
        wallets.append({"label": label, "chain": chain.lower(), "address": address})
    return wallets


async def fetch_wallet_balances(
    wallets: list[dict], eth_rpc_http_url: str | None, solana_rpc_http_url: str | None,
) -> list[dict]:
    """balance=None для кошелька, чей RPC-запрос не удался (или сеть не
    сконфигурирована/недоступна) — не 0 (0 выглядело бы как "кошелёк пуст",
    а не "не знаем"), тот же принцип, что price_feed.py. Своё, read-only
    RPC-соединение на вызов (безопасно конструировать заново — см. docstring
    common/protected_rpc.py про AsyncHTTPProvider session-caching), не
    завязано на то, какой профиль сейчас запущен в этом процессе."""
    eth_wallets = [w for w in wallets if w["chain"] == "eth"]
    solana_wallets = [w for w in wallets if w["chain"] == "solana"]
    results: list[dict] = []

    if eth_wallets and eth_rpc_http_url:
        from web3 import AsyncHTTPProvider, AsyncWeb3, Web3

        w3 = AsyncWeb3(AsyncHTTPProvider(eth_rpc_http_url))
        for w in eth_wallets:
            balance = None
            try:
                raw_balance = await w3.eth.get_balance(Web3.to_checksum_address(w["address"]))
                balance = raw_balance / _DECIMALS["eth"]
            except Exception as exc:
                logger.warning("не удалось получить ETH-баланс %s (%s): %s", w["label"], w["address"], type(exc).__name__)
            results.append({**w, "balance": balance})
    else:
        results.extend({**w, "balance": None} for w in eth_wallets)

    if solana_wallets and solana_rpc_http_url:
        from solana.rpc.async_api import AsyncClient
        from solders.pubkey import Pubkey

        async with AsyncClient(solana_rpc_http_url) as client:
            for w in solana_wallets:
                balance = None
                try:
                    resp = await client.get_balance(Pubkey.from_string(w["address"]))
                    balance = resp.value / _DECIMALS["solana"]
                except Exception as exc:
                    logger.warning("не удалось получить SOL-баланс %s (%s): %s", w["label"], w["address"], type(exc).__name__)
                results.append({**w, "balance": balance})
    else:
        results.extend({**w, "balance": None} for w in solana_wallets)

    return results


def aggregate_realized_pnl(pnl_ledger_file: str, prices: dict) -> dict:
    """Сумма realized_pnl по (chain, strategy) из ВСЕЙ истории pnl_ledger.jsonl
    (limit=100_000, тот же выбор, что strategy_stats_view — для суммы нужна
    вся выборка, не только "последние N"), с конвертацией в USD."""
    rows = read_closed_trades(pnl_ledger_file, limit=100_000)
    pnl_by_strategy: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["chain"], r["strategy"])
        pnl_by_strategy[key] = pnl_by_strategy.get(key, 0) + r["realized_pnl"]

    breakdown = []
    total_usd = 0.0
    complete = True
    for (chain, strategy), pnl_native in sorted(pnl_by_strategy.items()):
        decimals = _DECIMALS.get(chain)
        price = prices.get(_PRICE_KEYS.get(chain, chain))
        pnl_units = pnl_native / decimals if decimals else pnl_native
        pnl_usd = pnl_units * price if price is not None else None
        if pnl_usd is None:
            complete = False
        else:
            total_usd += pnl_usd
        breakdown.append({"chain": chain, "strategy": strategy, "realized_pnl": pnl_units, "realized_pnl_usd": pnl_usd})

    return {"breakdown": breakdown, "total_realized_pnl_usd": total_usd, "complete": complete}


def aggregate_capital(wallet_balances: list[dict], prices: dict) -> dict:
    """complete=False если список кошельков пуст (нечего агрегировать —
    отличаем от "агрегировали, но какая-то цена/баланс не получены") или
    если хоть один баланс/цена не получены — не показываем частичную сумму
    молча как полную (тот же "честно, не fake-precision" принцип, что и во
    всём остальном проекте)."""
    total_usd = 0.0
    complete = bool(wallet_balances)
    for w in wallet_balances:
        price = prices.get(_PRICE_KEYS.get(w["chain"], w["chain"]))
        if price is None or w.get("balance") is None:
            complete = False
            continue
        total_usd += w["balance"] * price
    return {"wallets": wallet_balances, "total_capital_usd": total_usd, "complete": complete}


def portfolio_summary(pnl_ledger_file: str, wallet_balances: list[dict], prices: dict) -> dict:
    return {
        "pnl": aggregate_realized_pnl(pnl_ledger_file, prices),
        "capital": aggregate_capital(wallet_balances, prices),
    }
