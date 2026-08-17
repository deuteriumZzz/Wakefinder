"""Watcher PENDING-вызовов SwapRouter02.exactInputSingle() в публичном
мемпуле — структурно идентичен liquidity_watcher.py/liquidation_watcher.py
(та же схема: подписка на все pending-хэши, decode_function_input по
calldata целевого контракта), но целится в SwapRouter02 и фильтрует по
СКОНФИГУРИРОВАННОЙ V3-паре (token0/token1/fee), не по адресу конкретного
пула — сам exactInputSingle не указывает адрес пула напрямую, он выводится
роутером из (tokenIn, tokenOut, fee).

ЧЕСТНАЯ ГРАНИЦА: покрывает ТОЛЬКО exactInputSingle (однохоповые прямые
свопы) — не exactInput (мультихоп через путь из нескольких пулов),
exactOutputSingle/exactOutput (свопы с фиксированным выходом, не входом) —
эти дают тот же экономический эффект для JIT (большой своп через пул), но
декодирование мультихоп-путей и exactOutput — заметно больше кода ради
дополнительного покрытия, отложено сознательно, не тихий недосмотр."""

from collections.abc import AsyncIterator

from web3 import AsyncWeb3

from wakefinder.chains.eth.univ3_abi import SWAP_ROUTER_02_ABI
from wakefinder.common.interfaces import MempoolWatcher, PendingLargeSwap


class V3LargeSwapWatcher(MempoolWatcher[PendingLargeSwap]):
    def __init__(self, w3: AsyncWeb3, swap_router_address: str, token0: str, token1: str, fee: int, min_amount_in: int):
        self.w3 = w3
        self.router = w3.eth.contract(address=swap_router_address, abi=SWAP_ROUTER_02_ABI)
        self._pair = {token0.lower(), token1.lower()}
        self._fee = fee
        self._min_amount_in = min_amount_in
        self._seen: set[str] = set()

    async def watch(self) -> AsyncIterator[PendingLargeSwap]:
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
            if tx["to"].lower() != self.router.address.lower():
                continue
            try:
                fn, params = self.router.decode_function_input(tx["input"])
            except Exception:
                continue
            if fn.fn_name != "exactInputSingle":
                continue

            p = params["params"]
            if p["fee"] != self._fee:
                continue
            if {p["tokenIn"].lower(), p["tokenOut"].lower()} != self._pair:
                continue
            if p["amountIn"] < self._min_amount_in:
                continue

            yield PendingLargeSwap(
                tx_hash=tx_hash.hex(), token_in=p["tokenIn"], token_out=p["tokenOut"], fee=p["fee"], amount_in=p["amountIn"],
            )
