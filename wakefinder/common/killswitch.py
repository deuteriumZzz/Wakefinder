"""Единая точка kill switch для всех 4 процессов (ETH/Solana × arb/copytrade).

Раньше каждый процесс проверял settings.kill_switch_file напрямую через
os.path.exists — формально один и тот же файл по умолчанию, но ОТНОСИТЕЛЬНЫЙ
путь (".kill"), который резолвится по-разному, если 4 процесса запущены из
разных рабочих директорий (разные деплой-скрипты, разные сервисы systemd) —
тогда "единый" kill switch на самом деле не единый. Дефолт здесь —
АБСОЛЮТНЫЙ путь в домашней директории, не зависящий от cwd процесса.

Плюс — CLI для операционной ясности, а не "не забудьте touch .kill в нужной
папке": `python -m wakefinder.common.killswitch stop|resume|status`.
"""

import argparse
import os
import time

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".wakefinder_kill_switch")


def is_engaged(path: str) -> bool:
    return os.path.exists(path)


def engage(path: str, reason: str = "") -> None:
    with open(path, "a") as f:
        f.write(f"{time.time()} {reason}\n")


def disengage(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Wakefinder kill switch — общий для всех 4 процессов")
    parser.add_argument("action", choices=["stop", "resume", "status"])
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--reason", default="manual CLI")
    args = parser.parse_args()

    if args.action == "stop":
        engage(args.path, args.reason)
        print(f"kill switch включён: {args.path}")
    elif args.action == "resume":
        disengage(args.path)
        print(f"kill switch снят: {args.path}")
    else:
        print(f"{'ВКЛЮЧЁН' if is_engaged(args.path) else 'выключен'}: {args.path}")


if __name__ == "__main__":
    _main()
