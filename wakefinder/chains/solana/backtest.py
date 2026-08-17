"""Бэктест Solana backrun-арбитражной стратегии на исторической истории
изменений резервов пулов — честный аналог wakefinder/backtest.py (ETH), но
ПО ДРУГОМУ источнику данных: у Solana нет эквивалента eth_call(block_identifier=...)
для произвольного исторического состояния аккаунта (архивный узел с этой
возможностью не входит в стандартный публичный JSON-RPC), и нет
верифицированного парсера сырых Raydium/Orca swap-инструкций — та же причина,
по которой снайпинг на Solana не парсит их напрямую (см. mint_watcher.py).

Вместо парсинга swap-инструкции — тот же принцип, что уже работает в LIVE-коде
(chains/solana/watcher.py:RaydiumVaultWatcher): не декодируем, ЧТО за
инструкция изменила резервы, а читаем итоговый БАЛАНС vault-аккаунта, который
транзакция уже подтвердила (RPC-нода сделала эту работу сама). "Своп" здесь —
транзакция, где баланс ровно ОДНОЙ стороны vault'а вырос, а другой упал (тот
же критерий дельты, что у RaydiumVaultWatcher, только по meta.preTokenBalances/
postTokenBalances ОДНОЙ транзакции вместо двух последовательных
accountSubscribe-уведомлений).

Честные известные ограничения:
- Глубина истории ограничена тем, что хранит конкретный RPC-провайдер (тот же
  класс ограничения, что chunk_size/getLogs-глубина у ETH-версии).
- `contested_opportunities` (см. BacktestResult) — тот же грубый прокси
  конкуренции, что в ETH-версии, только по слотам вместо блоков: сколько
  прибыльных возможностей приходится на слот, где ЦЕЛЕВОЙ vault тронула
  больше чем одна транзакция. НЕ число реальных конкурентов — платных
  исторических данных о том, кто ещё отправлял бандлы, у публичных RPC нет.
- Резервы РЕФЕРЕНСНОГО пула на момент каждого свопа цели реконструируются как
  "последняя транзакция этого пула не позже данного слота" — если референсный
  пул торговался редко, момент может быть заметно раньше по времени (тот же
  класс допущения, что "текущие настройки вместо исторических условий сети" в
  ETH-версии).
- account_index в meta.*TokenBalances, указывающий ЗА пределы статичного
  message.account_keys (аккаунт подгружен через Address Lookup Table), не
  резолвится — такие транзакции пропускаются, не считаются свопом. Апгрейд:
  дочитать meta.loaded_addresses (writable ++ readonly) и расширить список
  ключей перед резолвом индекса.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature

from wakefinder.common.amm import optimal_arb
from wakefinder.common.config import get_settings

logger = logging.getLogger("wakefinder.solana.backtest")

GAS_COST_LAMPORTS = 20_000  # две ноги + tip — та же оценка, что ESTIMATED_TX_FEE_LAMPORTS*2 в live simulator.py


@dataclass
class BacktestResult:
    swaps_scanned: int
    opportunities_found: int
    total_simulated_profit_lamports: int
    contested_opportunities: int = 0  # см. docstring модуля — грубый прокси, не число реальных конкурентов


def _resolve_balance(meta, account_keys: list[str], vault_address: str, field: str) -> int | None:
    for b in getattr(meta, field) or []:
        if b.account_index >= len(account_keys):
            continue  # ALT-подгруженный аккаунт — не резолвим, см. docstring модуля
        if account_keys[b.account_index] == vault_address:
            return int(b.ui_token_amount.amount)
    return None


async def _target_swap_from_tx(client: AsyncClient, sig: Signature, base_vault: str, quote_vault: str, base_mint: str, quote_mint: str):
    """(token_in, post_base, post_quote) для транзакции, где РОВНО одна
    сторона vault'а выросла — то же определение "своп", что у
    RaydiumVaultWatcher.watch(). None — не своп (добавление/вывод ликвидности,
    ALT-аккаунт, или транзакция вообще не найдена)."""
    tx_resp = await client.get_transaction(sig, encoding="jsonParsed", max_supported_transaction_version=0)
    if tx_resp.value is None:
        return None
    meta = tx_resp.value.transaction.meta
    account_keys = [k.pubkey for k in tx_resp.value.transaction.transaction.message.account_keys]

    pre_base = _resolve_balance(meta, account_keys, base_vault, "pre_token_balances")
    pre_quote = _resolve_balance(meta, account_keys, quote_vault, "pre_token_balances")
    post_base = _resolve_balance(meta, account_keys, base_vault, "post_token_balances")
    post_quote = _resolve_balance(meta, account_keys, quote_vault, "post_token_balances")
    if None in (pre_base, pre_quote, post_base, post_quote):
        return None

    delta_base = post_base - pre_base
    delta_quote = post_quote - pre_quote
    if delta_base > 0 and delta_quote < 0:
        return base_mint, post_base, post_quote
    if delta_quote > 0 and delta_base < 0:
        return quote_mint, post_base, post_quote
    return None  # оба выросли/упали/не изменились — не своп


async def _closest_reserves_at_or_before(client: AsyncClient, base_vault: str, quote_vault: str, before_sig: Signature) -> tuple[int, int] | None:
    sigs_resp = await client.get_signatures_for_address(Pubkey.from_string(base_vault), before=before_sig, limit=5)
    for entry in sigs_resp.value:
        tx_resp = await client.get_transaction(entry.signature, encoding="jsonParsed", max_supported_transaction_version=0)
        if tx_resp.value is None:
            continue
        meta = tx_resp.value.transaction.meta
        account_keys = [k.pubkey for k in tx_resp.value.transaction.transaction.message.account_keys]
        base = _resolve_balance(meta, account_keys, base_vault, "post_token_balances")
        quote = _resolve_balance(meta, account_keys, quote_vault, "post_token_balances")
        if base is not None and quote is not None:
            return base, quote
    return None


async def run_backtest(client: AsyncClient, reference_pools: dict[str, dict[str, str]], limit_per_pool: int = 1000) -> BacktestResult:
    """reference_pools: та же структура, что chains/solana/main.py —
    {pool_id: {base_vault, quote_vault, base_mint, quote_mint,
    target_base_vault, target_quote_vault}}. limit_per_pool — сколько
    последних сигнатур целевого vault'а просканировать (глубина истории, тот
    же принцип, что chunk_size у ETH-версии)."""
    settings = get_settings()
    scanned = 0
    found = 0
    total_profit = 0
    contested = 0

    for ref in reference_pools.values():
        target_base_vault, target_quote_vault = ref["target_base_vault"], ref["target_quote_vault"]
        base_mint, quote_mint = ref["base_mint"], ref["quote_mint"]

        sigs_resp = await client.get_signatures_for_address(Pubkey.from_string(target_base_vault), limit=limit_per_pool)

        # Сколько транзакций этого vault'а пришлось на каждый слот — прокси
        # "оживлённости" слота, см. docstring модуля.
        txs_per_slot: dict[int, int] = {}
        for entry in sigs_resp.value:
            txs_per_slot[entry.slot] = txs_per_slot.get(entry.slot, 0) + 1

        for entry in reversed(sigs_resp.value):  # от старых к новым — хронологический порядок
            swap = await _target_swap_from_tx(client, entry.signature, target_base_vault, target_quote_vault, base_mint, quote_mint)
            if swap is None:
                continue
            scanned += 1
            token_in, target_base, target_quote = swap

            ref_reserves = await _closest_reserves_at_or_before(client, ref["base_vault"], ref["quote_vault"], before_sig=entry.signature)
            if ref_reserves is None:
                continue
            ref_base, ref_quote = ref_reserves

            if token_in == base_mint:
                buy_reserve_in, buy_reserve_out = ref_base, ref_quote
                sell_reserve_in, sell_reserve_out = target_base, target_quote
            else:
                buy_reserve_in, buy_reserve_out = ref_quote, ref_base
                sell_reserve_in, sell_reserve_out = target_quote, target_base

            if buy_reserve_in < settings.min_reference_liquidity_sol * 10**9:
                continue  # тонкий референсный пул — тот же порог, что live simulator.py

            upper_bound = min(int(settings.max_capital_per_bundle_sol * 10**9), buy_reserve_in, sell_reserve_out)
            amount_in, profit = optimal_arb(
                buy_reserve_in=buy_reserve_in, buy_reserve_out=buy_reserve_out,
                sell_reserve_out=sell_reserve_out, sell_reserve_in=sell_reserve_in,
                gas_cost_wei=GAS_COST_LAMPORTS, upper_bound=upper_bound,
            )
            if profit > 0 and amount_in > 0:
                found += 1
                total_profit += profit
                if txs_per_slot[entry.slot] > 1:
                    contested += 1

    return BacktestResult(
        swaps_scanned=scanned, opportunities_found=found, total_simulated_profit_lamports=total_profit,
        contested_opportunities=contested,
    )


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Бэктест Solana-арбитража на исторических изменениях резервов пулов (заглушка с пустым reference_pools — см. docstring модуля)")
    parser.add_argument("--limit-per-pool", type=int, default=1000)
    args = parser.parse_args()

    settings = get_settings()
    client = AsyncClient(settings.solana_rpc_http_url.get_secret_value())
    try:
        result = await run_backtest(client, reference_pools={}, limit_per_pool=args.limit_per_pool)
    finally:
        await client.close()
    print(f"Просканировано свопов: {result.swaps_scanned}")
    print(f"Найдено возможностей: {result.opportunities_found}")
    print(f"Суммарная симулированная прибыль: {result.total_simulated_profit_lamports} lamports")
    if result.opportunities_found:
        pct = result.contested_opportunities / result.opportunities_found * 100
        print(f"Из них с признаком конкуренции (несколько транзакций в слоте): {result.contested_opportunities} ({pct:.0f}%)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
