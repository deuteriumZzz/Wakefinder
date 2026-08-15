"""Append-only JSONL-лог попыток сделок. Не полноценный PnL-дашборд — тот
потребовал бы снимков баланса до/после каждой сделки и защиты от гонок с
остальной активностью кошелька, это отдельная, намного большая задача. Здесь —
только сырые данные, достаточные, чтобы проанализировать реальные результаты
позже (симулированная прибыль против того, что реально произошло по tx_refs)."""

import json
import time


def log_attempt(
    path: str,
    chain: str,
    pool_address: str,
    expected_profit: int,
    included: bool,
    tx_refs: list[str],
    strategy: str = "arb",
    wallet: str = "",
) -> None:
    record = {
        "ts": time.time(),
        "chain": chain,
        "strategy": strategy,  # "arb" | "copytrade_entry" | "copytrade_exit"
        "pool": pool_address,
        "expected_profit": expected_profit,
        "included": included,
        "tx_refs": tx_refs,
        "wallet": wallet,  # watched-кошелёк, триггернувший сделку (для copytrade; пусто для arb)
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
