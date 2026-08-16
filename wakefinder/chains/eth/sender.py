"""Отправитель бандлов в Flashbots (и опционально другие MEV-relay,
совместимые по протоколу eth_sendBundle). Всегда симулирует перед отправкой —
страховка от рассинхронизации состояния (резервы в сети сдвинулись между
simulate() и отправкой) уже обеспечена через amountOutMin у каждой ноги: если
резервы сдвинулись за пределы допуска, сам роутер ревертит с
INSUFFICIENT_OUTPUT_AMOUNT, и эта симуляция ловит это ниже как ошибку по
конкретной транзакции. (coinbaseDiff сначала попробовали здесь и убрали: это
то, что МЫ платим билдеру в комиссиях, а не наша прибыль — прибыль оседает
как изменение баланса токена на нашем собственном кошельке, а не в
coinbaseDiff — так что она не может подменять собой ответ на вопрос "всё ещё
стоит ли это отправлять".)

Мульти-relay: один и тот же подписанный бандл отправляется параллельно во
все настроенные relay — у каждого свой набор builder'ов, поэтому это реально
увеличивает шанс попасть в блок, а не дублирует одну и ту же попытку. Дефолт —
только Flashbots (обратная совместимость). ponytail: relay помимо Flashbots
могут требовать собственную авторизацию (API-ключ/заголовок), которая здесь
НЕ реализована — добавляйте такой relay, только если он принимает тот же
`eth_sendBundle` без доп. авторизации, либо расширяйте `_client_for_relay`.

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

DEFAULT_RELAY_URLS = ["https://relay.flashbots.net"]


class FlashbotsBundleSender(BundleSender):
    def __init__(self, rpc_url: str, signer_account: LocalAccount, relay_urls: list[str] | None = None):
        self._clients: list[Web3] = []
        for relay_url in (relay_urls or DEFAULT_RELAY_URLS):
            w3 = Web3(HTTPProvider(rpc_url))
            flashbot(w3, signer_account, relay_url)
            self._clients.append(w3)

    def _simulate_sync(self, raw_txs: list[str], target_block: int) -> dict:
        fb_bundle = [{"signed_transaction": raw} for raw in raw_txs]
        # На первом клиенте — все relay симулируют против одного и того же
        # состояния сети, повторять на каждом бессмысленно.
        return self._clients[0].flashbots.simulate(fb_bundle, target_block)

    def _send_sync(self, bundle: Bundle) -> bool:
        simulation = self._simulate_sync(bundle.raw_txs, bundle.target_block)
        if simulation.get("error") or any(tx.get("error") for tx in simulation.get("results", [])):
            return False

        fb_bundle = [{"signed_transaction": raw} for raw in bundle.raw_txs]
        pending_results = []
        for w3 in self._clients:
            try:
                pending_results.append(w3.flashbots.send_bundle(fb_bundle, target_block_number=bundle.target_block))
            except Exception:
                continue  # один relay недоступен — остальные всё ещё пытаются

        for result in pending_results:
            receipts = result.wait()
            if receipts is not None and len(receipts) > 0:
                return True
        return False

    async def simulate(self, raw_txs: list[str], target_block: int) -> dict:
        """Публичный доступ к симуляции без реальной отправки — используется
        chains/eth/snipe_filter.py для round-trip проверки "можно ли продать
        этот токен обратно" (см. её docstring), не только send()'ом."""
        return await asyncio.to_thread(self._simulate_sync, raw_txs, target_block)

    async def send(self, bundle: Bundle) -> bool:
        return await asyncio.to_thread(self._send_sync, bundle)
