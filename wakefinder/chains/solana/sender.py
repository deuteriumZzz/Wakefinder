"""Отправитель бандлов в Jito. Tip-аккаунт запрашивается динамически через
getTipAccounts (не хардкодится — Jito может их менять). Bundle без tip не
рассматривается block engine'ом вообще — это встроенный, обязательный аналог
builder-payment, не опциональная настройка, как priority fee на Ethereum.

ponytail: `jito_py_rpc` — синхронная обёртка (requests), оборачиваем в
asyncio.to_thread — тот же приём, что и с `flashbots` на стороне ETH.
"""

import asyncio
import base64

from jito_py_rpc import JitoJsonRpcSDK
from solders.keypair import Keypair

from wakefinder.common.interfaces import Bundle, BundleSender


class JitoBundleSender(BundleSender):
    def __init__(self, jito_block_engine_url: str, keypair: Keypair):
        self.jito = JitoJsonRpcSDK(url=jito_block_engine_url)
        self.keypair = keypair

    def _get_tip_account_sync(self) -> str | None:
        return self.jito.get_random_tip_account()

    async def get_tip_account(self) -> str:
        account = await asyncio.to_thread(self._get_tip_account_sync)
        if account is None:
            raise RuntimeError(
                "Jito getTipAccounts не вернул ни одного аккаунта — без tip-аккаунта "
                "bundle собрать нельзя, block engine его не примет"
            )
        return account

    def _send_sync(self, bundle: Bundle) -> bool:
        response = self.jito.send_bundle(params=bundle.raw_txs)
        if not response.get("success"):
            return False
        data = response.get("data", {})
        return "error" not in data and "result" in data

    async def send(self, bundle: Bundle) -> bool:
        return await asyncio.to_thread(self._send_sync, bundle)


def to_base64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")
