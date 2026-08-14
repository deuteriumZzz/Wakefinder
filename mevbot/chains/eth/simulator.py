"""Two-pool arbitrage simulator: after a whale swap moves `pool`'s price, check
whether `reference_pool` (a different DEX's pool for the same pair, unaffected by
the victim's trade) now offers a profitable round trip.

Direction: the victim's swap makes token_out MORE expensive in the target pool
(they just bought it there). So the profitable leg order is buy token_out where
it's still cheap — the reference pool — then sell it into the target pool. Buying
in the target pool instead (the reverse) is the anti-arb; see mevbot/common/amm.py
for the buy_*/sell_* naming this must match at the call site.

A backrun against a single pool alone isn't an arbitrage — nothing pulls the price
back without a second liquidity source, hence the mandatory reference pool.
"""

from web3 import AsyncWeb3

from mevbot.chains.eth.abi import PAIR_ABI
from mevbot.common.amm import apply_swap, optimal_arb
from mevbot.common.config import get_settings
from mevbot.common.interfaces import PendingSwap, SimResult, Simulator

GAS_LIMIT = 200_000  # must match the actual per-leg gas limit used when building txs


class TwoPoolArbSimulator(Simulator):
    def __init__(self, w3: AsyncWeb3, target_router: str, reference_pools: dict[str, dict[str, str]]):
        """reference_pools: {target_pool_address: {"pool": ref_pool_address, "router": ref_router_address}}"""
        self.w3 = w3
        self.target_router = target_router
        self.reference_pools = {k.lower(): v for k, v in reference_pools.items()}
        self._token0_cache: dict[str, str] = {}

    async def _token0(self, pool_address: str) -> str:
        cached = self._token0_cache.get(pool_address.lower())
        if cached:
            return cached
        pool = self.w3.eth.contract(address=pool_address, abi=PAIR_ABI)
        token0 = await pool.functions.token0().call()
        self._token0_cache[pool_address.lower()] = token0
        return token0

    async def _reserves(self, pool_address: str, token_in: str, block_number: int) -> tuple[int, int]:
        pool = self.w3.eth.contract(address=pool_address, abi=PAIR_ABI)
        reserve0, reserve1, _ = await pool.functions.getReserves().call(block_identifier=block_number)
        token0 = await self._token0(pool_address)
        if token0.lower() == token_in.lower():
            return reserve0, reserve1
        return reserve1, reserve0

    async def simulate(self, swap: PendingSwap) -> SimResult:
        ref = self.reference_pools.get(swap.pool_address.lower())
        if ref is None:
            return SimResult(profitable=False, expected_profit_wei=0, reason="no reference pool configured")

        # Pin both reads to the same block: the target pool must be read
        # *before* the victim's swap lands, then advanced by apply_swap() to
        # model post-inclusion state; the reference pool must be read at the
        # same instant so the two aren't comparing prices from different blocks.
        block_number = await self.w3.eth.block_number

        target_reserve_in, target_reserve_out = await self._reserves(swap.pool_address, swap.token_in, block_number)
        new_target_in, new_target_out, _ = apply_swap(target_reserve_in, target_reserve_out, swap.amount_in)

        ref_reserve_in, ref_reserve_out = await self._reserves(ref["pool"], swap.token_in, block_number)

        settings = get_settings()

        # Cap the search here (not after the fact in the tx builder) so
        # sim.amount_in/expected_profit_wei stay consistent with the trade we
        # actually build+sign — a post-hoc cap would silently invalidate the
        # profit figure, since arb profit isn't linear in amount_in.
        #
        # ponytail: assumes token_in is 18-decimal (e.g. WETH) — the cap is a
        # raw wei figure. Reject non-WETH token_in at the caller until this
        # accounts for decimals properly.
        wei_cap = int(settings.max_capital_per_bundle_eth * 10**18)
        upper_bound = min(wei_cap, ref_reserve_in, new_target_out)

        # Two legs of gas, priced in token_in units, assuming token_in is WETH
        # (see the wei_cap note above — same assumption, same fix needed together).
        gas_cost_wei = 2 * GAS_LIMIT * int(settings.max_gas_gwei * 10**9)

        amount_in, profit = optimal_arb(
            buy_reserve_in=ref_reserve_in,
            buy_reserve_out=ref_reserve_out,
            sell_reserve_out=new_target_out,
            sell_reserve_in=new_target_in,
            gas_cost_wei=gas_cost_wei,
            upper_bound=upper_bound,
        )
        if profit <= 0 or amount_in <= 0:
            return SimResult(profitable=False, expected_profit_wei=0, reason="no net-of-gas arb after victim's swap")

        _, bought_amount, _ = apply_swap(ref_reserve_in, ref_reserve_out, amount_in)
        return SimResult(
            profitable=True,
            expected_profit_wei=profit,
            amount_in=amount_in,
            bought_amount=bought_amount,
            buy_router=ref["router"],
            sell_router=self.target_router,
        )
