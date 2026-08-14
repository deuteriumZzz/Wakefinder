"""Full-cycle pipeline: sniff -> simulate -> build two-leg bundle -> send.

Bundle order is [victim_raw, buy_leg_raw, sell_leg_raw]: the sell leg MUST land
after the victim's tx — that's the whole thesis, the victim's trade is what
makes the target pool's price attractive to sell into. Bundling all three
together means either the whole arb executes atomically or nothing does: no
partial fills, and no gas spent if the bundle isn't included (Flashbots-only
submission never touches the public mempool, so an unprofitable/unsimulated
bundle just never gets broadcast).

Requires every router this might trade through (target + all reference
routers) to already have ERC20 approval from ETH_PRIVATE_KEY's wallet for
every token involved — one-time setup, not part of the hot path.

Requires an RPC provider that supports eth_getRawTransactionByHash for the
victim's tx — not all providers do; this fails loudly (not silently) if yours
doesn't, rather than shipping an unverified raw-tx reconstruction. Not
runnable end-to-end without a funded testnet wallet + such a provider —
validate there before touching mainnet capital.

ponytail: swap processing is sequential (each opportunity's send() completes
before the next is even nonce-assigned) — deliberately, not an oversight.
Nonce is re-fetched fresh per attempt: a Flashbots-only bundle that never
lands leaves no trace in the node's own mempool view, so a fresh
get_transaction_count() naturally reuses the same nonce for a retried
attempt and advances once a bundle actually lands. Processing concurrently
would let two in-flight bundles grab the same nonce and race — fixing that
needs a nonce-pool or a lock, which isn't worth the complexity for a bot
that (per design) isn't colocated and isn't winning latency races anyway;
sequential trades a small chance of missing an overlapping opportunity
during one block for provably no nonce races.
"""

import asyncio
import logging
import os
import time

from eth_account import Account
from web3 import AsyncWeb3, Web3, WebsocketProviderV2

from mevbot.chains.eth.abi import ERC20_ABI, ROUTER_ABI
from mevbot.chains.eth.sender import FlashbotsBundleSender
from mevbot.chains.eth.simulator import GAS_LIMIT, TwoPoolArbSimulator
from mevbot.chains.eth.watcher import UniswapV2Watcher
from mevbot.common.config import get_settings
from mevbot.common.interfaces import Bundle, PendingSwap, SimResult

SLIPPAGE_BPS = 100  # 1% tolerance between simulation and on-chain execution
BASE_PRIORITY_FEE_WEI = Web3.to_wei(2, "gwei")  # floor tip even when profit_share math rounds to ~0
_ENCODER = Web3()  # provider-less: pure offline ABI encoding, never makes an RPC call

logger = logging.getLogger("mevbot.eth")


def _to_0x_hex(raw: bytes) -> str:
    hex_str = bytes(raw).hex()
    return hex_str if hex_str.startswith("0x") else "0x" + hex_str


def _sign_leg(router_address: str, account, chain_id: int, nonce: int, max_fee: int, priority_fee: int, path: list[str], amount_in: int, amount_out_min: int) -> bytes:
    router = _ENCODER.eth.contract(address=router_address, abi=ROUTER_ABI)
    tx = router.functions.swapExactTokensForTokens(
        amount_in,
        amount_out_min,
        path,
        account.address,
        int(time.time()) + 60,
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "chainId": chain_id,
        }
    )
    return account.sign_transaction(tx).raw_transaction


def _compute_fees(base_fee: int, max_gas_gwei: float, expected_profit_wei: int, profit_share_bps: int) -> tuple[int, int]:
    """maxFeePerGas/maxPriorityFeePerGas, with the tip bid up toward a share of
    captured profit — builders sort bundles by total value (fees + transfers),
    so a flat minimal tip routinely loses the inclusion auction to a bundle
    that bids closer to what it's actually worth."""
    tip_pool = expected_profit_wei * profit_share_bps // 10_000
    priority_fee = max(BASE_PRIORITY_FEE_WEI, tip_pool // (2 * GAS_LIMIT))
    max_fee = min(base_fee * 2 + priority_fee, Web3.to_wei(max_gas_gwei, "gwei"))
    return max_fee, priority_fee


async def _gas_fees(w3: AsyncWeb3, max_gas_gwei: float, expected_profit_wei: int, profit_share_bps: int) -> tuple[int, int]:
    latest = await w3.eth.get_block("latest")
    return _compute_fees(latest["baseFeePerGas"], max_gas_gwei, expected_profit_wei, profit_share_bps)


def _build_bundle_legs(account, chain_id: int, nonce: int, max_fee: int, priority_fee: int, swap: PendingSwap, sim: SimResult) -> list[bytes]:
    buy_amount_out_min = sim.bought_amount * (10_000 - SLIPPAGE_BPS) // 10_000
    buy_leg = _sign_leg(
        sim.buy_router, account, chain_id, nonce, max_fee, priority_fee,
        [swap.token_in, swap.token_out], sim.amount_in, buy_amount_out_min,
    )

    expected_final_out = sim.amount_in + sim.expected_profit_wei
    sell_amount_out_min = expected_final_out * (10_000 - SLIPPAGE_BPS) // 10_000
    sell_leg = _sign_leg(
        sim.sell_router, account, chain_id, nonce + 1, max_fee, priority_fee,
        [swap.token_out, swap.token_in], sim.bought_amount, sell_amount_out_min,
    )
    return [buy_leg, sell_leg]


async def _has_sufficient_balance(w3: AsyncWeb3, account_address: str, token_in: str, amount_in: int, gas_reserve_wei: int) -> bool:
    eth_balance = await w3.eth.get_balance(account_address)
    if eth_balance < gas_reserve_wei:
        logger.warning("insufficient ETH for gas: have=%d need=%d", eth_balance, gas_reserve_wei)
        return False

    token = w3.eth.contract(address=token_in, abi=ERC20_ABI)
    token_balance = await token.functions.balanceOf(account_address).call()
    if token_balance < amount_in:
        logger.warning("insufficient token_in balance: have=%d need=%d token=%s", token_balance, amount_in, token_in)
        return False
    return True


async def run(pool_registry: dict[tuple[str, str], str], reference_pools: dict[str, dict[str, str]], min_amount_in: int):
    settings = get_settings()
    account = Account.from_key(settings.eth_private_key.get_secret_value())
    fb_signer = Account.from_key(settings.flashbots_signer_key.get_secret_value())

    provider = WebsocketProviderV2(settings.eth_rpc_ws_url.get_secret_value())
    async with AsyncWeb3.persistent_websocket(provider) as w3:
        chain_id = await w3.eth.chain_id
        watcher = UniswapV2Watcher(w3, settings.eth_router_address, pool_registry, min_amount_in)
        simulator = TwoPoolArbSimulator(w3, settings.eth_router_address, reference_pools)
        sender = FlashbotsBundleSender(
            rpc_url=settings.eth_rpc_http_url.get_secret_value(),
            signer_account=fb_signer,
        )

        async for swap in watcher.watch():
            if os.path.exists(settings.kill_switch_file):
                logger.warning("kill switch file %s present — stopping", settings.kill_switch_file)
                return

            sim = await simulator.simulate(swap)
            if not sim.profitable:
                continue

            try:
                victim_raw = await w3.eth.get_raw_transaction(swap.tx_hash)
            except Exception as exc:
                logger.error(
                    "get_raw_transaction failed for %s (%s) — your RPC provider likely doesn't "
                    "support eth_getRawTransactionByHash; this bot cannot build a bundle without it.",
                    swap.tx_hash, type(exc).__name__,
                )
                continue

            max_fee, priority_fee = await _gas_fees(w3, settings.max_gas_gwei, sim.expected_profit_wei, settings.profit_share_bps)
            gas_reserve_wei = 2 * GAS_LIMIT * max_fee
            if not await _has_sufficient_balance(w3, account.address, swap.token_in, sim.amount_in, gas_reserve_wei):
                continue

            block_number = await w3.eth.block_number
            nonce = await w3.eth.get_transaction_count(account.address, "pending")

            buy_leg, sell_leg = _build_bundle_legs(account, chain_id, nonce, max_fee, priority_fee, swap, sim)

            bundle = Bundle(
                raw_txs=[_to_0x_hex(victim_raw), _to_0x_hex(buy_leg), _to_0x_hex(sell_leg)],
                target_block=block_number + 1,
            )
            included = await sender.send(bundle)
            logger.info("swap=%s profit_wei=%d included=%s", swap.tx_hash, sim.expected_profit_wei, included)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(pool_registry={}, reference_pools={}, min_amount_in=10**18))
