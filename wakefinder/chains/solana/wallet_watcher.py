"""Watcher конкретных кошельков на Solana — DEX-агностично. Подписывается на
logsSubscribe(mentions=[wallet]) для каждого watched-кошелька; при уведомлении
сравнивает preTokenBalances/postTokenBalances ВЛАДЕЛЬЦА (не всех аккаунтов
транзакции) — токен, чей баланс упал, это token_in, чей вырос — token_out.

Не декодирует конкретные инструкции Raydium/Orca/Jupiter/чего угодно ещё —
работает одинаково для любого DEX, потому что читает ЧИСТЫЙ ЭФФЕКТ транзакции
на баланс кошелька, а не пытается понять программную логику, которая к этому
привела. Это то, что делает возможным watchlist-копитрейдинг на Solana без
декодера под каждую площадку, которой может торговать отслеживаемый кошелёк.

Скорость: подписка идёт на commitment=Processed — самый быстрый публичный
уровень (нода увидела транзакцию, ещё не обязательно финализирована
супербольшинством; это не то же самое, что pending-мемпул на Ethereum, у
Solana такого для сторонних searcher'ов физически нет — приватный shred-stream
Jito требует отдельного одобрения). Саму транзакцию через getTransaction
запрашиваем на commitment=Confirmed с короткими повторами: `processed` для
getTransaction в основном не поддерживается публичными RPC (транзакция ещё не
обязательно проиндексирована для выборки), а между processed-уведомлением и
доступностью на confirmed — обычно доли секунды, не единицы.
"""

import asyncio
from collections.abc import AsyncIterator

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed
from solana.rpc.websocket_api import connect
from solders.pubkey import Pubkey
from solders.rpc.config import RpcTransactionLogsFilterMentions

from wakefinder.common.interfaces import MempoolWatcher, PendingSwap

FETCH_RETRIES = 5
FETCH_RETRY_DELAY_SECONDS = 0.2


class WalletSwapWatcher(MempoolWatcher):
    def __init__(self, ws_url: str, client: AsyncClient, watched_wallets: frozenset[str]):
        self.ws_url = ws_url
        self.client = client
        self.watched_wallets = watched_wallets

    async def _fetch_transaction(self, signature):
        for attempt in range(FETCH_RETRIES):
            try:
                tx_resp = await self.client.get_transaction(
                    signature, encoding="jsonParsed", commitment=Confirmed, max_supported_transaction_version=0
                )
            except Exception:
                tx_resp = None
            if tx_resp is not None and tx_resp.value is not None:
                return tx_resp.value
            if attempt < FETCH_RETRIES - 1:
                await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS)
        return None

    async def watch(self) -> AsyncIterator[PendingSwap]:
        async with connect(self.ws_url) as ws:
            sub_ids: dict[int, str] = {}
            for wallet in self.watched_wallets:
                await ws.logs_subscribe(
                    RpcTransactionLogsFilterMentions(Pubkey.from_string(wallet)), commitment=Processed
                )
                first = await ws.recv()
                sub_ids[first[0].result] = wallet

            async for messages in ws:
                for msg in messages:
                    wallet = sub_ids.get(msg.subscription)
                    if wallet is None:
                        continue
                    if msg.result.value.err is not None:
                        continue  # неудавшаяся транзакция — ничего не купили/продали

                    signature = msg.result.value.signature
                    tx_value = await self._fetch_transaction(signature)
                    if tx_value is None:
                        continue

                    meta = tx_value.transaction.meta
                    if meta is None or meta.pre_token_balances is None or meta.post_token_balances is None:
                        continue

                    pre = {b.mint: b for b in meta.pre_token_balances if str(b.owner) == wallet}
                    post = {b.mint: b for b in meta.post_token_balances if str(b.owner) == wallet}

                    token_in = token_out = None
                    amount_in = 0
                    for mint, post_balance in post.items():
                        pre_balance = pre.get(mint)
                        pre_amount = int(pre_balance.ui_token_amount.amount) if pre_balance else 0
                        post_amount = int(post_balance.ui_token_amount.amount)
                        delta = post_amount - pre_amount
                        if delta > 0:
                            token_out = str(mint)
                        elif delta < 0:
                            token_in = str(mint)
                            amount_in = -delta
                    # проданный токен мог полностью обнулиться и не попасть в post —
                    # проверяем и в эту сторону
                    if token_in is None:
                        for mint, pre_balance in pre.items():
                            if mint not in post:
                                token_in = str(mint)
                                amount_in = int(pre_balance.ui_token_amount.amount)
                                break

                    if token_in is None or token_out is None or amount_in <= 0:
                        continue  # не своп (например, просто перевод) или не удалось разобрать

                    yield PendingSwap(
                        tx_hash=str(signature),
                        pool_address="",  # неизвестен из balance-diff — не нужен для копитрейдинга
                        token_in=token_in,
                        token_out=token_out,
                        amount_in=amount_in,
                        sender=wallet,
                    )
