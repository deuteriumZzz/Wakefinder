"""Watcher мемпула Ethereum: подписывается на хэши pending-транзакций,
расшифровывает calldata свопов роутера Uniswap V2 и отдаёт свопы, прошедшие
любой из двух фильтров: размер (`min_amount_in`) ИЛИ отправитель из
`watched_wallets` (конкретные отслеживаемые кошельки — киты/игроки, список
которых берётся извне: вручную составленный watchlist или внешняя аналитика
типа Nansen/Arkham; сам по себе блокчейн не размечает адреса по "умности").

ponytail: адреса пулов передаёт вызывающий код (pool_registry) — один lookup
по словарю покрывает те пулы, которые реально важны для арбитража. Для
watchlist-триггеров (копитрейдинг обязан следовать за китом в любой токен, не
только заранее зарегистрированный) при промахе по pool_registry и заданном
`factory_address` адрес пула выводится через `getPair()` фабрики Uniswap V2 и
кэшируется — это НЕ включено для size-триггеров, чтобы не менять поведение
уже работающего арбитражного пути.

ponytail: `_seen` — неограниченное множество на весь срок жизни процесса —
нормально для бота, который периодически перезапускают; добавьте LRU-вытеснение,
если для долгоживущего инстанса память станет реальной проблемой.
"""

from collections.abc import AsyncIterator

from web3 import AsyncWeb3

from wakefinder.chains.eth.abi import FACTORY_ABI, ROUTER_ABI
from wakefinder.common.interfaces import MempoolWatcher, PendingSwap

SWAP_FUNCTIONS = {"swapExactTokensForTokens", "swapExactETHForTokens"}
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class UniswapV2Watcher(MempoolWatcher):
    def __init__(
        self,
        w3: AsyncWeb3,
        router_address: str,
        pool_registry: dict[tuple[str, str], str],
        min_amount_in: int,
        watched_wallets: frozenset[str] = frozenset(),
        factory_address: str | None = None,
    ):
        self.w3 = w3
        self.router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
        self.factory = w3.eth.contract(address=factory_address, abi=FACTORY_ABI) if factory_address else None
        self.pool_registry = pool_registry
        self.min_amount_in = min_amount_in
        self.watched_wallets = {a.lower() for a in watched_wallets}
        self._seen: set[str] = set()
        self._factory_cache: dict[tuple[str, str], str | None] = {}

    def _pool_for(self, token_in: str, token_out: str) -> str | None:
        return self.pool_registry.get((token_in.lower(), token_out.lower())) or self.pool_registry.get(
            (token_out.lower(), token_in.lower())
        )

    async def _pool_via_factory(self, token_in: str, token_out: str) -> str | None:
        key = (token_in.lower(), token_out.lower())
        if key in self._factory_cache:
            return self._factory_cache[key]
        pair = await self.factory.functions.getPair(token_in, token_out).call()
        result = None if pair.lower() == ZERO_ADDRESS else pair
        self._factory_cache[key] = result
        return result

    async def watch(self) -> AsyncIterator[PendingSwap]:
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
            if fn.fn_name not in SWAP_FUNCTIONS:
                continue

            amount_in = params.get("amountIn", tx.get("value", 0))
            sender = (tx.get("from") or "").lower()
            is_large_enough = amount_in >= self.min_amount_in
            is_watched_wallet = sender in self.watched_wallets
            if not (is_large_enough or is_watched_wallet):
                continue

            path = params["path"]
            if len(path) != 2:
                # Многохоповые свопы не двигают один пул так, как это моделирует
                # симулятор бота — сопоставление с пулом, которого кит не касался,
                # оценило бы вымышленную неверную цену.
                continue

            pool = self._pool_for(path[0], path[-1])
            if pool is None and is_watched_wallet and self.factory is not None:
                pool = await self._pool_via_factory(path[0], path[-1])
            if pool is None:
                continue

            yield PendingSwap(
                tx_hash=tx_hash.hex(),
                pool_address=pool,
                token_in=path[0],
                token_out=path[-1],
                amount_in=amount_in,
                sender=sender,
            )
