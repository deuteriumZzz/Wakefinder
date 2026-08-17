"""Watcher PENDING-вызовов addLiquidityETH на роутере — для backrun-снайпинга
(chains/eth/snipe.py, SNIPE_BACKRUN_MODE), в отличие от pair_watcher.py
(смотрит уже смайненное событие PairCreated). Структурно почти идентичен
watcher.py (UniswapV2Watcher) — та же схема "подписка на все pending-хэши,
decode_function_input по calldata роутера" — но декодирует другую функцию и
не нуждается в pool_registry: пул ещё не существует, целевой токен и есть
единственный идентификатор возможности.

Uniswap V2 Router02.addLiquidityETH(token, amountTokenDesired, amountTokenMin,
amountETHMin, to, deadline) payable — router САМ вызывает factory.createPair()
внутри, если пары ещё нет, и добавляет начальную ликвидность — это тот самый
"запуск токена" на практике: одна pending-транзакция создателя, которую
снайпер хочет забэкранить в том же блоке, а не ждать, пока она замайнится и
эмитит PairCreated (см. docstring pair_watcher.py про это compromise).

Честное ограничение: addLiquidity (без ETH, ERC20/ERC20-пары) не покрыт —
снайпинг во всём проекте работает только с WETH-котируемыми парами (см.
snipe_filter.py: WETH_PATH_UNSUPPORTED), addLiquidityETH — единственный
релевантный путь запуска токена под это ограничение.
"""

from collections.abc import AsyncIterator

from web3 import AsyncWeb3

from wakefinder.chains.eth.abi import ROUTER_ABI
from wakefinder.common.interfaces import MempoolWatcher, PendingLiquidityAdd


class LiquidityAddWatcher(MempoolWatcher):
    def __init__(self, w3: AsyncWeb3, router_address: str, min_amount_eth: int):
        self.w3 = w3
        self.router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
        self.min_amount_eth = min_amount_eth
        self._seen: set[str] = set()

    async def watch(self) -> AsyncIterator[PendingLiquidityAdd]:
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
            if fn.fn_name != "addLiquidityETH":
                continue

            amount_eth = tx.get("value", 0)
            if amount_eth < self.min_amount_eth:
                continue

            yield PendingLiquidityAdd(
                tx_hash=tx_hash.hex(),
                token=params["token"],
                amount_token_desired=params["amountTokenDesired"],
                amount_eth=amount_eth,
            )
