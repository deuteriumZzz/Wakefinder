"""Детекция ЗАВИСШЕЙ позиции: N подряд неудачных попыток оценить цену
(getAmountsOut/quote бросает исключение или явно возвращает None — обычно
означает высохшую ликвидность/rug) — принципиально другое состояние, чем
"trailing-stop/стоп-лосс просто ещё не сработал", где цену УДАЁТСЯ узнать,
просто она ещё не пересекла порог.

Без этого трекера все 4 price-check loop'а (_stop_loss_loop/_trailing_stop_loop
в chains/{eth,solana}/{copytrade,snipe}.py) на сбой оценки цены делают только
`continue` — бот молча держит мёртвую позицию неограниченно долго, ничего не
сигнализируя оператору."""

from dataclasses import dataclass, field


@dataclass
class StuckPositionTracker:
    threshold: int

    _failures: dict[str, int] = field(default_factory=dict)
    _stuck: set[str] = field(default_factory=set)

    def record_failure(self, token: str) -> bool:
        """Возвращает True РОВНО ОДИН РАЗ — в момент пересечения порога
        (чтобы вызывающий код мог послать один алерт, а не на каждой
        итерации, пока позиция остаётся зависшей)."""
        token = token.lower()
        count = self._failures.get(token, 0) + 1
        self._failures[token] = count
        if count >= self.threshold and token not in self._stuck:
            self._stuck.add(token)
            return True
        return False

    def record_success(self, token: str) -> bool:
        """Возвращает True, если позиция ДО ЭТОГО считалась зависшей
        (восстановилась — цену снова удалось узнать)."""
        token = token.lower()
        self._failures.pop(token, None)
        was_stuck = token in self._stuck
        self._stuck.discard(token)
        return was_stuck

    def is_stuck(self, token: str) -> bool:
        return token.lower() in self._stuck


def demo() -> None:
    t = StuckPositionTracker(threshold=3)
    assert t.record_failure("0xAAA") is False
    assert t.record_failure("0xaaa") is False
    assert t.record_failure("0xAAA") is True  # порог пересечён на 3-й раз
    assert t.record_failure("0xAAA") is False  # уже зависшая — не повторяем алерт
    assert t.is_stuck("0xaaa") is True
    assert t.record_success("0xAAA") is True  # восстановилась
    assert t.is_stuck("0xaaa") is False
    assert t.record_success("0xAAA") is False  # уже не зависшая — не сигнализируем повторно
    print("OK")


if __name__ == "__main__":
    demo()
