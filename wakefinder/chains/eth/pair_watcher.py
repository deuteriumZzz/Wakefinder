"""Watcher новых пар на Uniswap V2 Factory (событие `PairCreated`) — для
снайпинга свежесозданных пулов, в отличие от watcher.py (который смотрит
PENDING-своп в УЖЕ существующем пуле).

Сознательное упрощение: здесь смотрим уже ПОДТВЕРЖДЁННОЕ событие создания
пары, не pending-tx создания ликвидности (`addLiquidityETH` на роутере).
Реакция на pending-tx и бэкран в том же блоке через Flashbots (тот же
принцип, что chains/eth/main.py для арбитража) была бы быстрее, но это
отдельное усложнение — см. README "Memecoin-снайпинг" про этот компромисс.

Фильтр по адресу Factory достаточен без явного topic-хэша события: Factory
эмитит только `PairCreated`, декодирование — через `process_log` из ABI.
"""

from collections.abc import AsyncIterator

from web3 import AsyncWeb3

from wakefinder.chains.eth.abi import FACTORY_ABI
from wakefinder.common.interfaces import MempoolWatcher, NewPool


class PairCreatedWatcher(MempoolWatcher[NewPool]):
    def __init__(self, w3: AsyncWeb3, factory_address: str):
        self.w3 = w3
        self.factory = w3.eth.contract(address=factory_address, abi=FACTORY_ABI)
        self._seen: set[str] = set()

    async def watch(self) -> AsyncIterator[NewPool]:
        sub_id = await self.w3.eth.subscribe("logs", {"address": self.factory.address})
        async for message in self.w3.ws.process_subscriptions():
            if message.get("subscription") != sub_id:
                continue
            log = message["result"]

            try:
                decoded = self.factory.events.PairCreated().process_log(log)
            except Exception:
                continue  # лог не от PairCreated (не должно случаться при фильтре по address, но не доверяем формату слепо)

            pool_address = decoded["args"]["pair"]
            if pool_address in self._seen:
                continue
            self._seen.add(pool_address)

            yield NewPool(
                tx_hash=decoded["transactionHash"].hex(),
                pool_address=pool_address,
                token0=decoded["args"]["token0"],
                token1=decoded["args"]["token1"],
                block_number=decoded["blockNumber"],
            )
