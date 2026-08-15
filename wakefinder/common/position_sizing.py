"""Корректировка размера копитрейд-позиции по историческому win rate
конкретного watched-кошелька (wallet_stats.py) — не полноценный Kelly
criterion (тот требует парных P&L по каждой сделке; у нас только
агрегированные суммы входов/выходов на кошелёк, см. docstring
wallet_stats.py про то же ограничение точности), а честный более простой
прокси: кошелёк, который исторически выигрывает чаще половины, получает
БОЛЬШЕ размера на новый вход; который реже — МЕНЬШЕ. Множитель вокруг 1.0,
умножается на COPYTRADE_SIZE_PCT, не абсолютный размер сам по себе.

Не корректирует размер, пока не накопилось min_trades подтверждённых
выходов по этому кошельку — на малой выборке win_rate статистически
бессмысленен (1 из 1 прибыльных сделок — это не "100% win rate", это n=1)."""


def win_rate_size_multiplier(
    win_rate: float,
    sample_size: int,
    min_trades: int = 5,
    min_multiplier: float = 0.25,
    max_multiplier: float = 1.5,
) -> float:
    if sample_size < min_trades:
        return 1.0  # недостаточно данных — не корректируем размер вслепую
    if win_rate >= 0.5:
        t = (win_rate - 0.5) / 0.5
        return 1.0 + t * (max_multiplier - 1.0)
    t = win_rate / 0.5
    return min_multiplier + t * (1.0 - min_multiplier)
