"""Watcher резервов пулов Solana (Raydium AMM V4 и совместимые constant-product пулы).

Честное отличие от Ethereum: у Solana нет публичного pending-мемпула, доступного
сторонним searcher'ам — приватный shred-stream Jito требует одобрения как
searcher (не просто API-ключ). Поэтому здесь наблюдаем не "ожидающую"
транзакцию, а изменение резервов пула (баланс vault-аккаунтов) сразу после
того, как своп УЖЕ подтверждён и слот обработан нодой — приходит пушем через
`accountSubscribe`. Это НЕ pre-confirmation sniping: гонка идёт за то, чтобы
попасть в СЛЕДУЮЩИЙ слот после уже подтверждённого сдвига цены, а не в тот же
блок вместе с чужой транзакцией (в отличие от Flashbots-бэкрана на Ethereum,
где транзакция жертвы и наша идут в одном бандле).

ponytail / известное ограничение: watchlist по конкретным кошелькам (как для
ETH) здесь НЕ реализован. accountSubscribe на vault-аккаунт не даёт отправителя
транзакции — увидеть его можно только дополнительно расшифровав транзакцию,
вызвавшую изменение баланса (getSignaturesForAddress + getTransaction), а
формат этой транзакции зависит от того, каким DEX/инструкцией торговал
конкретный кошелёк — декодер под произвольный DEX не входит в этот MVP.
Добавляйте `logsSubscribe(mentions=[wallet])` по конкретным адресам отдельно,
если это понадобится.

Live-config: self.pools мутируется live_config.sync_dict() IN PLACE (тот же
объект, не пересоздаётся) — watch() периодически (каждые
SUBSCRIPTION_SYNC_INTERVAL_SECONDS, чаще при активном трафике) пересчитывает
vault-индекс из self.pools и до/отписывает accountSubscribe на разницу, тот
же принцип, что и в chains/solana/wallet_watcher.py:WalletSwapWatcher.
"""

import asyncio
from collections.abc import AsyncIterator

from solana.rpc.websocket_api import connect
from solders.pubkey import Pubkey

from wakefinder.common.interfaces import MempoolWatcher, PendingSwap

SUBSCRIPTION_SYNC_INTERVAL_SECONDS = 5


class RaydiumVaultWatcher(MempoolWatcher):
    def __init__(
        self,
        ws_url: str,
        pools: dict[str, dict[str, str]],
        min_amount_in: int,
    ):
        """pools: {pool_id: {"base_vault": ..., "quote_vault": ..., "base_mint": ..., "quote_mint": ...}}"""
        self.ws_url = ws_url
        self.pools = pools  # мутируется live_config'ом in place — см. docstring модуля
        self.min_amount_in = min_amount_in
        self._last_balance: dict[str, int] = {}

    def _vault_index(self) -> dict[str, tuple[str, str]]:
        """адрес vault -> (pool_id, "base"/"quote") — пересчитывается из
        self.pools на каждый вызов, а не кэшируется в __init__, чтобы live-
        добавление/удаление пула отражалось без пересборки watcher'а."""
        index: dict[str, tuple[str, str]] = {}
        for pool_id, cfg in self.pools.items():
            index[cfg["base_vault"]] = (pool_id, "base")
            index[cfg["quote_vault"]] = (pool_id, "quote")
        return index

    async def _sync_subscriptions(self, ws, sub_ids: dict[int, str]) -> None:
        target = set(self._vault_index())
        current = set(sub_ids.values())
        for vault_address in target - current:
            await ws.account_subscribe(Pubkey.from_string(vault_address), encoding="jsonParsed")
            first = await ws.recv()
            sub_ids[first[0].result] = vault_address
        for sub_id, vault_address in list(sub_ids.items()):
            if vault_address not in target:
                await ws.account_unsubscribe(sub_id)
                del sub_ids[sub_id]
                self._last_balance.pop(vault_address, None)

    async def watch(self) -> AsyncIterator[PendingSwap]:
        async with connect(self.ws_url) as ws:
            sub_ids: dict[int, str] = {}
            await self._sync_subscriptions(ws, sub_ids)

            while True:
                try:
                    messages = await asyncio.wait_for(ws.recv(), timeout=SUBSCRIPTION_SYNC_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    await self._sync_subscriptions(ws, sub_ids)
                    continue

                vault_index = self._vault_index()
                for msg in messages:
                    vault_address = sub_ids.get(msg.subscription)
                    if vault_address is None:
                        continue
                    entry = vault_index.get(vault_address)
                    if entry is None:
                        continue  # пул удалён из self.pools между подпиской и этим сообщением — отписка догонит на следующем sync
                    pool_id, side = entry

                    try:
                        amount = int(msg.result.value.data.parsed["info"]["tokenAmount"]["amount"])
                    except (AttributeError, KeyError, TypeError):
                        continue

                    previous = self._last_balance.get(vault_address)
                    self._last_balance[vault_address] = amount
                    if previous is None:
                        continue  # первое уведомление после подписки — стартовый снимок, не дельта

                    delta = amount - previous
                    if delta <= 0:
                        continue  # растёт та сторона, куда ЗАШЁЛ токен — это и есть token_in свопа

                    if delta < self.min_amount_in:
                        continue

                    cfg = self.pools[pool_id]
                    if side == "base":
                        token_in, token_out = cfg["base_mint"], cfg["quote_mint"]
                    else:
                        token_in, token_out = cfg["quote_mint"], cfg["base_mint"]

                    yield PendingSwap(
                        tx_hash=f"slot-delta:{pool_id}:{amount}",
                        pool_address=pool_id,
                        token_in=token_in,
                        token_out=token_out,
                        amount_in=delta,
                    )

                await self._sync_subscriptions(ws, sub_ids)
