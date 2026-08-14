"""AIMD-контроллер доли профита, предлагаемой как tip/priority fee. Проигранный
аукцион (бандл не попал в блок) -> увеличиваем ставку крупным шагом (агрессивнее
конкурируем); выигранный -> немного снижаем (оставляем себе больше маржи).
Ограничен [floor_bps, ceiling_bps].

ponytail: простой AIMD (тот же принцип, что управление перегрузкой в TCP), не
полноценный bandit/ML-подбор ставки — этого достаточно для бота такого
масштаба; апгрейд до модели конкуренции, если понадобится точнее.
"""


class AdaptiveTipController:
    def __init__(
        self,
        initial_bps: int,
        floor_bps: int = 1000,
        ceiling_bps: int = 9900,
        increase_step: int = 500,
        decrease_step: int = 100,
    ):
        if not floor_bps <= initial_bps <= ceiling_bps:
            raise ValueError("initial_bps должен быть между floor_bps и ceiling_bps")
        self._bps = initial_bps
        self.floor_bps = floor_bps
        self.ceiling_bps = ceiling_bps
        self.increase_step = increase_step
        self.decrease_step = decrease_step

    def record_outcome(self, included: bool) -> None:
        if included:
            self._bps = max(self.floor_bps, self._bps - self.decrease_step)
        else:
            self._bps = min(self.ceiling_bps, self._bps + self.increase_step)

    @property
    def current_bps(self) -> int:
        return self._bps
