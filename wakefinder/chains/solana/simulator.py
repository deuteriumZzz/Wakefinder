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
    def __init__(self, client: AsyncClient, reference_pools: dict[str, dict[str, str]]):
        """reference_pools: {pool_id_целевого_пула: {"base_vault":.., "quote_vault":.., "base_mint":.., "quote_mint":.., "dex_label":.., "target_base_vault":.., "target_quote_vault":.., "target_dex_label":..}}"""
        self.client = client
        self.reference_pools = reference_pools

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

        # ponytail: тот же приём, что и в ETH-версии — предполагаем, что
        # token_in имеет 9 decimals (wrapped SOL), кэп задан в сырых lamports.
        lamports_cap = int(settings.max_capital_per_bundle_sol * 10**9)
        upper_bound = min(lamports_cap, ref_reserve_in, target_reserve_out)

        gas_cost_lamports = 2 * ESTIMATED_TX_FEE_LAMPORTS  # две ноги + tip-транзакция

        amount_in, profit = optimal_arb(
            buy_reserve_in=ref_reserve_in,
            buy_reserve_out=ref_reserve_out,
            sell_reserve_out=target_reserve_out,
            sell_reserve_in=target_reserve_in,
            gas_cost_wei=gas_cost_lamports,
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
