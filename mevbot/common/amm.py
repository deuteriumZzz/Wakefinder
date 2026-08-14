"""Constant-product (x*y=k) AMM math, chain-agnostic.

Naming uses buy_*/sell_* (not pool "A"/"B") after a real bug: generic A/B naming
let the two-leg direction get swapped at the call site with nothing to catch it —
see mevbot/chains/eth/simulator.py for the arb direction this must match:
buy token_out where it's still cheap (the untouched reference pool), sell it
where the victim's trade just made it expensive (the target pool).
"""

FEE_BPS = 30  # 0.3%, standard Uniswap V2 / most V2 forks


def get_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = FEE_BPS) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    amount_in_with_fee = amount_in * (10_000 - fee_bps)
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * 10_000 + amount_in_with_fee
    return numerator // denominator


def apply_swap(
    reserve_in: int, reserve_out: int, amount_in: int, fee_bps: int = FEE_BPS
) -> tuple[int, int, int]:
    amount_out = get_amount_out(amount_in, reserve_in, reserve_out, fee_bps)
    return reserve_in + amount_in, reserve_out - amount_out, amount_out


def arb_profit(
    amount_in: int,
    buy_reserve_in: int,
    buy_reserve_out: int,
    sell_reserve_out: int,
    sell_reserve_in: int,
    fee_bps: int = FEE_BPS,
    gas_cost_wei: int = 0,
) -> int:
    """Net profit (in token_in units) from buying token_out in the buy pool, then
    selling it back for token_in in the sell pool, minus a flat gas cost. Both
    pools' reserves are passed as (token_in reserve, token_out reserve) — the sell
    pool's are reversed internally to match the direction of that leg."""
    bought = get_amount_out(amount_in, buy_reserve_in, buy_reserve_out, fee_bps)
    returned = get_amount_out(bought, sell_reserve_out, sell_reserve_in, fee_bps)
    return returned - amount_in - gas_cost_wei


def optimal_arb(
    buy_reserve_in: int,
    buy_reserve_out: int,
    sell_reserve_out: int,
    sell_reserve_in: int,
    fee_bps: int = FEE_BPS,
    gas_cost_wei: int = 0,
    upper_bound: int | None = None,
) -> tuple[int, int]:
    """Ternary search for the net-profit-maximizing amount_in.

    ponytail: profit(amount_in) is unimodal (concave, with integer-rounding
    plateaus) for two-pool constant-product arb, so ternary search finds the
    optimum without deriving/trusting a hand-rolled closed-form formula. The
    `lo=m1 / hi=m2` (not `m1+1` / `m2-1`) bounds are required for correctness on
    the plateaus this integer version has — the tighter bounds can skip the
    optimum. Good enough at searcher-bot scale (microseconds either way).
    """
    lo, hi = 0, upper_bound if upper_bound is not None else min(buy_reserve_in, sell_reserve_out)
    if hi <= 0:
        return 0, 0

    def profit(amount: int) -> int:
        return arb_profit(amount, buy_reserve_in, buy_reserve_out, sell_reserve_out, sell_reserve_in, fee_bps, gas_cost_wei)

    while hi - lo > 2:
        m1 = lo + (hi - lo) // 3
        m2 = hi - (hi - lo) // 3
        if profit(m1) < profit(m2):
            lo = m1
        else:
            hi = m2
    # 0 is always a valid choice (don't trade, pay no gas) — not profit(0),
    # which would already include gas_cost_wei for a trade that never happens.
    best_amount, best_profit = 0, 0
    for amount in range(lo, hi + 1):
        p = profit(amount)
        if p > best_profit:
            best_amount, best_profit = amount, p
    return best_amount, best_profit
