"""CLI-дашборд: сводка по trade_log.jsonl и открытым позициям, без веб-сервера
и новых зависимостей. Переиспользует ту же агрегацию, что и трекинг
результативности кошельков (wakefinder.common.wallet_stats) — это два способа
показать одни и те же данные, не два разных источника правды.

Запуск: python -m wakefinder.dashboard [--trade-log PATH] [--positions PATH ...]
"""

import argparse
import json
import os

from wakefinder.common.price_feed import fetch_usd_prices
from wakefinder.common.wallet_stats import compute_wallet_stats

_CHAIN_DECIMALS = {"eth": 10**18, "sol": 10**9}


def _print_positions(label: str, path: str) -> None:
    if not os.path.exists(path):
        print(f"{label}: файл не найден ({path})")
        return
    with open(path) as f:
        positions = json.load(f)
    if not positions:
        print(f"{label}: открытых позиций нет")
        return
    print(f"{label}: {len(positions)} открытых позиций")
    for token, pos in positions.items():
        print(f"  {token[:10]}...  entry_amount_in={pos['entry_amount_in']}  wallet={pos['watched_wallet'][:10]}...")


def _usd_estimate(net_pnl_estimate: int, chain: str, prices: dict[str, float]) -> str:
    decimals = _CHAIN_DECIMALS.get(chain)
    price = prices.get(chain)
    if decimals is None or price is None:
        return ""  # неизвестная сеть или цена недоступна (CoinGecko недоступен/rate-limit) — не гадаем
    return f"  (~${net_pnl_estimate / decimals * price:,.2f})"


def _print_wallet_stats(trade_log_path: str, prices: dict[str, float]) -> None:
    stats = compute_wallet_stats(trade_log_path)
    if not stats:
        print(f"Статистика по кошелькам: нет данных в {trade_log_path}")
        return
    print("Статистика по кошелькам (приблизительно, см. docstring wallet_stats.py):")
    for s in sorted(stats.values(), key=lambda s: s.net_pnl_estimate, reverse=True):
        usd = _usd_estimate(s.net_pnl_estimate, s.chain, prices)
        print(f"  {s.wallet[:12]}...  entries={s.entries}  exits={s.exits}  net_pnl~={s.net_pnl_estimate}{usd}  win_rate={s.win_rate:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Wakefinder CLI-дашборд")
    parser.add_argument("--trade-log", default="trades.jsonl")
    parser.add_argument("--positions", default="positions.json")
    parser.add_argument("--positions-solana", default="positions_solana.json")
    parser.add_argument("--no-usd", action="store_true", help="не запрашивать USD-цены с CoinGecko (офлайн/приватность)")
    args = parser.parse_args()

    prices = {} if args.no_usd else fetch_usd_prices()

    print("=== Wakefinder dashboard ===\n")
    _print_positions("ETH copytrade", args.positions)
    _print_positions("Solana copytrade", args.positions_solana)
    print()
    _print_wallet_stats(args.trade_log, prices)


if __name__ == "__main__":
    main()
