"""Оценка результативности watched-кошельков по trade_log.jsonl.

ponytail: PnL здесь — сумма КОТИРУЕМЫХ (на момент отправки, до фактического
слиппеджа) amount_in/expected_profit по записям входа/выхода одного и того же
кошелька, не аудированная постфактум бухгалтерия по реальным балансам. Тот же
компромисс, что и во всём остальном логировании этого проекта (см. docstring
trade_log.py) — направление верное ("этот кит стабильно прибылен" против "по
нулям"), точность до цента не гарантирована. Апгрейд — сверка с реальными
балансами/receipts, если понадобится точнее.
"""

import json
import os
from dataclasses import dataclass


@dataclass
class WalletStats:
    wallet: str
    entries: int
    exits: int
    net_pnl_estimate: int  # сумма exit-сумм минус сумма entry-сумм, приблизительно
    win_rate: float  # доля exit-записей с положительной оценкой profit
    chain: str = ""  # из record["chain"] — нужен, чтобы правильно перевести net_pnl_estimate в USD (разные decimals/цена у ETH и Solana)


def compute_wallet_stats(trade_log_path: str) -> dict[str, WalletStats]:
    if not os.path.exists(trade_log_path):
        return {}

    entries_sum: dict[str, int] = {}
    exits_sum: dict[str, int] = {}
    entries_count: dict[str, int] = {}
    exits_count: dict[str, int] = {}
    exit_wins: dict[str, int] = {}
    wallet_chain: dict[str, str] = {}

    with open(trade_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            wallet = (record.get("wallet") or "").lower()  # тот же lowercase-принцип сравнения адресов, что в allowlist.py/consensus.py
            if not wallet or not record.get("included"):
                continue

            strategy = record.get("strategy", "arb")
            amount = record.get("expected_profit", 0)
            wallet_chain[wallet] = record.get("chain", "")

            if strategy == "copytrade_entry":
                entries_sum[wallet] = entries_sum.get(wallet, 0) - amount
                entries_count[wallet] = entries_count.get(wallet, 0) + 1
            elif strategy == "copytrade_exit":
                exits_sum[wallet] = exits_sum.get(wallet, 0) + amount
                exits_count[wallet] = exits_count.get(wallet, 0) + 1
                if amount > 0:
                    exit_wins[wallet] = exit_wins.get(wallet, 0) + 1

    wallets = set(entries_count) | set(exits_count)
    result: dict[str, WalletStats] = {}
    for wallet in wallets:
        exits = exits_count.get(wallet, 0)
        result[wallet] = WalletStats(
            wallet=wallet,
            entries=entries_count.get(wallet, 0),
            exits=exits,
            net_pnl_estimate=entries_sum.get(wallet, 0) + exits_sum.get(wallet, 0),
            win_rate=(exit_wins.get(wallet, 0) / exits) if exits else 0.0,
            chain=wallet_chain.get(wallet, ""),
        )
    return result
