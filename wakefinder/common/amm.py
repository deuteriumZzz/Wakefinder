"""Математика AMM с постоянным произведением (x*y=k), не привязана к конкретной сети.

Именование через buy_*/sell_* (а не пул "A"/"B") — после реальной ошибки: общие
имена A/B позволили перепутать направление двух ног сделки в месте вызова, и
ничего этого не поймало — см. wakefinder/chains/eth/simulator.py, направление
арбитража там должно совпадать с этим: покупаем token_out там, где он ещё дёшев
(нетронутый референсный пул), продаём там, где сделка кита только что сделала
его дороже (целевой пул).
"""

FEE_BPS = 30  # 0.3%, стандарт Uniswap V2 / большинства V2-форков


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
    """Чистая прибыль (в единицах token_in) от покупки token_out в buy-пуле и
    последующей продажи его обратно за token_in в sell-пуле, за вычетом фиксированной
    стоимости газа. Резервы обоих пулов передаются как (резерв token_in, резерв
    token_out) — для sell-пула порядок разворачивается внутри функции под
    направление этой ноги."""
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
    """Тернарный поиск amount_in, максимизирующего чистую прибыль.

    ponytail: profit(amount_in) унимодальна (вогнута, с плато из-за целочисленного
    округления) для арбитража между двумя пулами с постоянным произведением, так
    что тернарный поиск находит оптимум без вывода/доверия самодельной формуле в
    закрытом виде. Границы `lo=m1 / hi=m2` (а не `m1+1` / `m2-1`) нужны именно для
    корректности на плато, которые даёт эта целочисленная версия — более тесные
    границы могут проскочить оптимум. Для масштаба searcher-бота этого достаточно
    (разница — микросекунды в любую сторону).
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
    # 0 — всегда допустимый выбор (не торговать, не платить газ) — не profit(0),
    # которая уже включала бы gas_cost_wei за сделку, которой не будет.
    best_amount, best_profit = 0, 0
    for amount in range(lo, hi + 1):
        p = profit(amount)
        if p > best_profit:
            best_amount, best_profit = amount, p
    return best_amount, best_profit
