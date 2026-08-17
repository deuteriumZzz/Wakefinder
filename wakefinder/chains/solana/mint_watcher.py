"""Watcher новых SPL-токенов на Solana — подписывается на логи SPL Token
Program (`TOKEN_PROGRAM_ID`, реальный адрес из `spl.token.constants`, не
угадан) и отдаёт минты, для которых прошла инструкция `InitializeMint`/
`InitializeMint2`.

ЧЕСТНО другой подход, чем ETH `PairCreatedWatcher`: там `PairCreated` — это
единственное, стабильное, ABI-описанное событие ОДНОЙ конкретной фабрики
(Uniswap V2), которое можно с уверенностью распарсить, не имея доступа к
живой сети для проверки (тот же ABI используется тысячами проектов). На
Solana аналога "фабрики создания пула" с одним стабильным ABI нет — у
каждого AMM (Raydium AMM V4, Raydium CLMM, Orca Whirlpool, ...) свой
program ID и свой формат accounts для инструкции создания пула, и в этом
окружении нет живого Solana-валидатора для fork-теста такого парсинга (в
отличие от anvil для ETH) — риск тихо ошибиться в layout был бы слишком
велик, чтобы полагаться на него для реальных денег.

Поэтому здесь — НАМЕРЕННО более широкий и медленный, но проверяемый сигнал:
факт создания НОВОГО МИНТА (SPL Token Program — фундаментальный, стабильный,
используется буквально всеми) — большинство новых минтов НИКОГДА не получат
пул вообще, это не проблема: реальная проверка ликвидности/маршрута — через
Jupiter (chains/solana/snipe_filter.py), который сам агрегирует все DEX и
возвращает маршрут, только если он реально существует. Jupiter индексирует
новый пул не мгновенно — это делает снайпинг медленнее ETH-варианта, но
не полагается на непроверенное здесь знание об account-layout стороннего
AMM.

Инструкция парсится через `jsonParsed`-транзакцию (тот же формат, что уже
использует wallet_watcher.py для pre/post token balances) — `parsed.type`
и `parsed.info.mint` для `initializeMint`/`initializeMint2` — задокументированный
формат публичного Solana JSON-RPC (https://solana.com/docs/rpc), не instruction-level
байтовый парсинг."""

import asyncio
from collections.abc import AsyncIterator

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed
from solana.rpc.websocket_api import connect
from solders.rpc.config import RpcTransactionLogsFilterMentions
from spl.token.constants import TOKEN_PROGRAM_ID

from wakefinder.common.interfaces import MempoolWatcher, NewMint

FETCH_RETRIES = 5
FETCH_RETRY_DELAY_SECONDS = 0.2
INITIALIZE_MINT_TYPES = ("initializeMint", "initializeMint2")


class NewMintWatcher(MempoolWatcher[NewMint]):
    def __init__(self, ws_url: str, client: AsyncClient):
        self.ws_url = ws_url
        self.client = client
        self._seen: set[str] = set()

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

    async def watch(self) -> AsyncIterator[NewMint]:
        async with connect(self.ws_url) as ws:
            await ws.logs_subscribe(
                RpcTransactionLogsFilterMentions(TOKEN_PROGRAM_ID), commitment=Processed
            )
            first = await ws.recv()
            sub_id = first[0].result

            async for messages in ws:
                for msg in messages:
                    if msg.subscription != sub_id:
                        continue
                    if msg.result.value.err is not None:
                        continue

                    signature = msg.result.value.signature
                    sig_str = str(signature)
                    if sig_str in self._seen:
                        continue
                    self._seen.add(sig_str)

                    tx_value = await self._fetch_transaction(signature)
                    if tx_value is None:
                        continue

                    try:
                        instructions = tx_value.transaction.transaction.message.instructions
                    except AttributeError:
                        continue

                    for ix in instructions:
                        parsed = getattr(ix, "parsed", None)
                        if not isinstance(parsed, dict):
                            continue
                        if parsed.get("type") not in INITIALIZE_MINT_TYPES:
                            continue
                        mint = parsed.get("info", {}).get("mint")
                        if not mint:
                            continue
                        yield NewMint(tx_hash=sig_str, mint_address=mint, slot=tx_value.slot)
