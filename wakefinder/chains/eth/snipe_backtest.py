"""Бэктест EXIT-логики снайпинга (trailing-stop/momentum-reversal) на
исторических ценах — реплей ЖИВОГО TrailingStopTracker
(common/trailing_stop.py), тот же принцип, что backtest.py для арбитража:
тестируем код, который реально исполняется, не отдельную копию.

ЧЕСТНАЯ ГРАНИЦА: бэктестит ТОЛЬКО exit после уже случившегося входа
(token_address/entry_block — вход оператора, не результат прогона
entry-фильтров). ENTRY-фильтры снайпинга (deployer reputation, on-chain
momentum confirmation, social signal) сознательно НЕ реплеятся — deployer
reputation/social signal дёргают внешние API без исторических данных
("как выглядело N часов назад" не то же самое, что "сейчас"), momentum
confirmation требует реконструкции точного состояния мемпула на момент
создания пула, которого у публичного RPC нет. Значит этот бэктест отвечает
на "если бы я вошёл здесь — как сработал бы стоп", не на "нашёл бы я вообще
эту возможность".

Цена на каждом историческом блоке — router.getAmountsOut() с
block_identifier=block_number, тот же вызов, что chains/eth/snipe.py делает
live (_exit_position) — не отдельная реконструкция резервов пула."""

import logging
from dataclasses import dataclass

from web3 import AsyncWeb3

from wakefinder.chains.eth.abi import ROUTER_ABI
from wakefinder.common.trailing_stop import TrailingStopTracker

logger = logging.getLogger("wakefinder.eth.snipe_backtest")


@dataclass
class SnipeBacktestResult:
    entry_block: int
    exit_block: int
    entry_value_wei: int
    exit_value_wei: int
    realized_pnl_wei: int
    stopped_out: bool  # True = trailing-stop/momentum сработал до to_block; False = держали до конца окна (неизвестно, что было бы дальше)
    quote_failures: int = 0  # сколько раз getAmountsOut упал (высохшая ликвидность/rug) — пропущены, не считаются срабатыванием стопа


async def run_snipe_backtest(
    w3: AsyncWeb3,
    router_address: str,
    weth_address: str,
    token_address: str,
    amount_held: int,
    entry_block: int,
    to_block: int,
    trail_pct: float,
    momentum_reversal_pct: float | None = None,
    check_every_n_blocks: int = 1,
) -> SnipeBacktestResult:
    router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
    tracker = TrailingStopTracker(trail_pct=trail_pct, momentum_reversal_pct=momentum_reversal_pct)

    async def _quote(block_number: int) -> int | None:
        try:
            amounts = await router.functions.getAmountsOut(amount_held, [token_address, weth_address]).call(block_identifier=block_number)
            return amounts[-1]
        except Exception as exc:
            logger.info("getAmountsOut не удался на блоке %d (%s) — пропуск, вероятно высохшая ликвидность/rug", block_number, type(exc).__name__)
            return None

    entry_value = await _quote(entry_block)
    if entry_value is None:
        raise ValueError(f"нет котировки на entry_block={entry_block} — токен уже неликвиден в момент входа, бэктест невозможен")
    tracker.update(entry_value)

    last_value = entry_value
    quote_failures = 0
    block = entry_block + check_every_n_blocks
    while block <= to_block:
        value = await _quote(block)
        if value is None:
            quote_failures += 1
            block += check_every_n_blocks
            continue
        last_value = value
        if tracker.update(value):
            return SnipeBacktestResult(
                entry_block=entry_block, exit_block=block, entry_value_wei=entry_value, exit_value_wei=value,
                realized_pnl_wei=value - entry_value, stopped_out=True, quote_failures=quote_failures,
            )
        block += check_every_n_blocks

    return SnipeBacktestResult(
        entry_block=entry_block, exit_block=to_block, entry_value_wei=entry_value, exit_value_wei=last_value,
        realized_pnl_wei=last_value - entry_value, stopped_out=False, quote_failures=quote_failures,
    )
