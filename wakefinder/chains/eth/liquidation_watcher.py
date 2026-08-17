"""Watcher PENDING-вызовов Aave V3 Pool.liquidationCall() в публичном
мемпуле — структурно идентичен liquidity_watcher.py (та же схема: подписка
на все pending-хэши, decode_function_input по calldata целевого контракта),
но целится в Aave Pool, а не в Uniswap V2 роутер.

Discovery-стратегия для chains/eth/liquidate.py — РЕАКТИВНАЯ, не
самостоятельный поиск недообеспеченных позиций: увидеть чужой pending
liquidationCall означает "эта позиция liquidatable ПРЯМО СЕЙЧАС" (кто-то уже
сделал работу по обнаружению), и стратегия пытается сконкурировать за то же
самое включение своей копией вызова. Полный самостоятельный индекс
заёмщиков через историю Borrow/Repay-событий — сознательно НЕ реализован,
на порядок больше объёма работы (та же честная граница, что у Solscan в
wallet_scanner.py)."""

from collections.abc import AsyncIterator

from web3 import AsyncWeb3

from wakefinder.chains.eth.aave_abi import AAVE_POOL_ABI
from wakefinder.common.interfaces import MempoolWatcher, PendingLiquidation


class LiquidationWatcher(MempoolWatcher[PendingLiquidation]):
    def __init__(self, w3: AsyncWeb3, pool_address: str):
        self.w3 = w3
        self.pool = w3.eth.contract(address=pool_address, abi=AAVE_POOL_ABI)
        self._seen: set[str] = set()

    async def watch(self) -> AsyncIterator[PendingLiquidation]:
        sub_id = await self.w3.eth.subscribe("newPendingTransactions")
        async for message in self.w3.ws.process_subscriptions():
            if message.get("subscription") != sub_id:
                continue
            tx_hash = message["result"]
            if tx_hash in self._seen:
                continue
            self._seen.add(tx_hash)

            try:
                tx = await self.w3.eth.get_transaction(tx_hash)
            except Exception:
                continue
            if not tx or not tx.get("to"):
                continue
            if tx["to"].lower() != self.pool.address.lower():
                continue
            try:
                fn, params = self.pool.decode_function_input(tx["input"])
            except Exception:
                continue
            if fn.fn_name != "liquidationCall":
                continue

            yield PendingLiquidation(
                tx_hash=tx_hash.hex(),
                collateral_asset=params["collateralAsset"],
                debt_asset=params["debtAsset"],
                user=params["user"],
                debt_to_cover=params["debtToCover"],
            )
