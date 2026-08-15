"""Агрегированные метрики по trade_log.jsonl — fill rate и точность симуляции
(realized vs expected, см. common/reconciliation.py). Тот же принцип
переиспользования данных, что и в wallet_stats.py: один источник правды
(trade_log.jsonl), разные срезы одних и тех же записей."""

import json
import os
from dataclasses import dataclass


@dataclass
class ChainMetrics:
    chain: str
    total_attempts: int
    included: int
    fill_rate: float  # included / total_attempts, 0.0 если попыток не было
    avg_expected_profit: float
    avg_realized_profit: float | None  # None, если ни одной записи с realized_profit
    simulation_accuracy: float | None  # среднее realized/expected там, где оба есть и expected != 0


def compute_chain_metrics(trade_log_path: str) -> dict[str, ChainMetrics]:
    if not os.path.exists(trade_log_path):
        return {}

    totals: dict[str, int] = {}
    included_counts: dict[str, int] = {}
    expected_sums: dict[str, int] = {}
    realized_sums: dict[str, int] = {}
    realized_counts: dict[str, int] = {}
    accuracy_sums: dict[str, float] = {}
    accuracy_counts: dict[str, int] = {}

    with open(trade_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            chain = record.get("chain")
            if not chain:
                continue

            totals[chain] = totals.get(chain, 0) + 1
            if record.get("included"):
                included_counts[chain] = included_counts.get(chain, 0) + 1

            expected = record.get("expected_profit", 0)
            expected_sums[chain] = expected_sums.get(chain, 0) + expected

            realized = record.get("realized_profit")
            if realized is not None:
                realized_sums[chain] = realized_sums.get(chain, 0) + realized
                realized_counts[chain] = realized_counts.get(chain, 0) + 1
                if expected:
                    accuracy_sums[chain] = accuracy_sums.get(chain, 0.0) + (realized / expected)
                    accuracy_counts[chain] = accuracy_counts.get(chain, 0) + 1

    result: dict[str, ChainMetrics] = {}
    for chain, total in totals.items():
        included = included_counts.get(chain, 0)
        result[chain] = ChainMetrics(
            chain=chain,
            total_attempts=total,
            included=included,
            fill_rate=(included / total) if total else 0.0,
            avg_expected_profit=expected_sums.get(chain, 0) / total,
            avg_realized_profit=(realized_sums[chain] / realized_counts[chain]) if realized_counts.get(chain) else None,
            simulation_accuracy=(accuracy_sums[chain] / accuracy_counts[chain]) if accuracy_counts.get(chain) else None,
        )
    return result
