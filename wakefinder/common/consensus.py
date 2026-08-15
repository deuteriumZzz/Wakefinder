"""Консенсус-трекер: требует, чтобы N РАЗНЫХ отслеживаемых кошельков купили
один и тот же токен в течение окна времени, прежде чем считать это сигналом на
вход для копитрейдинга. Один кит может ошибаться; несколько независимых китов,
сходящихся на одном токене почти одновременно — сильнее сигнал.

В отличие от буферизации возможностей в арбитраже (сознательно НЕ сделана —
там окно исполнения миллисекунды, к моменту сравнения котировка устареет),
здесь буферизация уместна: позиция копитрейдинга держится минутами/часами, а
не одним блоком, так что несколько секунд на сбор консенсуса не обесценивают
сигнал.
"""

import time


class ConsensusTracker:
    def __init__(self, min_wallets: int, window_seconds: float):
        self.min_wallets = min_wallets
        self.window_seconds = window_seconds
        self._signals: dict[str, dict[str, float]] = {}  # token -> {wallet: timestamp}

    def record_buy(self, token: str, wallet: str, now: float | None = None) -> bool:
        """Возвращает True, если после этой записи консенсус (min_wallets
        разных кошельков за window_seconds) достигнут."""
        now = time.time() if now is None else now
        token = token.lower()
        wallets = self._signals.setdefault(token, {})
        wallets[wallet.lower()] = now

        cutoff = now - self.window_seconds
        for w in [w for w, t in wallets.items() if t < cutoff]:
            del wallets[w]

        return len(wallets) >= self.min_wallets

    def clear(self, token: str) -> None:
        self._signals.pop(token.lower(), None)
