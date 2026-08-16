"""Лёгкий лог истории цены открытых позиций — JSONL, append-only, тот же
принцип, что и trade_log.jsonl. НЕ архивные исторические данные (не
запрашивает прошлые блоки через архивную ноду — дорого/медленно для
свежесозданных мемкоин-пулов, да и не все публичные RPC вообще это
поддерживают) — только точки, накопленные С МОМЕНТА, когда что-то реально
проверяло текущую стоимость позиции.

Пишется из wakefinder/live_state.py на каждом успешном /api/state-опросе
дашборда — ПОБОЧНЫЙ эффект уже выполняемого RPC-запроса (getAmountsOut/
Jupiter quote для отображения), не отдельный источник нагрузки. Следствие:
история копится, только пока дашборд запущен и его открывают — честный
компромисс, не баг; см. README "Живые графики позиций"."""

import json
import os
import time


def log_snapshot(path: str, token: str, value: float) -> None:
    record = {"ts": time.time(), "token": token.lower(), "value": value}
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def read_history(path: str, token: str, limit: int = 500) -> list[dict]:
    if not os.path.exists(path):
        return []
    token = token.lower()
    points = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("token") == token:
                points.append(record)
    return points[-limit:]
