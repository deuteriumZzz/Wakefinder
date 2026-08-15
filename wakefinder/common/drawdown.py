"""Circuit breaker по совокупной РЕАЛИЗОВАННОЙ просадке за скользящее окно —
агрегирует ВСЕ стратегии одной сети (arb + copytrade вместе, не по кошелькам).
Независим от MAX_CONSECUTIVE_FAILURES: тот защищает от "бандл не попадает в
блок" (сломанный пайплайн), этот — от "стратегия работает как задумано, но
теряет деньги" (плохая неделя у китов, слиппедж съедает маржу арбитража).

check_drawdown принимает необязательный unrealized_pnl (в тех же нативных
единицах, что и realized) — вызывающая сторона (copytrade-энтрипоинты)
переоценивает свои открытые позиции живым RPC/quote-запросом и передаёт
число сюда; сам drawdown.py остаётся синхронным и не делает сетевых вызовов.
Дублирования со стоп-лосс циклом нет: тот закрывает ОДНУ позицию по цене,
этот — читает уже посчитанную агрегированную переоценку всех позиций сразу.

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
    unrealized_pnl: int
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
    trade_log_path: str, chain: str, window_seconds: float, max_loss: int, now: float | None = None,
    unrealized_pnl: int = 0,
) -> DrawdownStatus:
    realized = compute_realized_pnl(trade_log_path, chain, window_seconds, now=now)
    total = realized + unrealized_pnl
    return DrawdownStatus(realized_pnl=realized, unrealized_pnl=unrealized_pnl, breached=total < -max_loss)
