"""Тесты common/portfolio.py — чистые агрегационные функции проверяются на
реальных структурах данных (тот же формат, что pnl_ledger.py пишет).
fetch_wallet_balances (единственная функция, ходящая в сеть) проверяется
через monkeypatch конструкторов AsyncWeb3/AsyncClient — тот же приём, что
tests/test_protected_rpc.py."""

import asyncio
import os
import tempfile

from wakefinder.common.pnl_ledger import record_closed_trade
from wakefinder.common.portfolio import (
    aggregate_capital,
    aggregate_realized_pnl,
    fetch_wallet_balances,
    parse_portfolio_wallets,
    portfolio_summary,
)

PRICES = {"eth": 3000.0, "sol": 150.0}


def test_parse_portfolio_wallets_valid():
    result = parse_portfolio_wallets("eth_arb:eth:0x1111,sol_snipe:solana:ABC123")
    assert result == [
        {"label": "eth_arb", "chain": "eth", "address": "0x1111"},
        {"label": "sol_snipe", "chain": "solana", "address": "ABC123"},
    ]


def test_parse_portfolio_wallets_empty():
    assert parse_portfolio_wallets("") == []


def test_parse_portfolio_wallets_skips_malformed_entry():
    result = parse_portfolio_wallets("good:eth:0x1111,badentry,also:bad:too:many:parts")
    assert result == [{"label": "good", "chain": "eth", "address": "0x1111"}]


def test_aggregate_realized_pnl_sums_by_chain_and_strategy():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "pnl.jsonl")
        record_closed_trade(path, "eth", "arb", 10**18)  # +1 ETH
        record_closed_trade(path, "eth", "arb", 5 * 10**17)  # +0.5 ETH
        record_closed_trade(path, "eth", "liquidate", -2 * 10**17)  # -0.2 ETH
        record_closed_trade(path, "solana", "arb", 2 * 10**9)  # +2 SOL

        result = aggregate_realized_pnl(path, PRICES)

        by_key = {(b["chain"], b["strategy"]): b for b in result["breakdown"]}
        assert by_key[("eth", "arb")]["realized_pnl"] == 1.5
        assert by_key[("eth", "arb")]["realized_pnl_usd"] == 1.5 * 3000.0
        assert by_key[("eth", "liquidate")]["realized_pnl"] == -0.2
        assert by_key[("solana", "arb")]["realized_pnl"] == 2.0
        assert result["complete"] is True
        expected_total = 1.5 * 3000.0 + (-0.2) * 3000.0 + 2.0 * 150.0
        assert abs(result["total_realized_pnl_usd"] - expected_total) < 1e-6


def test_aggregate_realized_pnl_incomplete_when_price_missing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "pnl.jsonl")
        record_closed_trade(path, "eth", "arb", 10**18)
        result = aggregate_realized_pnl(path, prices={})  # нет цен вообще
        assert result["complete"] is False


def test_aggregate_capital_sums_wallets():
    wallets = [
        {"label": "a", "chain": "eth", "address": "0x1", "balance": 2.0},
        {"label": "b", "chain": "solana", "address": "S1", "balance": 10.0},
    ]
    result = aggregate_capital(wallets, PRICES)
    assert result["complete"] is True
    assert result["total_capital_usd"] == 2.0 * 3000.0 + 10.0 * 150.0


def test_aggregate_capital_empty_wallets_is_incomplete():
    result = aggregate_capital([], PRICES)
    assert result["complete"] is False
    assert result["total_capital_usd"] == 0.0


def test_aggregate_capital_missing_balance_marks_incomplete_but_sums_rest():
    wallets = [
        {"label": "a", "chain": "eth", "address": "0x1", "balance": 2.0},
        {"label": "b", "chain": "eth", "address": "0x2", "balance": None},  # RPC не удался
    ]
    result = aggregate_capital(wallets, PRICES)
    assert result["complete"] is False
    assert result["total_capital_usd"] == 2.0 * 3000.0  # частичная сумма всё же посчитана


def test_portfolio_summary_combines_both():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "pnl.jsonl")
        record_closed_trade(path, "eth", "arb", 10**18)
        wallets = [{"label": "a", "chain": "eth", "address": "0x1", "balance": 5.0}]
        summary = portfolio_summary(path, wallets, PRICES)
        assert "pnl" in summary and "capital" in summary
        assert summary["capital"]["total_capital_usd"] == 5.0 * 3000.0


class _FakeEth:
    def __init__(self, balances):
        self._balances = balances

    async def get_balance(self, address):
        return self._balances[address]


class _FakeW3:
    def __init__(self, balances):
        self.eth = _FakeEth(balances)


def test_fetch_wallet_balances_eth(monkeypatch):
    wallets = [{"label": "a", "chain": "eth", "address": "0x1111111111111111111111111111111111111111"}]

    def _fake_async_web3(provider):
        return _FakeW3({"0x1111111111111111111111111111111111111111": 5 * 10**18})

    import web3
    monkeypatch.setattr(web3, "AsyncWeb3", _fake_async_web3)

    result = asyncio.run(fetch_wallet_balances(wallets, eth_rpc_http_url="http://fake", solana_rpc_http_url=None))
    assert len(result) == 1
    assert result[0]["balance"] == 5.0


def test_fetch_wallet_balances_no_rpc_url_gives_none_balance():
    wallets = [{"label": "a", "chain": "eth", "address": "0x1111111111111111111111111111111111111111"}]
    result = asyncio.run(fetch_wallet_balances(wallets, eth_rpc_http_url=None, solana_rpc_http_url=None))
    assert result == [{"label": "a", "chain": "eth", "address": "0x1111111111111111111111111111111111111111", "balance": None}]


if __name__ == "__main__":
    test_parse_portfolio_wallets_valid()
    test_parse_portfolio_wallets_empty()
    test_parse_portfolio_wallets_skips_malformed_entry()
    test_aggregate_realized_pnl_sums_by_chain_and_strategy()
    test_aggregate_realized_pnl_incomplete_when_price_missing()
    test_aggregate_capital_sums_wallets()
    test_aggregate_capital_empty_wallets_is_incomplete()
    test_aggregate_capital_missing_balance_marks_incomplete_but_sums_rest()
    test_portfolio_summary_combines_both()
    test_fetch_wallet_balances_no_rpc_url_gives_none_balance()
    print("ok")
