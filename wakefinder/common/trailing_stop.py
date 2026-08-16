"""Trailing-stop выход для снайпинга — принципиально другой триггер, чем
копитрейдинг: там выход "кит продал" или фиксированный стоп-лосс от цены
входа (common/drawdown.py использует то же "от входа" мышление). Здесь нет
кита, за которым следить, а фиксированный стоп-лосс от входа отдаёт всю
прибыль обратно рынку при резком памп-и-дампе — держит позицию до отката
НИЖЕ ПИКА, а не ниже входа, так что часть движения вверх фиксируется."""

from dataclasses import dataclass


@dataclass
class TrailingStopTracker:
    trail_pct: float  # напр. 30 = выход при просадке на 30% от локального пика после входа
    peak: int = 0

    def update(self, current_value: int) -> bool:
        """current_value — текущая оценка позиции (в единицах token_in, напр.
        wei ETH). Возвращает True, если сработал trailing stop (пора выходить)."""
        if current_value > self.peak:
            self.peak = current_value
            return False
        if self.peak == 0:
            return False
        floor = self.peak * (100 - self.trail_pct) // 100
        return current_value < floor


def demo() -> None:
    t = TrailingStopTracker(trail_pct=30)
    assert t.update(100) is False and t.peak == 100
    assert t.update(150) is False and t.peak == 150  # новый пик
    assert t.update(120) is False  # 120 >= 150*0.7=105, ещё держим
    assert t.update(100) is True  # 100 < 105, стоп сработал
    print("ok")


if __name__ == "__main__":
    demo()
