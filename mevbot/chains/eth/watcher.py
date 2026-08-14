"""Ethereum mempool watcher: subscribes to pending tx hashes, decodes Uniswap V2
router swap calldata, and yields whale swaps above a configurable size.

ponytail: pool addresses are supplied by the caller (pool_registry), not derived
via the factory CREATE2 salt — one dict lookup covers the pools you actually
care about; add factory-based derivation if you need to watch arbitrary pairs.

ponytail: `_seen` is an unbounded set for the life of the process — fine for a
bot you restart periodically; add LRU eviction if a long-running instance's
memory becomes a real concern.
"""

from collections.abc import AsyncIterator

from web3 import AsyncWeb3

from mevbot.chains.eth.abi import ROUTER_ABI
from mevbot.common.interfaces import MempoolWatcher, PendingSwap

SWAP_FUNCTIONS = {"swapExactTokensForTokens", "swapExactETHForTokens"}


class UniswapV2Watcher(MempoolWatcher):
    def __init__(
        self,
        w3: AsyncWeb3,
        router_address: str,
        pool_registry: dict[tuple[str, str], str],
        min_amount_in: int,
    ):
        self.w3 = w3
        self.router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
        self.pool_registry = pool_registry
        self.min_amount_in = min_amount_in
        self._seen: set[str] = set()

    def _pool_for(self, token_in: str, token_out: str) -> str | None:
        return self.pool_registry.get((token_in.lower(), token_out.lower())) or self.pool_registry.get(
            (token_out.lower(), token_in.lower())
        )

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
            if amount_in < self.min_amount_in:
                continue

            path = params["path"]
            if len(path) != 2:
                # Multi-hop swaps don't move a single pool the way this bot's
                # simulator models it — matching one against a pool the victim
                # never touched would price a fictional mispricing.
                continue

            pool = self._pool_for(path[0], path[-1])
            if pool is None:
                continue

            yield PendingSwap(
                tx_hash=tx_hash.hex(),
                pool_address=pool,
                token_in=path[0],
                token_out=path[-1],
                amount_in=amount_in,
            )
