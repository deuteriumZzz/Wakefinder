"""Симулятор арбитража между двумя пулами для Solana. Проще, чем ETH-версия:
там приходилось СИМУЛИРОВАТЬ эффект ещё не приземлившейся транзакции жертвы
(apply_swap поверх старых резервов), здесь own watcher уже сообщает о
подтверждённом изменении — резервы целевого пула читаем текущими, они уже
отражают состояние ПОСЛЕ свопа. Не нужно ничего домысливать поверх старого
состояния.

Направление то же самое, что в ETH-версии (см. wakefinder/common/amm.py):
покупаем token_out там, где он ещё дёшев (референсный пул), продаём там, где
он только что стал дороже (целевой пул).
"""

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

from wakefinder.common.amm import apply_swap, optimal_arb
from wakefinder.common.config import get_settings
from wakefinder.common.interfaces import PendingSwap, SimResult, Simulator

# Комиссия за одну Jito-bundle-транзакцию — не совпадает по природе с ETH-газом
# (Solana берёт fee за сигнатуру + priority fee за compute unit), но для оценки
# net-of-fee профита достаточно консервативной фиксированной оценки в lamports.
ESTIMATED_TX_FEE_LAMPORTS = 10_000


class TwoPoolArbSimulator(Simulator):
    def __init__(self, client: AsyncClient, reference_pools: dict[str, dict[str, str]], wsol_mint: str, jupiter=None):
        """reference_pools: {pool_id_целевого_пула: {"base_vault":.., "quote_vault":.., "base_mint":.., "quote_mint":.., "dex_label":.., "target_base_vault":.., "target_quote_vault":.., "target_dex_label":..}}.
        jupiter — опционален: нужен только когда token_in != wsol_mint (см. _lamports_to_token_in)."""
        self.client = client
        self.reference_pools = reference_pools
        self.wsol_mint = wsol_mint
        self.jupiter = jupiter

    async def _lamports_to_token_in(self, token_in: str, lamports: int) -> int | None:
        """Конвертирует сумму в lamports (газ, кэп по капиталу) в эквивалент в
        единицах token_in через Jupiter-квоту wSOL->token_in. Когда token_in
        уже wSOL — возвращает как есть, без RPC-вызова (быстрый путь для
        доминирующего случая). None означает "не удалось оценить" (нет
        jupiter-клиента или для пары wSOL/token_in нет маршрута)."""
        if token_in.lower() == self.wsol_mint.lower():
            return lamports
        if self.jupiter is None:
            return None
        try:
            quote = await self.jupiter.quote(
                input_mint=self.wsol_mint, output_mint=token_in, amount=lamports,
                slippage_bps=100, only_direct_routes=True,
            )
            return int(quote["outAmount"])
        except Exception:
            return None

    async def _reserves(self, base_vault: str, quote_vault: str, base_mint: str, token_in: str) -> tuple[int, int]:
        base_resp = await self.client.get_token_account_balance(Pubkey.from_string(base_vault))
        quote_resp = await self.client.get_token_account_balance(Pubkey.from_string(quote_vault))
        base = int(base_resp.value.amount)
        quote = int(quote_resp.value.amount)
        if base_mint.lower() == token_in.lower():
            return base, quote
        return quote, base

    async def simulate(self, swap: PendingSwap) -> SimResult:
        ref = self.reference_pools.get(swap.pool_address)
        if ref is None:
            return SimResult(profitable=False, expected_profit_wei=0, reason="референсный пул не настроен")

        target_reserve_in, target_reserve_out = await self._reserves(
            ref["target_base_vault"], ref["target_quote_vault"], ref["base_mint"], swap.token_in
        )
        ref_reserve_in, ref_reserve_out = await self._reserves(
            ref["base_vault"], ref["quote_vault"], ref["base_mint"], swap.token_in
        )

        settings = get_settings()

        if ref_reserve_in < settings.min_reference_liquidity_sol * 10**9:
            return SimResult(profitable=False, expected_profit_wei=0, reason="референсный пул слишком тонкий — риск манипуляции ценой")

        # Кэп по капиталу и газ считаются в реальных lamports, затем
        # конвертируются в единицы token_in через Jupiter — см. docstring
        # _lamports_to_token_in(). Быстрый путь без quote-запроса сохранён
        # для доминирующего случая token_in==wSOL.
        capital_cap_lamports = int(settings.max_capital_per_bundle_sol * 10**9)
        gas_cost_lamports = 2 * ESTIMATED_TX_FEE_LAMPORTS  # две ноги + tip-транзакция

        capital_cap_in_token_in = await self._lamports_to_token_in(swap.token_in, capital_cap_lamports)
        gas_cost_in_token_in = await self._lamports_to_token_in(swap.token_in, gas_cost_lamports)
        if capital_cap_in_token_in is None or gas_cost_in_token_in is None:
            return SimResult(
                profitable=False, expected_profit_wei=0,
                reason="не удалось сконвертировать lamports-стоимость газа/кэпа в token_in — нет маршрута wSOL/token_in",
            )

        upper_bound = min(capital_cap_in_token_in, ref_reserve_in, target_reserve_out)

        amount_in, profit = optimal_arb(
            buy_reserve_in=ref_reserve_in,
            buy_reserve_out=ref_reserve_out,
            sell_reserve_out=target_reserve_out,
            sell_reserve_in=target_reserve_in,
            gas_cost_wei=gas_cost_in_token_in,
            upper_bound=upper_bound,
        )
        if profit <= 0 or amount_in <= 0:
            return SimResult(profitable=False, expected_profit_wei=0, reason="нет net-of-fee арбитража после свопа")

        _, bought_amount, _ = apply_swap(ref_reserve_in, ref_reserve_out, amount_in)
        return SimResult(
            profitable=True,
            expected_profit_wei=profit,
            amount_in=amount_in,
            bought_amount=bought_amount,
            buy_router=ref["dex_label"],
            sell_router=ref.get("target_dex_label", ref["dex_label"]),
        )
