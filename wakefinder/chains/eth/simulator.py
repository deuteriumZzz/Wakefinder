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

Авто-обнаружение доп. референсных пулов (auto_discover_factories): помимо
явно заданного в reference_pools пула, симулятор может дополнительно
запросить getPair() у других известных Uniswap-V2-совместимых фабрик
(см. KNOWN_DEX_FACTORIES) на ту же пару токенов и просимулировать против
КАЖДОГО найденного пула — берётся тот, что даёт максимальную прибыль, а не
только предзаданный. Не добавляет РИСКА (min_reference_liquidity-проверка
и denylist применяются к каждому кандидату одинаково), просто не ограничивает
арбитраж одним заранее угаданным DEX.
"""

from web3 import AsyncWeb3

from wakefinder.chains.eth.abi import FACTORY_ABI, PAIR_ABI, ROUTER_ABI
from wakefinder.common.amm import apply_swap, optimal_arb
from wakefinder.common.config import get_settings
from wakefinder.common.interfaces import PendingSwap, SimResult, Simulator

GAS_LIMIT = 200_000  # должно совпадать с реальным лимитом газа на ногу при сборке транзакций
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Известные Uniswap-V2-совместимые DEX на mainnet: label -> (factory, router).
# Адреса те же, что уже используются как ETH_FACTORY_ADDRESS/KNOWN_ROUTERS в
# common/config.py — не новые, не угаданные.
KNOWN_DEX_FACTORIES = {
    "uniswap_v2": ("0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f", "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"),
    "sushiswap": ("0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac", "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F"),
}


class TwoPoolArbSimulator(Simulator):
    def __init__(
        self, w3: AsyncWeb3, target_router: str, reference_pools: dict[str, dict[str, str]], weth_address: str,
        auto_discover_factories: dict[str, tuple[str, str]] | None = None,
    ):
        """reference_pools: {адрес_целевого_пула: {"pool": адрес_референсного_пула, "router": адрес_референсного_роутера}}
        auto_discover_factories: {label: (адрес_фабрики, адрес_роутера)} — доп.
        DEX, на которых искать альтернативный референсный пул той же пары."""
        self.w3 = w3
        self.target_router = target_router
        self.reference_pools = {k.lower(): v for k, v in reference_pools.items()}
        self.weth_address = weth_address
        self.auto_discover_factories = auto_discover_factories or {}
        self._token0_cache: dict[str, str] = {}
        self._factory_pair_cache: dict[tuple[str, str, str], str | None] = {}

    async def _discover_pool(self, factory_address: str, token_a: str, token_b: str, block_number: int) -> str | None:
        key = (factory_address.lower(), token_a.lower(), token_b.lower())
        if key in self._factory_pair_cache:
            return self._factory_pair_cache[key]
        factory = self.w3.eth.contract(address=factory_address, abi=FACTORY_ABI)
        try:
            pair = await factory.functions.getPair(token_a, token_b).call(block_identifier=block_number)
        except Exception:
            self._factory_pair_cache[key] = None
            return None
        result = None if pair.lower() == ZERO_ADDRESS else pair
        self._factory_pair_cache[key] = result
        return result

    async def _eth_wei_to_token_in(self, token_in: str, eth_wei: int, block_number: int) -> int | None:
        """Конвертирует сумму в ETH wei (газ, кэп по капиталу) в эквивалент
        в единицах token_in через getAmountsOut на целевом роутере, на том же
        block_number, что и остальная симуляция (важно для бэктеста — иначе
        конвертация тихо использовала бы текущий live-курс для исторического
        свопа). Когда token_in уже WETH — просто возвращает как есть, без
        RPC-вызова (сохраняет текущее поведение/задержку для доминирующего
        случая). None означает "не удалось оценить" (нет прямой пары
        WETH/token_in на роутере) — вызывающий код должен трактовать это как
        "не торгуем", не как ноль."""
        if token_in.lower() == self.weth_address.lower():
            return eth_wei
        router = self.w3.eth.contract(address=self.target_router, abi=ROUTER_ABI)
        try:
            amounts = await router.functions.getAmountsOut(eth_wei, [self.weth_address, token_in]).call(block_identifier=block_number)
            return amounts[-1]
        except Exception:
            return None

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

    async def _simulate_against(
        self, ref_pool: str, ref_router: str, swap: PendingSwap, new_target_in: int, new_target_out: int,
        block_number: int, capital_cap_in_token_in: int, gas_cost_in_token_in: int, settings,
    ) -> SimResult:
        ref_reserve_in, ref_reserve_out = await self._reserves(ref_pool, swap.token_in, block_number)

        if ref_reserve_in < settings.min_reference_liquidity_eth * 10**18:
            return SimResult(profitable=False, expected_profit_wei=0, reason="референсный пул слишком тонкий — риск манипуляции ценой")

        upper_bound = min(capital_cap_in_token_in, ref_reserve_in, new_target_out)

        amount_in, profit = optimal_arb(
            buy_reserve_in=ref_reserve_in,
            buy_reserve_out=ref_reserve_out,
            sell_reserve_out=new_target_out,
            sell_reserve_in=new_target_in,
            gas_cost_wei=gas_cost_in_token_in,
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
            buy_router=ref_router,
            sell_router=self.target_router,
        )

    async def simulate(self, swap: PendingSwap, block_number: int | None = None) -> SimResult:
        """block_number=None (по умолчанию, прод) -> текущий блок сети — своп
        ещё pending, значит "текущее" состояние и есть состояние ДО его
        включения. Явный block_number — для бэктеста (wakefinder/backtest.py):
        состояние на block_number-1 относительно исторического блока свопа."""
        # Привязываем все чтения к одному блоку: целевой пул нужно прочитать
        # *до* того, как приземлится своп жертвы, затем продвинуть через
        # apply_swap(), чтобы смоделировать состояние после включения;
        # референсные пулы нужно прочитать в тот же момент, чтобы не сравнивать
        # цены из разных блоков.
        if block_number is None:
            block_number = await self.w3.eth.block_number

        target_reserve_in, target_reserve_out = await self._reserves(swap.pool_address, swap.token_in, block_number)
        new_target_in, new_target_out, _ = apply_swap(target_reserve_in, target_reserve_out, swap.amount_in)

        candidates: list[tuple[str, str]] = []
        ref = self.reference_pools.get(swap.pool_address.lower())
        if ref is not None:
            candidates.append((ref["pool"], ref["router"]))

        for factory_address, router_address in self.auto_discover_factories.values():
            discovered = await self._discover_pool(factory_address, swap.token_in, swap.token_out, block_number)
            if discovered and discovered.lower() not in {c[0].lower() for c in candidates}:
                candidates.append((discovered, router_address))

        if not candidates:
            return SimResult(profitable=False, expected_profit_wei=0, reason="референсный пул не настроен")

        settings = get_settings()

        # Кэп по капиталу и газ считаются в реальных ETH wei, затем конвертируются
        # в единицы token_in — раньше оба использовались как есть в предположении
        # token_in==WETH, что для любого другого token_in либо давало бессмысленный
        # кэп, либо (для газа) газ-стоимость в чужих единицах гарантированно топила
        # любую реальную прибыль. Быстрый путь без RPC-вызова сохранён для
        # доминирующего случая token_in==WETH — см. _eth_wei_to_token_in().
        capital_cap_eth_wei = int(settings.max_capital_per_bundle_eth * 10**18)
        gas_cost_eth_wei = 2 * GAS_LIMIT * int(settings.max_gas_gwei * 10**9)

        capital_cap_in_token_in = await self._eth_wei_to_token_in(swap.token_in, capital_cap_eth_wei, block_number)
        gas_cost_in_token_in = await self._eth_wei_to_token_in(swap.token_in, gas_cost_eth_wei, block_number)
        if capital_cap_in_token_in is None or gas_cost_in_token_in is None:
            return SimResult(
                profitable=False, expected_profit_wei=0,
                reason="не удалось сконвертировать ETH-стоимость газа/кэпа в token_in — нет прямой пары с WETH на роутере",
            )

        best: SimResult | None = None
        last_reason = "нет арбитража ни по одному референсному пулу"
        for pool_address, router_address in candidates:
            result = await self._simulate_against(
                pool_address, router_address, swap, new_target_in, new_target_out,
                block_number, capital_cap_in_token_in, gas_cost_in_token_in, settings,
            )
            if result.profitable and (best is None or result.expected_profit_wei > best.expected_profit_wei):
                best = result
            elif not result.profitable:
                last_reason = result.reason

        return best or SimResult(profitable=False, expected_profit_wei=0, reason=last_reason)
