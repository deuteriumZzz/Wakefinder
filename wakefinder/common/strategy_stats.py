"""Sharpe/Sortino ratio + дрифт win-rate ПО СТРАТЕГИИ (arb/copytrade/snipe,
на каждой сети отдельно) — поверх pnl_ledger.py (реализованный PnL закрытых
сделок, common/pnl_ledger.py:read_closed_trades()).

Отличается от circuit breaker'а по просадке (common/drawdown.py): тот
смотрит на АБСОЛЮТНУЮ просадку в native units за скользящее окно и
останавливает торговлю при превышении порога — жёсткий стоп. Это —
риск-adjusted показатель (доходность на единицу волатильности) и
качественный сигнал деградации стратегии (win rate снижается со временем),
информационный, не блокирующий.

Sharpe/Sortino — безразмерные отношения (mean/std), поэтому считаются прямо
в native units (wei/lamports) без конвертации в ETH/SOL/USD — масштаб
сокращается в отношении."""

import statistics
from dataclasses import dataclass


@dataclass
class StrategyStats:
    chain: str
    strategy: str
    trades: int
    win_rate: float
    sharpe: float | None
    sortino: float | None
    win_rate_recent: float
    win_rate_drift: float  # win_rate_recent - win_rate; отрицательное = недавние сделки хуже, чем в среднем


def _sharpe(pnls: list[float]) -> float | None:
    if len(pnls) < 2:
        return None
    std = statistics.stdev(pnls)
    if std == 0:
        return None
    return statistics.mean(pnls) / std


def _sortino(pnls: list[float]) -> float | None:
    """Знаменатель — стандартное отклонение ТОЛЬКО убыточных сделок (downside
    deviation), а не всех — та же идея, что и Sharpe, но не штрафует за
    волатильность прибыли, только за волатильность убытков."""
    if len(pnls) < 2:
        return None
    downside = [p for p in pnls if p < 0]
    if not downside:
        return None  # нет убыточных сделок — Sortino не определён, честно не бесконечность
    downside_std = statistics.pstdev(downside) if len(downside) > 1 else abs(downside[0])
    if downside_std == 0:
        return None
    return statistics.mean(pnls) / downside_std


def compute_strategy_stats(closed_trades: list[dict], recent_window: int = 20) -> dict[tuple[str, str], StrategyStats]:
    """closed_trades — записи read_closed_trades() (chain/strategy/
    realized_pnl), уже в хронологическом порядке (как из pnl_ledger.jsonl).
    Группирует по (chain, strategy)."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for t in closed_trades:
        key = (t.get("chain", ""), t.get("strategy", ""))
        grouped.setdefault(key, []).append(t)

    result: dict[tuple[str, str], StrategyStats] = {}
    for key, trades in grouped.items():
        pnls = [float(t.get("realized_pnl", 0)) for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) if pnls else 0.0
        recent = pnls[-recent_window:]
        recent_wins = sum(1 for p in recent if p > 0)
        win_rate_recent = recent_wins / len(recent) if recent else 0.0
        result[key] = StrategyStats(
            chain=key[0], strategy=key[1], trades=len(pnls), win_rate=win_rate,
            sharpe=_sharpe(pnls), sortino=_sortino(pnls),
            win_rate_recent=win_rate_recent, win_rate_drift=win_rate_recent - win_rate,
        )
    return result


def demo() -> None:
    trades = [
        {"chain": "eth", "strategy": "copytrade", "realized_pnl": 100},
        {"chain": "eth", "strategy": "copytrade", "realized_pnl": -50},
        {"chain": "eth", "strategy": "copytrade", "realized_pnl": 200},
        {"chain": "solana", "strategy": "snipe", "realized_pnl": -10},
    ]
    stats = compute_strategy_stats(trades)
    eth_ct = stats[("eth", "copytrade")]
    assert eth_ct.trades == 3
    assert eth_ct.sharpe is not None
    assert eth_ct.sortino is not None
    sol_snipe = stats[("solana", "snipe")]
    assert sol_snipe.sharpe is None  # только 1 сделка — нет дисперсии
    print("OK")


if __name__ == "__main__":
    demo()
