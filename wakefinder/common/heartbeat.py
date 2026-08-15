"""Heartbeat-файл: периодическая отметка "процесс жив" для внешнего мониторинга
(cron/systemd watchdog/что угодно, читающее timestamp из файла). Не заменяет
process supervision (см. deploy/systemd/*.service, Restart=always) — это сигнал
ДЛЯ супервизора/алертинга о том, что событийный цикл процесса не завис молча
(что не ловит ни kill switch, ни with_reconnect — оба реагируют на явные
исключения, а не на тихое зависание)."""

import argparse
import asyncio
import os
import time


def write(path: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(str(time.time()))
    os.replace(tmp, path)  # атомарно — читающий процесс не увидит частичную запись


def last_beat(path: str) -> float | None:
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_stale(path: str, max_age_seconds: float, now: float | None = None) -> bool:
    beat = last_beat(path)
    if beat is None:
        return True
    return (now if now is not None else time.time()) - beat > max_age_seconds


async def loop(path: str, interval_seconds: float) -> None:
    while True:
        write(path)
        await asyncio.sleep(interval_seconds)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Проверка heartbeat-файла процесса wakefinder")
    parser.add_argument("path")
    parser.add_argument("max_age_seconds", type=float)
    args = parser.parse_args()

    beat = last_beat(args.path)
    if beat is None:
        print(f"нет heartbeat: {args.path}")
        raise SystemExit(1)

    age = time.time() - beat
    stale = age > args.max_age_seconds
    print(f"последний heartbeat {age:.0f}s назад ({'STALE' if stale else 'ok'})")
    raise SystemExit(1 if stale else 0)


if __name__ == "__main__":
    _main()
