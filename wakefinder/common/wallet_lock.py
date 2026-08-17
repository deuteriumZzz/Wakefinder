"""Взаимоисключающая блокировка по адресу кошелька — НЕ nonce-координатор.

Проект и так требует: разные стратегии на одном ETH_PRIVATE_KEY/
SOLANA_PRIVATE_KEY ДОЛЖНЫ идти в разных процессах/кошельках (см. docstring
каждого chains/*/*.py:run() — "иначе конфликт nonce"). Строить координатор,
который делает шаринг кошелька БЕЗОПАСНЫМ, значило бы поощрять конфигурацию,
которую сам проект считает плохой практикой не только из-за nonce (общий
кошелёк = общий риск на все стратегии сразу, сложнее считать exposure per
strategy). Вместо этого — сделать саму ОШИБКУ конфигурации громкой СРАЗУ при
старте, а не спустя часы работы как случайные "nonce too low"/"replacement
transaction underpriced" в рантайме, когда это труднее всего связать с
причиной.

Механизм — файловая блокировка (fcntl.flock, POSIX-only — тот же таргет
деплоя, что systemd-юниты в deploy/), тот же принцип межпроцессной
синхронизации без общей памяти, что kill switch/live_config.py."""

import fcntl
import os


class WalletAlreadyRunningError(RuntimeError):
    pass


def acquire_wallet_lock(lock_dir: str, address: str, process_name: str):
    """Держите возвращённый file handle живым весь срок жизни процесса —
    закрытие (в т.ч. падение процесса) освобождает блокировку автоматически,
    новый процесс сможет её захватить. НЕ закрывайте его сразу после вызова."""
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f"{address.lower()}.lock")
    f = open(lock_path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        holder = f.read().strip() or "неизвестный процесс"
        f.close()
        raise WalletAlreadyRunningError(
            f"кошелёк {address} уже используется процессом '{holder}' (lock-файл {lock_path}) — "
            f"разные стратегии на одном кошельке конфликтуют по nonce, см. README "
            f"'Операционные требования'. Используйте отдельный кошелёк для '{process_name}' "
            f"или остановите другой процесс."
        ) from None
    f.seek(0)
    f.truncate()
    f.write(process_name)
    f.flush()
    return f
