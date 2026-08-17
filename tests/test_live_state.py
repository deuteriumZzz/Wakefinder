import asyncio
import json

import wakefinder.live_state as live_state
from wakefinder.common.pnl_ledger import record_closed_trade
from wakefinder.live_state import (
    eth_copytrade_positions_live,
    eth_snipe_positions_live,
    gather_state,
    pnl_history_view,
    render_prometheus,
    solana_copytrade_positions_live,
)

TOKEN = "0xTOKEN"
TOKEN_IN = "0xWETH"
ROUTER = "0xROUTER"


class _Awaitable:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _coro():
            return self.value
        return _coro().__await__()


class _Callable:
    def __init__(self, value):
        self.value = value

    def call(self):
        return _Awaitable(self.value)


class _RaisingCallable:
    def call(self):
        raise RuntimeError("нет ликвидности — rug/dead pool")


class _RouterFunctions:
    def __init__(self, sell_out):
        self._sell_out = sell_out

    def getAmountsOut(self, amount_in, path):
        if self._sell_out is None:
            return _RaisingCallable()
        return _Callable([amount_in, self._sell_out])


class FakeW3:
    def __init__(self, sell_out):
        self._router = type("R", (), {"functions": _RouterFunctions(sell_out)})()

    @property
    def eth(self):
        return self

    def contract(self, address, abi):
        return self._router


class FakeJupiter:
    def __init__(self, out_amount):
        self._out_amount = out_amount

    async def quote(self, input_mint, output_mint, amount, slippage_bps, only_direct_routes):
        if self._out_amount is None:
            raise RuntimeError("нет маршрута")
        return {"outAmount": str(self._out_amount)}


def test_eth_copytrade_position_live_computes_pnl():
    positions = {TOKEN: {"entry_amount_in": 10**18, "amount_held": 5 * 10**17, "token_in": TOKEN_IN, "watched_wallet": "0xWHALE"}}
    w3 = FakeW3(sell_out=15 * 10**17)  # текущая стоимость выше входа -> прибыль
    result = asyncio.run(eth_copytrade_positions_live(w3, ROUTER, positions))
    assert len(result) == 1
    assert result[0]["current_value"] == 1.5
    assert result[0]["entry_amount_in"] == 1.0
    assert abs(result[0]["pnl_pct"] - 50.0) < 1e-9
    assert result[0]["watched_wallet"] == "0xWHALE"


def test_eth_copytrade_position_live_handles_dead_pool():
    positions = {TOKEN: {"entry_amount_in": 10**18, "amount_held": 5 * 10**17, "token_in": TOKEN_IN}}
    w3 = FakeW3(sell_out=None)  # getAmountsOut ревертит — rug/высохшая ликвидность
    result = asyncio.run(eth_copytrade_positions_live(w3, ROUTER, positions))
    assert result[0]["current_value"] is None
    assert result[0]["pnl_pct"] is None


def test_eth_snipe_position_live_uses_wei_field_name():
    positions = {TOKEN: {"entry_amount_in_wei": 2 * 10**18, "amount_held": 10**18}}
    w3 = FakeW3(sell_out=10**18)  # без изменения цены
    result = asyncio.run(eth_snipe_positions_live(w3, ROUTER, TOKEN_IN, positions))
    assert result[0]["entry_amount_in"] == 2.0
    assert result[0]["current_value"] == 1.0
    assert abs(result[0]["pnl_pct"] - (-50.0)) < 1e-9


def test_solana_copytrade_position_live_computes_pnl():
    positions = {TOKEN: {"entry_amount_in": 10**9, "amount_held": 5 * 10**8, "token_in": "So1111...", "watched_wallet": "wallet1"}}
    jupiter = FakeJupiter(out_amount=8 * 10**8)  # ниже входа -> убыток
    result = asyncio.run(solana_copytrade_positions_live(jupiter, positions))
    assert result[0]["current_value"] == 0.8
    assert abs(result[0]["pnl_pct"] - (-20.0)) < 1e-9


class _SecretStub:
    def __init__(self, value):
        self._value = value

    def get_secret_value(self):
        return self._value


class _FakeSettings:
    def __init__(self, tmp_path):
        self.kill_switch_file = str(tmp_path / "kill")
        self.heartbeat_dir = str(tmp_path)
        self.trade_log_file = str(tmp_path / "trades.jsonl")
        self.pnl_ledger_file = str(tmp_path / "pnl_ledger.jsonl")
        self.copytrade_positions_file = str(tmp_path / "positions.json")
        self.snipe_positions_file = str(tmp_path / "positions_snipe.json")
        self.solana_copytrade_positions_file = str(tmp_path / "positions_solana.json")
        self.eth_rpc_http_url = _SecretStub("https://unreachable.invalid")
        self.eth_router_address = "0xROUTER"
        self.eth_weth_address = "0xWETH"
        self.solana_rpc_http_url = None  # ветка Solana пропускается целиком (short-circuit на and)

    def resolved_eth_private_key(self):
        return "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004"


class _DeadRouterFunctions:
    def getAmountsOut(self, amount_in, path):
        return _RaisingCallable()


class _RaisingW3:
    """Симулирует недоступный RPC без реального сетевого запроса в тестах.
    contract() — локальная операция и у настоящего AsyncWeb3 (не ходит в
    сеть), поэтому здесь она тоже успешна; падает именно вызов балансов и
    котировок — та же асимметрия, что и в реальности, которую и ловит
    test_gather_state_still_shows_positions_despite_eth_rpc_failure ниже."""

    def __init__(self, provider):
        pass

    @property
    def eth(self):
        return self

    async def get_balance(self, address):
        raise RuntimeError("нет соединения с RPC")

    def contract(self, address, abi):
        return type("R", (), {"functions": _DeadRouterFunctions()})()


def test_gather_state_reports_kill_switch_despite_eth_rpc_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(live_state, "AsyncWeb3", _RaisingW3)
    monkeypatch.setattr(live_state, "fetch_usd_prices", lambda: {})

    settings = _FakeSettings(tmp_path)
    with open(settings.kill_switch_file, "w") as f:
        f.write("test")

    state = asyncio.run(gather_state(settings))
    assert state["kill_switch_engaged"] is True
    assert state["eth"]["balance"] is None
    assert "eth_error" in state
    assert state["solana"]["address"] is None


def test_gather_state_still_shows_positions_despite_eth_rpc_failure(tmp_path, monkeypatch):
    """Регрессия: раньше сбой ЗАПРОСА БАЛАНСА обрывал весь ETH-блок ДО того,
    как позиции вообще успевали загрузиться из файла — теперь загрузка из
    файла не зависит от того, удался ли отдельный RPC-вызов баланса."""
    monkeypatch.setattr(live_state, "AsyncWeb3", _RaisingW3)
    monkeypatch.setattr(live_state, "fetch_usd_prices", lambda: {})

    settings = _FakeSettings(tmp_path)
    with open(settings.copytrade_positions_file, "w") as f:
        json.dump({TOKEN: {"entry_amount_in": 10**18, "amount_held": 5 * 10**17, "token_in": TOKEN_IN, "watched_wallet": "0xWHALE"}}, f)

    state = asyncio.run(gather_state(settings))
    assert state["eth"]["balance"] is None  # RPC действительно недоступен
    positions = state["eth"]["copytrade_positions"]
    assert len(positions) == 1
    assert positions[0]["entry_amount_in"] == 1.0  # из файла, без RPC
    assert positions[0]["current_value"] is None  # живую оценку получить не удалось — честно, не нулём


def test_pnl_history_view_converts_to_native_units_and_orders_newest_first(tmp_path):
    path = str(tmp_path / "pnl.jsonl")
    record_closed_trade(path, "eth", "copytrade", 10**18, token="0xTOKEN", wallet="0xWHALE", opened_at=1000.0)
    record_closed_trade(path, "solana", "arb", 2 * 10**9)
    rows = pnl_history_view(path, prices={"eth": 3000.0, "sol": 150.0})
    assert len(rows) == 2
    assert rows[0]["chain"] == "solana"  # последняя запись первой
    assert rows[0]["realized_pnl"] == 2.0
    assert rows[0]["realized_pnl_usd"] == 300.0
    assert rows[1]["realized_pnl"] == 1.0
    assert rows[1]["realized_pnl_usd"] == 3000.0
    assert rows[1]["holding_seconds"] > 0


def test_pnl_history_view_omits_usd_without_price(tmp_path):
    path = str(tmp_path / "pnl.jsonl")
    record_closed_trade(path, "eth", "arb", 10**18)
    rows = pnl_history_view(path, prices={})
    assert rows[0]["realized_pnl_usd"] is None


def test_pnl_history_view_missing_file_returns_empty(tmp_path):
    assert pnl_history_view(str(tmp_path / "nope.jsonl"), prices={}) == []


_PROM_STATE = {
    "kill_switch_engaged": False,
    "heartbeats": [{"process": "eth_arb", "age_seconds": 12.3, "stale": False}, {"process": "eth_copytrade", "age_seconds": None, "stale": True}],
    "metrics": {"eth": {"total_attempts": 10, "included": 8, "fill_rate": 0.8, "avg_expected_profit": 100000.0, "avg_realized_profit": 90000.0, "simulation_accuracy": 0.9, "avg_latency_ms": 142.5, "median_latency_ms": 120.0}},
    "eth": {"balance": 1.5},
    "solana": {"balance": None},
}


def test_render_prometheus_includes_kill_switch_gauge():
    text = render_prometheus(_PROM_STATE)
    assert "wakefinder_kill_switch_engaged 0" in text


def test_render_prometheus_includes_chain_labeled_metrics():
    text = render_prometheus(_PROM_STATE)
    assert 'wakefinder_fill_rate{chain="eth"} 0.8' in text
    assert 'wakefinder_trade_attempts_total{chain="eth"} 10' in text


def test_render_prometheus_includes_heartbeat_gauges_with_process_label():
    text = render_prometheus(_PROM_STATE)
    assert 'wakefinder_heartbeat_age_seconds{process="eth_arb"} 12.3' in text
    assert 'wakefinder_heartbeat_stale{process="eth_copytrade"} 1' in text


def test_render_prometheus_omits_none_values_not_crashes():
    text = render_prometheus(_PROM_STATE)
    assert "wakefinder_sol_balance" not in text  # solana.balance is None
    assert "wakefinder_heartbeat_age_seconds{process=\"eth_copytrade\"}" not in text  # age_seconds is None for this process


def test_render_prometheus_includes_latency_gauges():
    text = render_prometheus(_PROM_STATE)
    assert 'wakefinder_avg_latency_ms{chain="eth"} 142.5' in text
    assert 'wakefinder_median_latency_ms{chain="eth"} 120.0' in text


def test_render_prometheus_has_help_and_type_lines():
    text = render_prometheus(_PROM_STATE)
    assert "# HELP wakefinder_fill_rate" in text
    assert "# TYPE wakefinder_fill_rate gauge" in text


if __name__ == "__main__":
    test_eth_copytrade_position_live_computes_pnl()
    test_eth_copytrade_position_live_handles_dead_pool()
    test_eth_snipe_position_live_uses_wei_field_name()
    test_solana_copytrade_position_live_computes_pnl()
    print("ok")
