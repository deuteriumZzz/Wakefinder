"""Симулятор арбитража между двумя пулами: после того как своп кита сдвигает
цену в `pool`, проверяем, предлагает ли `reference_pool` (пул той же пары на
другой DEX, не затронутый сделкой жертвы) теперь прибыльный круговой обмен.

Направление: своп жертвы делает token_out ДОРОЖЕ в целевом пуле (жертва только
что купила его там). Значит прибыльный порядок ног — купить token_out там, где
он ещё дёшев (референсный пул), затем продать его в целевой пул. Покупка в
целевом пуле вместо этого (наоборот) — анти-арбитраж; см. wakefinder/common/amm.py,
куда должно совпадать это направление по именам buy_*/sell_*.

Бэкран против одного-единственного пула — не арбитраж: без второго источника
ликвидности ничего не тянет цену обратно, отсюда обязательность референсного пула.
"""

from web3 import AsyncWeb3

from wakefinder.chains.eth.abi import PAIR_ABI
from wakefinder.common.amm import apply_swap, optimal_arb
from wakefinder.common.config import get_settings
from wakefinder.common.interfaces import PendingSwap, SimResult, Simulator

GAS_LIMIT = 200_000  # должно совпадать с реальным лимитом газа на ногу при сборке транзакций


class TwoPoolArbSimulator(Simulator):
    def __init__(self, w3: AsyncWeb3, target_router: str, reference_pools: dict[str, dict[str, str]]):
        """reference_pools: {адрес_целевого_пула: {"pool": адрес_референсного_пула, "router": адрес_референсного_роутера}}"""
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
            return SimResult(profitable=False, expected_profit_wei=0, reason="референсный пул не настроен")

        # Привязываем оба чтения к одному блоку: целевой пул нужно прочитать
        # *до* того, как приземлится своп жертвы, затем продвинуть через
        # apply_swap(), чтобы смоделировать состояние после включения;
        # референсный пул нужно прочитать в тот же момент, чтобы не сравнивать
        # цены из разных блоков.
        block_number = await self.w3.eth.block_number

        target_reserve_in, target_reserve_out = await self._reserves(swap.pool_address, swap.token_in, block_number)
        new_target_in, new_target_out, _ = apply_swap(target_reserve_in, target_reserve_out, swap.amount_in)

        ref_reserve_in, ref_reserve_out = await self._reserves(ref["pool"], swap.token_in, block_number)

        settings = get_settings()

        if ref_reserve_in < settings.min_reference_liquidity_eth * 10**18:
            return SimResult(profitable=False, expected_profit_wei=0, reason="референсный пул слишком тонкий — риск манипуляции ценой")

        # Кэп по капиталу применяем здесь (не постфактум в сборщике транзакций),
        # чтобы sim.amount_in/expected_profit_wei оставались согласованы со
        # сделкой, которую реально соберём и подпишем — кэп задним числом
        # незаметно обесценил бы цифру прибыли, так как прибыль от арбитража
        # не линейна по amount_in.
        #
        # ponytail: предполагается, что token_in — 18-decimal токен (например,
        # WETH) — кэп задан в сырых wei. Отклоняйте token_in, отличный от WETH,
        # на стороне вызывающего кода, пока это не учитывает decimals как надо.
        wei_cap = int(settings.max_capital_per_bundle_eth * 10**18)
        upper_bound = min(wei_cap, ref_reserve_in, new_target_out)

        # Газ на две ноги, в единицах token_in, в предположении что token_in — WETH
        # (см. заметку про wei_cap выше — то же допущение, чинить нужно вместе).
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
            return SimResult(profitable=False, expected_profit_wei=0, reason="нет арбитража с учётом газа после свопа жертвы")

        _, bought_amount, _ = apply_swap(ref_reserve_in, ref_reserve_out, amount_in)
        return SimResult(
            profitable=True,
            expected_profit_wei=profit,
            amount_in=amount_in,
            bought_amount=bought_amount,
            buy_router=ref["router"],
            sell_router=self.target_router,
        )
