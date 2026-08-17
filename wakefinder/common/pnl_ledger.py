"""Append-only JSONL-леджер РЕАЛИЗОВАННЫХ (закрытых) сделок — в отличие от
trade_log.jsonl, где entry/exit копитрейдинга/снайпинга — это ДВЕ отдельные
записи попыток (без привязки друг к другу), здесь одна запись = одна
закрытая позиция с уже посчитанным realized_pnl (для arb — та же цифра, что
и realized_profit в trade_log, т.к. там сделка атомарна и entry/exit
совпадают; сюда просто дублируется как "закрытая сделка" для единого
представления). Пишется только когда сделка реально включилась в блок —
без include ни entry, ни exit не дают настоящих чисел (см. общий принцип
"не оценка, а честный факт" в trade_log.py)."""

import json
import os
import time


def record_closed_trade(
    path: str,
    chain: str,
    strategy: str,
    realized_pnl: int,
    token: str = "",
    wallet: str = "",
    opened_at: float | None = None,
) -> None:
    record = {
        "ts": time.time(),
        "chain": chain,
        "strategy": strategy,  # "arb" | "copytrade" | "snipe"
        "token": token,
        "wallet": wallet,
        "realized_pnl": realized_pnl,
        "holding_seconds": (time.time() - opened_at) if opened_at is not None else None,
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def read_closed_trades(path: str, chain: str | None = None, limit: int = 1000) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if chain is not None and record.get("chain") != chain:
                continue
            out.append(record)
    return out[-limit:]


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "pnl.jsonl")
        record_closed_trade(p, "eth", "copytrade", 500, token="0xTOKEN", wallet="0xWHALE", opened_at=time.time() - 30)
        record_closed_trade(p, "solana", "arb", -100, token="")
        rows = read_closed_trades(p)
        assert len(rows) == 2
        assert rows[0]["realized_pnl"] == 500
        assert rows[0]["holding_seconds"] > 0
        assert read_closed_trades(p, chain="eth") == [rows[0]]
        assert read_closed_trades(p, chain="solana") == [rows[1]]
        assert read_closed_trades(os.path.join(d, "missing.jsonl")) == []
        print("ok")
