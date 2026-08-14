"""Flashbots bundle sender. Always simulates before sending — the state-drift
safety net (on-chain reserves having moved between simulate() and now) is
already provided by each leg's amountOutMin: if reserves shifted past that
tolerance, the router itself reverts with INSUFFICIENT_OUTPUT_AMOUNT and this
simulation catches it as a per-tx error below. (coinbaseDiff was tried here
first and removed: it's what WE pay the builder in fees, not our profit — our
profit lands as a token balance change in our own wallet, not in coinbaseDiff —
so it can't stand in for "is this still worth sending".)

ponytail: the `flashbots` pip package only wraps sync Web3 (no async client), so
this uses a plain Web3 instance internally via asyncio.to_thread rather than
threading async support through a library that doesn't have it.
"""

import asyncio

from eth_account.signers.local import LocalAccount
from flashbots import flashbot
from web3 import HTTPProvider, Web3

from mevbot.common.interfaces import Bundle, BundleSender


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
