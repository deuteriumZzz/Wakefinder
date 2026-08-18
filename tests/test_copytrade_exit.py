"""Регрессия для бага, найденного реальным форк-тестом 2026-08-18: раньше
_exit_position сохранял удаление позиции на диск ДО попытки продажи — если
попытка ревертила/бросала исключение, позиция тихо пропадала из
отслеживания навсегда, хотя токен на кошельке никуда не девался. Эти тесты
монkeypatch'ят зависимости самого нижнего уровня (_reserves/
_approve_token/_send_single_swap/get_amount_out), не строят полный
fake-стек w3/account — проверяется именно КОНТРОЛЬ-ФЛОУ (позиция
восстанавливается при неудаче, сохраняется на диск только при успехе),
не механика подписания (та уже покрыта test_tx_signing.py)."""

import asyncio
import json

import wakefinder.chains.eth.copytrade as copytrade
from wakefinder.chains.eth.copytrade import Position, _exit_position, _load_positions
from wakefinder.common.config import get_settings

TOKEN = "0xTOKEN"
TOKEN_IN = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
POOL = "0xPOOL"
WALLET = "0xWHALE"


def _position():
    return Position(
        token=TOKEN, token_in=TOKEN_IN, pool_address=POOL, amount_held=10**18,
        entry_amount_in=10**17, watched_wallet=WALLET, opened_at=1000.0,
    )


async def _fake_reserves_ok(w3, pool_address, token_in):
    return 10**18, 10**18


async def _await_none(*args, **kwargs):
    return None


def _run_exit(monkeypatch, tmp_path, send_single_swap_fn):
    positions_file = str(tmp_path / "positions.json")
    positions = {TOKEN.lower(): _position()}
    with open(positions_file, "w") as f:
        json.dump({k: vars(v) for k, v in positions.items()}, f)

    monkeypatch.setattr(copytrade, "_reserves", _fake_reserves_ok)
    monkeypatch.setattr(copytrade, "_approve_token", _await_none)
    monkeypatch.setattr(copytrade, "_send_single_swap", send_single_swap_fn)

    settings = get_settings()
    settings.exit_via_cowswap = False
    trade_log_file = str(tmp_path / "trades.jsonl")

    positions_lock = asyncio.Lock()
    asyncio.run(_exit_position(
        w3=None, account=None, router_address="0xROUTER", chain_id=1, token=TOKEN, reason="тест",
        positions=positions, positions_lock=positions_lock, positions_file=positions_file, trade_log_file=trade_log_file,
    ))
    return positions, positions_file


def test_failed_exit_restores_position_in_memory_and_leaves_disk_untouched(monkeypatch, tmp_path):
    async def _failing_swap(*args, **kwargs):
        return False, ""  # included=False — своп не прошёл (revert/не попал в блок)

    positions, positions_file = _run_exit(monkeypatch, tmp_path, _failing_swap)

    assert TOKEN.lower() in positions  # позиция вернулась в память
    on_disk = _load_positions(positions_file)
    assert TOKEN.lower() in on_disk  # и осталась на диске — не терялась ни на миг


def test_exception_during_exit_restores_position(monkeypatch, tmp_path):
    async def _raising_swap(*args, **kwargs):
        raise RuntimeError("симуляция сетевого сбоя посреди свопа")

    positions, positions_file = _run_exit(monkeypatch, tmp_path, _raising_swap)

    assert TOKEN.lower() in positions
    on_disk = _load_positions(positions_file)
    assert TOKEN.lower() in on_disk


def test_successful_exit_removes_position_from_disk(monkeypatch, tmp_path):
    async def _successful_swap(*args, **kwargs):
        return True, "0xTXHASH"

    positions, positions_file = _run_exit(monkeypatch, tmp_path, _successful_swap)

    assert TOKEN.lower() not in positions
    on_disk = _load_positions(positions_file)
    assert TOKEN.lower() not in on_disk


if __name__ == "__main__":
    print("run via pytest (uses tmp_path/monkeypatch fixtures)")
