"""Отправитель бандлов в Flashbots. Всегда симулирует перед отправкой — страховка
от рассинхронизации состояния (резервы в сети сдвинулись между simulate() и
отправкой) уже обеспечена через amountOutMin у каждой ноги: если резервы
сдвинулись за пределы допуска, сам роутер ревертит с INSUFFICIENT_OUTPUT_AMOUNT,
и эта симуляция ловит это ниже как ошибку по конкретной транзакции.
(coinbaseDiff сначала попробовали здесь и убрали: это то, что МЫ платим билдеру
в комиссиях, а не наша прибыль — прибыль оседает как изменение баланса токена
на нашем собственном кошельке, а не в coinbaseDiff — так что она не может
подменять собой ответ на вопрос "всё ещё стоит ли это отправлять".)

ponytail: пакет `flashbots` на pip оборачивает только синхронный Web3 (async-клиента
нет), поэтому здесь внутри используется обычный синхронный Web3 через
asyncio.to_thread, а не попытка протащить async-поддержку через библиотеку,
у которой её нет.
"""

import asyncio

from eth_account.signers.local import LocalAccount
from flashbots import flashbot
from web3 import HTTPProvider, Web3

from wakefinder.common.interfaces import Bundle, BundleSender


class FlashbotsBundleSender(BundleSender):
    def __init__(self, rpc_url: str, signer_account: LocalAccount, relay_url: str = "https://relay.flashbots.net"):
        self.w3 = Web3(HTTPProvider(rpc_url))
        flashbot(self.w3, signer_account, relay_url)

    def _send_sync(self, bundle: Bundle) -> bool:
        fb_bundle = [{"signed_transaction": raw} for raw in bundle.raw_txs]

        simulation = self.w3.flashbots.simulate(fb_bundle, bundle.target_block)
        if simulation.get("error") or any(tx.get("error") for tx in simulation.get("results", [])):
            return False

        result = self.w3.flashbots.send_bundle(fb_bundle, target_block_number=bundle.target_block)
        receipts = result.wait()
        return receipts is not None and len(receipts) > 0

    async def send(self, bundle: Bundle) -> bool:
        return await asyncio.to_thread(self._send_sync, bundle)
