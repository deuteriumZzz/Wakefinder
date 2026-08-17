from wakefinder.common.strategy_stats import compute_strategy_stats


def _trade(chain, strategy, pnl):
    return {"chain": chain, "strategy": strategy, "realized_pnl": pnl}


def test_groups_by_chain_and_strategy():
    trades = [
        _trade("eth", "copytrade", 100),
        _trade("eth", "snipe", -20),
        _trade("solana", "copytrade", 50),
    ]
    stats = compute_strategy_stats(trades)
    assert set(stats.keys()) == {("eth", "copytrade"), ("eth", "snipe"), ("solana", "copytrade")}


def test_win_rate_computed_correctly():
    trades = [_trade("eth", "arb", 100), _trade("eth", "arb", -50), _trade("eth", "arb", 200), _trade("eth", "arb", -10)]
    s = compute_strategy_stats(trades)[("eth", "arb")]
    assert s.trades == 4
    assert s.win_rate == 0.5


def test_sharpe_none_with_fewer_than_two_trades():
    s = compute_strategy_stats([_trade("eth", "arb", 100)])[("eth", "arb")]
    assert s.sharpe is None
    assert s.sortino is None


def test_sharpe_none_when_all_pnls_identical():
    trades = [_trade("eth", "arb", 100), _trade("eth", "arb", 100)]
    s = compute_strategy_stats(trades)[("eth", "arb")]
    assert s.sharpe is None  # std == 0


def test_sortino_none_when_no_losing_trades():
    trades = [_trade("eth", "arb", 100), _trade("eth", "arb", 200)]
    s = compute_strategy_stats(trades)[("eth", "arb")]
    assert s.sharpe is not None
    assert s.sortino is None  # нет убыточных сделок


def test_sortino_computed_with_losing_trades():
    trades = [_trade("eth", "arb", 100), _trade("eth", "arb", -50), _trade("eth", "arb", -30)]
    s = compute_strategy_stats(trades)[("eth", "arb")]
    assert s.sortino is not None


def test_win_rate_drift_detects_recent_degradation():
    # 10 старых прибыльных сделок, потом 5 убыточных подряд
    trades = [_trade("eth", "arb", 100) for _ in range(10)] + [_trade("eth", "arb", -10) for _ in range(5)]
    s = compute_strategy_stats(trades, recent_window=5)[("eth", "arb")]
    assert s.win_rate_recent == 0.0
    assert s.win_rate_drift < 0


def test_empty_trades_returns_empty_dict():
    assert compute_strategy_stats([]) == {}
