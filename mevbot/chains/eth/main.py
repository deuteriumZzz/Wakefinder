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
"""

import asyncio
import time

from eth_account import Account
from web3 import AsyncWeb3, Web3, WebsocketProviderV2

from mevbot.chains.eth.abi import ROUTER_ABI
from mevbot.chains.eth.sender import FlashbotsBundleSender
from mevbot.chains.eth.simulator import GAS_LIMIT, TwoPoolArbSimulator
from mevbot.chains.eth.watcher import UniswapV2Watcher
from mevbot.common.config import get_settings
from mevbot.common.interfaces import Bundle, PendingSwap, SimResult

SLIPPAGE_BPS = 100  # 1% tolerance between simulation and on-chain execution
_ENCODER = Web3()  # provider-less: pure offline ABI encoding, never makes an RPC call


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


async def _gas_fees(w3: AsyncWeb3, max_gas_gwei: float) -> tuple[int, int]:
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = min(latest["baseFeePerGas"] * 2 + priority_fee, Web3.to_wei(max_gas_gwei, "gwei"))
    return max_fee, priority_fee


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
            sim = await simulator.simulate(swap)
            if not sim.profitable:
                continue

            try:
                victim_raw = await w3.eth.get_raw_transaction(swap.tx_hash)
            except Exception as exc:
                print(f"get_raw_transaction unsupported/failed for {swap.tx_hash}: {exc!r} — "
                      "your RPC provider likely doesn't support eth_getRawTransactionByHash; "
                      "switch providers, this bot cannot build a bundle without it.")
                continue

            block_number = await w3.eth.block_number
            nonce = await w3.eth.get_transaction_count(account.address, "pending")
            max_fee, priority_fee = await _gas_fees(w3, settings.max_gas_gwei)

            buy_leg, sell_leg = _build_bundle_legs(account, chain_id, nonce, max_fee, priority_fee, swap, sim)

            bundle = Bundle(
                raw_txs=[_to_0x_hex(victim_raw), _to_0x_hex(buy_leg), _to_0x_hex(sell_leg)],
                target_block=block_number + 1,
                min_profit_wei=sim.expected_profit_wei,
            )
            included = await sender.send(bundle)
            print(f"swap={swap.tx_hash} profit_wei={sim.expected_profit_wei} included={included}")


if __name__ == "__main__":
    asyncio.run(run(pool_registry={}, reference_pools={}, min_amount_in=10**18))
