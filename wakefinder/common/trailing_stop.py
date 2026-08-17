"""Trailing-stop выход для снайпинга — принципиально другой триггер, чем
копитрейдинг: там выход "кит продал" или фиксированный стоп-лосс от цены
входа (common/drawdown.py использует то же "от входа" мышление). Здесь нет
кита, за которым следить, а фиксированный стоп-лосс от входа отдаёт всю
прибыль обратно рынку при резком памп-и-дампе — держит позицию до отката
НИЖЕ ПИКА, а не ниже входа, так что часть движения вверх фиксируется.

Опциональный momentum-выход (`momentum_reversal_pct`) — ДОПОЛНИТЕЛЬНЫЙ,
более быстрый триггер поверх обычного trail_pct: тот смотрит на просадку от
ПИКА (может понадобиться несколько проверок подряд, чтобы накопиться), этот
— на скорость обвала МЕЖДУ ДВУМЯ ПОСЛЕДНИМИ проверками (один резкий провал
между соседними замерами). Полезно, когда обвал настолько быстрый, что
кумулятивный % от пика ещё не пробил trail_pct, а цена уже обрушивается.
По умолчанию выключен (`None`) — поведение не меняется, пока не задан явно."""

from dataclasses import dataclass


@dataclass
class TrailingStopTracker:
    trail_pct: float  # напр. 30 = выход при просадке на 30% от локального пика после входа
    momentum_reversal_pct: float | None = None  # напр. 20 = выход при обвале на 20%+ МЕЖДУ ДВУМЯ последними проверками
    peak: int = 0
    _last_value: int | None = None

    def update(self, current_value: int) -> bool:
        """current_value — текущая оценка позиции (в единицах token_in, напр.
        wei ETH). Возвращает True, если сработал любой из двух триггеров
        (пора выходить)."""
        momentum_triggered = False
        if (
            self.momentum_reversal_pct is not None
            and self._last_value is not None
            and self._last_value > 0
            and current_value < self._last_value
        ):
            drop_pct = (self._last_value - current_value) / self._last_value * 100
            momentum_triggered = drop_pct >= self.momentum_reversal_pct
        self._last_value = current_value

        if current_value > self.peak:
            self.peak = current_value
            return False  # новый пик по определению не может быть провалом (см. docstring момента выше)
        if self.peak == 0:
            return momentum_triggered
        floor = self.peak * (100 - self.trail_pct) // 100
        return momentum_triggered or current_value < floor


def demo() -> None:
    t = TrailingStopTracker(trail_pct=30)
    assert t.update(100) is False and t.peak == 100
    assert t.update(150) is False and t.peak == 150  # новый пик
    assert t.update(120) is False  # 120 >= 150*0.7=105, ещё держим
    assert t.update(100) is True  # 100 < 105, стоп сработал

    m = TrailingStopTracker(trail_pct=30, momentum_reversal_pct=20)
    assert m.update(100) is False and m.peak == 100
    assert m.update(150) is False  # новый пик
    assert m.update(130) is False  # (150-130)/150 = 13% < 20%, momentum не сработал, и 130 >= floor 105
    assert m.update(100) is True  # (130-100)/130 = 23% >= 20% — momentum сработал РАНЬШЕ обычного trail_pct
    print("ok")


if __name__ == "__main__":
    demo()
