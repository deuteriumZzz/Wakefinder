"""Тесты run_snipe_backtest — фейковый router.getAmountsOut() возвращает
заранее заданную последовательность котировок по block_identifier, реальный
TrailingStopTracker (импортирован напрямую, не подделан) принимает решения —
проверяем именно интеграцию бэктеста с ЖИВЫМ трекером, не переизобретаем
его тесты (см. test_trailing_stop.py)."""

import asyncio

import pytest

from wakefinder.chains.eth.snipe_backtest import run_snipe_backtest

ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TOKEN = "0x1111111111111111111111111111111111111111"


class _RouterFunctions:
    def __init__(self, quotes_by_block, fail_blocks=()):
        self._quotes_by_block = quotes_by_block  # {block: out_amount}
        self._fail_blocks = set(fail_blocks)

    def getAmountsOut(self, amount_in, path):
        functions = self

        class _Pending:
            async def call(_self, block_identifier=None):
                if block_identifier in functions._fail_blocks:
                    raise RuntimeError("execution reverted")
                out = functions._quotes_by_block[block_identifier]
                return [amount_in, out]
        return _Pending()


class _FakeRouter:
    def __init__(self, quotes_by_block, fail_blocks=()):
        self.functions = _RouterFunctions(quotes_by_block, fail_blocks)


class _FakeEth:
    def __init__(self, router):
        self._router = router

    def contract(self, address, abi):
        return self._router


class _FakeW3:
    def __init__(self, quotes_by_block, fail_blocks=()):
        self.eth = _FakeEth(_FakeRouter(quotes_by_block, fail_blocks))


def test_trailing_stop_triggers_on_drawdown_from_peak():
    # 100 -> 150 (новый пик) -> 120 (держим, >=70% от пика) -> 100 (стоп: <70% от 150=105)
    quotes = {100: 100, 101: 150, 102: 120, 103: 100}
    w3 = _FakeW3(quotes)

    result = asyncio.run(run_snipe_backtest(
        w3, ROUTER, WETH, TOKEN, amount_held=10**18, entry_block=100, to_block=103, trail_pct=30,
    ))

    assert result.stopped_out is True
    assert result.exit_block == 103
    assert result.entry_value_wei == 100
    assert result.exit_value_wei == 100
    assert result.realized_pnl_wei == 0


def test_no_stop_holds_until_to_block():
    quotes = {100: 100, 101: 110, 102: 120}
    w3 = _FakeW3(quotes)

    result = asyncio.run(run_snipe_backtest(
        w3, ROUTER, WETH, TOKEN, amount_held=10**18, entry_block=100, to_block=102, trail_pct=30,
    ))

    assert result.stopped_out is False
    assert result.exit_block == 102
    assert result.realized_pnl_wei == 20


def test_momentum_reversal_triggers_before_trail_pct():
    # 100 -> 150 (пик) -> 100: (150-100)/150=33% >= momentum_reversal_pct=20%, срабатывает momentum
    # обычный trail_pct=50% ещё НЕ пробит (floor=75, 100>=75) -- значит momentum сработал раньше
    quotes = {100: 100, 101: 150, 102: 100}
    w3 = _FakeW3(quotes)

    result = asyncio.run(run_snipe_backtest(
        w3, ROUTER, WETH, TOKEN, amount_held=10**18, entry_block=100, to_block=102,
        trail_pct=50, momentum_reversal_pct=20,
    ))

    assert result.stopped_out is True
    assert result.exit_block == 102


def test_quote_failure_is_skipped_not_treated_as_exit():
    quotes = {100: 100, 102: 110}
    w3 = _FakeW3(quotes, fail_blocks={101})

    result = asyncio.run(run_snipe_backtest(
        w3, ROUTER, WETH, TOKEN, amount_held=10**18, entry_block=100, to_block=102, trail_pct=30,
    ))

    assert result.quote_failures == 1
    assert result.stopped_out is False
    assert result.exit_value_wei == 110


def test_no_entry_quote_raises():
    w3 = _FakeW3({}, fail_blocks={100})
    with pytest.raises(ValueError, match="entry_block"):
        asyncio.run(run_snipe_backtest(w3, ROUTER, WETH, TOKEN, amount_held=10**18, entry_block=100, to_block=105, trail_pct=30))


if __name__ == "__main__":
    test_trailing_stop_triggers_on_drawdown_from_peak()
    test_no_stop_holds_until_to_block()
    test_momentum_reversal_triggers_before_trail_pct()
    test_quote_failure_is_skipped_not_treated_as_exit()
    test_no_entry_quote_raises()
    print("ok")
