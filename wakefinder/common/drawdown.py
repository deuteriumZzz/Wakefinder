"""Circuit breaker по совокупной РЕАЛИЗОВАННОЙ просадке за скользящее окно —
агрегирует ВСЕ стратегии одной сети (arb + copytrade вместе, не по кошелькам).
Независим от MAX_CONSECUTIVE_FAILURES: тот защищает от "бандл не попадает в
блок" (сломанный пайплайн), этот — от "стратегия работает как задумано, но
теряет деньги" (плохая неделя у китов, слиппедж съедает маржу арбитража).

ponytail: считает только РЕАЛИЗОВАННЫЙ PnL из trade_log.jsonl, не
незафиксированную стоимость открытых копитрейд-позиций — переоценка живых
позиций задублировала бы логику, уже существующую в стоп-лосс циклах каждой
сети. Апгрейд — общий колбэк переоценки, если понадобится учитывать unrealized.

ponytail: полное сканирование файла на каждой проверке — O(n) от размера
trade_log.jsonl, растущего без ограничения. Нормально для текущего масштаба;
апгрейд — ротация лога или инкрементальная агрегация, если станет узким местом.

Нет USD-конвертации (нет price feed) — порог в нативных единицах сети
(wei для ETH, lamports для Solana), раздельно.
"""

import json
import os
import time
from dataclasses import dataclass


@dataclass
class DrawdownStatus:
    realized_pnl: int
    breached: bool


def compute_realized_pnl(trade_log_path: str, chain: str, window_seconds: float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    cutoff = now - window_seconds
    if not os.path.exists(trade_log_path):
        return 0

    total = 0
    with open(trade_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("chain") != chain or not record.get("included"):
                continue
            if record.get("ts", 0) < cutoff:
                continue

            strategy = record.get("strategy", "arb")
            amount = record.get("expected_profit", 0)
            if strategy == "arb":
                total += amount  # арбитраж по построению неотрицателен (simulator.py фильтрует profit<=0)
            elif strategy == "copytrade_entry":
                total -= amount  # потратили на вход
            elif strategy == "copytrade_exit":
                total += amount  # получили при выходе

    return total


def check_drawdown(
    trade_log_path: str, chain: str, window_seconds: float, max_loss: int, now: float | None = None
) -> DrawdownStatus:
    pnl = compute_realized_pnl(trade_log_path, chain, window_seconds, now=now)
    return DrawdownStatus(realized_pnl=pnl, breached=pnl < -max_loss)
