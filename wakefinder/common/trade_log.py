"""Append-only JSONL-лог попыток сделок. expected_profit — оценка на момент
симуляции; realized_profit (опционален, см. common/reconciliation.py) —
фактическая разница баланса token_in до/после, снятая ПОСТФАКТУМ после
подтверждения включения. Не защищено от гонок с остальной активностью того
же кошелька в промежутке (та же честная оговорка, что и раньше) — снимки
баланса, не индивидуальный трейс переводов.

Каждая запись — часть hash-chain (common/hash_chain.py) — правка/удаление
задним числом любой строки обнаружима через hash_chain.verify_chain(path),
не предотвращается (см. его docstring про честную границу)."""

import time

from wakefinder.common.hash_chain import append_chained


def log_attempt(
    path: str,
    chain: str,
    pool_address: str,
    expected_profit: int,
    included: bool,
    tx_refs: list[str],
    strategy: str = "arb",
    wallet: str = "",
    realized_profit: int | None = None,
    latency_ms: float | None = None,
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
    if realized_profit is not None:
        record["realized_profit"] = realized_profit
    if latency_ms is not None:
        # Детекция -> отправка бандла/tx, см. PendingSwap.detected_at и
        # common/metrics.py — только ENTRY-путям есть смысл это писать
        # (exit триггерится нашим же trailing-stop/live-config опросом, не
        # внешним событием, "задержка реакции" там не определена так же).
        record["latency_ms"] = latency_ms
    append_chained(path, record)
