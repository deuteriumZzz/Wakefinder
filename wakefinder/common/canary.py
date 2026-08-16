"""Поэтапный ввод капитала: вместо торговли настроенным
max_capital_per_bundle_*/copytrade_size_pct на полную с первого дня,
размер линейно растёт от CANARY_START_FRACTION до 100% по мере накопления
ПОДТВЕРЖДЁННЫХ (included) сделок — тот же принцип, что и canary-деплой в
софте, только для капитала. Дефолт (start_fraction=1.0) — canary выключен,
поведение не меняется, пока не задано явно в профиле.

Работает поверх уже существующего живого Settings-синглтона (pydantic-модели
мутируемы по умолчанию) — CanaryController держит ОРИГИНАЛЬНЫЕ значения полей
(взятые один раз при старте) и на каждом обновлении масштабирует их заново
от оригинала, а не от уже уменьшенного текущего значения — иначе повторные
вызовы прогрессивно урезали бы риск-параметры до нуля.

Не заменяет drawdown circuit breaker (common/drawdown.py) — та отвечает за
"стоп при просадке", эта — за "постепенный разгон при отсутствии просадки".
Разные роли, разные модули."""

import json
import os


def compute_canary_fraction(
    trade_log_path: str, chain: str, start_fraction: float, ramp_trades: int,
) -> float:
    """Доля (0..1] полного размера позиции, доступная сейчас — линейно растёт
    от start_fraction до 1.0 по мере накопления included-сделок по этой сети
    (арбитраж + копитрейд вместе, тот же принцип агрегации, что в drawdown.py)."""
    if ramp_trades <= 0:
        return 1.0
    if not os.path.exists(trade_log_path):
        return start_fraction

    included_count = 0
    with open(trade_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("chain") == chain and record.get("included"):
                included_count += 1

    progress = min(included_count / ramp_trades, 1.0)
    return start_fraction + (1.0 - start_fraction) * progress


class CanaryController:
    def __init__(self, settings, start_fraction: float, ramp_trades: int):
        self.settings = settings
        self.start_fraction = start_fraction
        self.ramp_trades = ramp_trades
        self._original = {
            "max_capital_per_bundle_eth": settings.max_capital_per_bundle_eth,
            "max_capital_per_bundle_sol": settings.max_capital_per_bundle_sol,
            "copytrade_size_pct": settings.copytrade_size_pct,
            "snipe_size_pct": settings.snipe_size_pct,
        }

    def update(self, trade_log_path: str, chain: str) -> float:
        fraction = compute_canary_fraction(trade_log_path, chain, self.start_fraction, self.ramp_trades)
        if chain == "eth":
            self.settings.max_capital_per_bundle_eth = self._original["max_capital_per_bundle_eth"] * fraction
            self.settings.snipe_size_pct = self._original["snipe_size_pct"] * fraction
        elif chain == "solana":
            self.settings.max_capital_per_bundle_sol = self._original["max_capital_per_bundle_sol"] * fraction
        self.settings.copytrade_size_pct = self._original["copytrade_size_pct"] * fraction
        return fraction
