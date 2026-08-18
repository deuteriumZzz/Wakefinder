"""Оркестрация _handle_pending_swap — простые фейки для pool/npm/sender/w3
(не реальный Web3-энкодер, тот проверяется отдельно live-скриптом на
mint()/multicall()/событиях, см. коммит) — здесь проверяется УПРАВЛЯЮЩАЯ
ЛОГИКА: bundle не включён -> лог промаха, ничего не выводим;
bundle включён -> вывод по tokenId из receipt, PnL по WETH-стороне."""

import asyncio
import os
import tempfile

from wakefinder.chains.eth import jit_liquidity as jit
from wakefinder.common.config import get_settings
from wakefinder.common.interfaces import PendingLargeSwap

TOKEN0 = "0x1111111111111111111111111111111111111111"
TOKEN1 = "0x2222222222222222222222222222222222222222"


class _Awaitable:
    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _get():
            return self._value
        return _get().__await__()


class _Call:
    def __init__(self, value):
        self._value = value

    async def call(self):
        return self._value


def _tick_to_sqrt(tick):
    from wakefinder.common.univ3_math import tick_to_sqrt_price_x96
    return tick_to_sqrt_price_x96(tick)


class _FakePoolFunctions:
    def __init__(self, tick):
        self._tick = tick

    def slot0(self):
        return _Call((_tick_to_sqrt(self._tick), self._tick, 0, 0, 0, 0, True))


class _FakePool:
    def __init__(self, tick):
        self.functions = _FakePoolFunctions(tick)


class _FakeBuiltTx:
    def build_transaction(self, tx):
        return {**tx, "to": "0xNPM", "data": "0xmint"}


class _FakeNPMFunctions:
    def mint(self, params):
        return _FakeBuiltTx()

    def multicall(self, data):
        return _FakeBuiltTx()


class _FakeIncreaseLiquidityEvent:
    def __init__(self, token_id):
        self._token_id = token_id

    def process_receipt(self, receipt):
        return [{"args": {"tokenId": self._token_id, "liquidity": 999, "amount0": 0, "amount1": 0}}]


class _FakeCollectEvent:
    def __init__(self, amount0, amount1):
        self._amount0 = amount0
        self._amount1 = amount1

    def process_receipt(self, receipt):
        return [{"args": {"amount0": self._amount0, "amount1": self._amount1}}]


class _FakeNPMEvents:
    def __init__(self, token_id, collected0, collected1):
        self.IncreaseLiquidity = lambda: _FakeIncreaseLiquidityEvent(token_id)
        self.Collect = lambda: _FakeCollectEvent(collected0, collected1)


class _FakeNPM:
    def __init__(self, token_id=42, collected0=0, collected1=0):
        self.functions = _FakeNPMFunctions()
        self.events = _FakeNPMEvents(token_id, collected0, collected1)

    def encode_abi(self, fn_name, args):
        return "0x" + fn_name.encode().hex()


class _FakeAccount:
    address = "0x4444444444444444444444444444444444444444"

    def sign_transaction(self, tx):
        class _Signed:
            rawTransaction = b"\x01\x02\x03"
        return _Signed()


class _FakeEth:
    def __init__(self, npm_receipt=None):
        self._npm_receipt = npm_receipt or {"status": 1}
        self.withdraw_sent = False

    async def get_transaction_count(self, addr, tag):
        return 1

    async def get_block(self, tag):
        return {"baseFeePerGas": 10**9}

    @property
    def block_number(self):
        return _Awaitable(100)

    async def get_raw_transaction(self, tx_hash):
        return b"\xde\xad\xbe\xef"

    async def send_raw_transaction(self, raw):
        self.withdraw_sent = True
        return b"\xaa" * 32

    async def wait_for_transaction_receipt(self, tx_hash, timeout):
        return self._npm_receipt


class _FakeW3:
    def __init__(self, npm_receipt=None):
        self.eth = _FakeEth(npm_receipt)


class _FakeSender:
    def __init__(self, included):
        self._included = included
        self.sent_bundle = None

    async def send(self, bundle):
        self.sent_bundle = bundle
        return self._included


def _settings():
    s = get_settings()
    s.jit_tick_range_half_width = 100
    s.jit_slippage_bps = 100
    return s


def test_bundle_not_included_logs_miss_and_returns(monkeypatch):
    monkeypatch.setattr(jit.Web3, "keccak", staticmethod(lambda raw: b"\x00" * 32))
    settings = _settings()
    with tempfile.TemporaryDirectory() as d:
        settings.trade_log_file = os.path.join(d, "trades.jsonl")
        settings.pnl_ledger_file = os.path.join(d, "pnl.jsonl")

        w3 = _FakeW3()
        sender = _FakeSender(included=False)
        pending = PendingLargeSwap(tx_hash="0xVICTIM", token_in=TOKEN0, token_out=TOKEN1, fee=3000, amount_in=100 * 10**18)

        result = asyncio.run(jit._handle_pending_swap(
            w3, _FakeAccount(), 1, settings, sender, _FakePool(tick=0), _FakeNPM(), TOKEN0, TOKEN1, 3000, 60,
            "token0", 10**18, 10**18, pending,
        ))

        assert result is False  # для consecutive_failures в run() — не включённый бандл считается неудачей
        assert sender.sent_bundle is not None
        assert w3.eth.withdraw_sent is False  # не включён -> withdraw не должен вызываться

        with open(settings.trade_log_file) as f:
            import json
            record = json.loads(f.readline())
            assert record["included"] is False


def test_bundle_included_withdraws_and_records_weth_side_profit(monkeypatch):
    monkeypatch.setattr(jit.Web3, "keccak", staticmethod(lambda raw: b"\x00" * 32))
    settings = _settings()
    with tempfile.TemporaryDirectory() as d:
        settings.trade_log_file = os.path.join(d, "trades.jsonl")
        settings.pnl_ledger_file = os.path.join(d, "pnl.jsonl")

        w3 = _FakeW3()
        sender = _FakeSender(included=True)
        # token0=WETH: собрали 10**18 + 50 wei "прибыли" по WETH-стороне
        npm = _FakeNPM(token_id=7, collected0=10**18 + 50, collected1=0)
        pending = PendingLargeSwap(tx_hash="0xVICTIM", token_in=TOKEN0, token_out=TOKEN1, fee=3000, amount_in=100 * 10**18)

        result = asyncio.run(jit._handle_pending_swap(
            w3, _FakeAccount(), 1, settings, sender, _FakePool(tick=0), npm, TOKEN0, TOKEN1, 3000, 60,
            "token0", 10**18, 10**18, pending,
        ))

        assert result is True  # для consecutive_failures в run() — сбрасывает счётчик
        assert w3.eth.withdraw_sent is True

        from wakefinder.common.pnl_ledger import read_closed_trades
        rows = read_closed_trades(settings.pnl_ledger_file)
        assert len(rows) == 1
        assert rows[0]["strategy"] == "jit_liquidity"
        # profit_wei = weth_collected - weth_used; weth_used <= amount0_desired (10**18), collected = 10**18+50
        assert rows[0]["realized_pnl"] > 0


if __name__ == "__main__":
    test_bundle_not_included_logs_miss_and_returns()
    test_bundle_included_withdraws_and_records_weth_side_profit()
    print("ok")
