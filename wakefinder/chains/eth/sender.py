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
только Flashbots (обратная совместимость).

Авторизация non-Flashbots relay (bloXroute, Eden и т.п., ETH_RELAY_API_KEYS в
config.py): `flashbots` пакет добавляет ТОЛЬКО `X-Flashbots-Signature`
(`FlashbotProvider.get_request_headers()` — не читает `request_kwargs`,
проверено чтением исходника, не предположение) — здесь `_AuthedFlashbotProvider`
переопределяет именно этот метод, чтобы домешать `Authorization: <ключ>`.
Общий случай bearer-токена; relay с другой схемой авторизации (не
`Authorization`-заголовок) сюда не впишутся без доработки.

ponytail: пакет `flashbots` на pip оборачивает только синхронный Web3 (async-клиента
нет), поэтому здесь внутри используется обычный синхронный Web3 через
asyncio.to_thread, а не попытка протащить async-поддержку через библиотеку,
у которой её нет.
"""

import asyncio

from eth_account.signers.local import LocalAccount
from flashbots import Flashbots, attach_modules, construct_flashbots_middleware, flashbot
from flashbots.provider import FlashbotProvider
from web3 import HTTPProvider, Web3

from wakefinder.common.interfaces import Bundle, BundleSender

DEFAULT_RELAY_URLS = ["https://relay.flashbots.net"]


class _AuthedFlashbotProvider(FlashbotProvider):
    """FlashbotProvider домешивает только X-Flashbots-Signature
    (get_request_headers() не читает request_kwargs — проверено чтением
    исходника пакета flashbots, см. docstring модуля) — здесь добавляем
    Authorization для relay, которым этого недостаточно."""

    def __init__(self, signature_account: LocalAccount, endpoint_uri: str, api_key: str):
        super().__init__(signature_account, endpoint_uri)
        self._api_key = api_key

    def get_request_headers(self) -> dict:
        return {**super().get_request_headers(), "Authorization": self._api_key}


def _flashbot_client(rpc_url: str, signer_account: LocalAccount, relay_url: str, api_key: str) -> Web3:
    w3 = Web3(HTTPProvider(rpc_url))
    if not api_key:
        flashbot(w3, signer_account, relay_url)
        return w3
    # Тот же монтаж, что flashbot() делает внутри, только с authed-провайдером —
    # flashbot() сам такой провайдер не принимает (только endpoint_uri).
    provider = _AuthedFlashbotProvider(signer_account, relay_url, api_key)
    w3.middleware_onion.add(construct_flashbots_middleware(provider))
    attach_modules(w3, {"flashbots": (Flashbots,)})
    return w3


class FlashbotsBundleSender(BundleSender):
    def __init__(
        self, rpc_url: str, signer_account: LocalAccount, relay_urls: list[str] | None = None,
        relay_api_keys: list[str] | None = None,
    ):
        urls = relay_urls or DEFAULT_RELAY_URLS
        keys = (relay_api_keys or []) + [""] * len(urls)  # короче списка URL -> остальным пусто, см. docstring config.py
        self._clients: list[Web3] = [
            _flashbot_client(rpc_url, signer_account, relay_url, api_key)
            for relay_url, api_key in zip(urls, keys)
        ]

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
