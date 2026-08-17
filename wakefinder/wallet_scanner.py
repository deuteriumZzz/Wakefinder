"""Обнаружение кандидатов в watchlist ("умные" кошельки) — не автоматический
рейтинг прибыльности (для этого нужна была бы полная реконструкция истории
входов/выходов каждого кандидата по реальным ценам, что по сложности не
уступало бы полноценному бэктестингу под каждого кандидата отдельно), а
скромный, честно реализуемый первый фильтр:

1. find_candidate_wallets_eth() / find_candidate_wallets_solana() — кто
   вообще торговал в интересующих пулах и как часто, через тот же RPC, что
   и live/backtest-режимы — без нового источника данных.
2. filter_by_etherscan_activity() — опциональный фильтр по числу транзакций
   кошелька через бесплатный Etherscan API (отсеивает свежие/одноразовые
   sybil-адреса) — без ETHERSCAN_API_KEY просто пропускается, не ошибка.

Результат — список КАНДИДАТОВ для watched_wallets, не готовый вердикт;
финальное решение (действительно ли этот кошелёк стабильно прибылен)
по-прежнему требует человека или внешней аналитики (Nansen/Arkham/DeBank,
или wakefinder.common.wallet_stats после того как кошелёк уже добавлен в
watchlist и накопил историю в trade_log.jsonl) — это дешёвый первый проход
перед тем решением, не замена ему.

ponytail (ETH): кандидат == Swap.to (получатель токенов) — корректно для
single-hop свопов (path длины 2, тот же случай, что покрывает watcher.py);
для мультихопов `to` промежуточного лега — адрес следующего пула, не
человек, может засорить кандидатов адресами роутеров/пулов. Отдельного
"похоже на контракт" фильтра нет — байткод-эвристика для этого ненадёжна,
тот же компромисс, что в docstring allowlist.py.

Solscan-эквивалент filter_by_etherscan_activity() не реализован (Solscan
Public API требует отдельной регистрации и имеет более жёсткие лимиты, чем
классический Etherscan API) — честно отложено, не тихий недосмотр.
"""

import asyncio
import logging
from collections import Counter

import requests
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey
from web3 import AsyncWeb3

from wakefinder.chains.eth.abi import PAIR_ABI

logger = logging.getLogger("wakefinder.wallet_scanner")

ETHERSCAN_API_URL = "https://api.etherscan.io/api"


async def find_candidate_wallets_eth(
    w3: AsyncWeb3, pool_addresses: list[str], from_block: int, to_block: int, chunk_size: int = 2000,
) -> Counter:
    """Counter{wallet_address: количество свопов} по заданным пулам за
    диапазон блоков — чем чаще кошелёк торгует в интересующем пуле, тем
    выше приоритет присмотреться к нему вручную."""
    counts: Counter = Counter()
    for pool_address in pool_addresses:
        pool = w3.eth.contract(address=pool_address, abi=PAIR_ABI)
        block = from_block
        while block <= to_block:
            chunk_end = min(block + chunk_size - 1, to_block)
            logs = await pool.events.Swap.get_logs(fromBlock=block, toBlock=chunk_end)
            for log in logs:
                counts[log["args"]["to"].lower()] += 1
            block = chunk_end + 1
    return counts


def filter_by_etherscan_activity(candidates: list[str], api_key: str, min_tx_count: int = 10) -> list[str]:
    """Оставляет кандидатов с >= min_tx_count транзакций (грубый фильтр
    против свежих/одноразовых адресов). Без api_key возвращает candidates
    как есть (фильтр пропущен). Сбой запроса для конкретного адреса ->
    адрес НЕ включается (fail-closed — молчаливое включение непроверенного
    адреса было бы хуже, чем пропуск сомнительного)."""
    if not api_key:
        return candidates
    survivors = []
    for address in candidates:
        try:
            resp = requests.get(
                ETHERSCAN_API_URL,
                params={
                    "module": "account", "action": "txlist", "address": address,
                    "page": 1, "offset": min_tx_count, "sort": "asc", "apikey": api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json().get("result")
            tx_count = len(result) if isinstance(result, list) else 0
        except Exception as exc:
            logger.warning("не удалось проверить активность %s через Etherscan (%s)", address, type(exc).__name__)
            continue
        if tx_count >= min_tx_count:
            survivors.append(address)
    return survivors


async def check_deployer_reputation(w3: AsyncWeb3, pool_tx_hash: str, api_key: str, min_tx_count: int) -> bool:
    """Опциональный анти-burner-wallet фильтр для снайпинга ETH
    (chains/eth/snipe.py) — деплойер пула (адрес, отправивший транзакцию
    PairCreated) с ОЧЕНЬ малой историей транзакций — классический признак
    кошелька, созданного специально под один rug. Переиспользует уже
    существующую filter_by_etherscan_activity (тот же фильтр, что и
    discover-команда для watched_wallets), через asyncio.to_thread — она
    синхронная (requests.get), нельзя звать напрямую из async live-цикла.

    Без api_key возвращает True (проверка пропущена, не блокирует вход) —
    тот же принцип "мягкого выключения", что у filter_by_etherscan_activity.
    Solana-эквивалента нет — см. docstring модуля про Solscan API."""
    if not api_key:
        return True
    tx = await w3.eth.get_transaction(pool_tx_hash)
    deployer = tx["from"]
    survivors = await asyncio.to_thread(filter_by_etherscan_activity, [deployer], api_key, min_tx_count)
    return deployer in survivors


async def find_candidate_wallets_solana(
    client: AsyncClient, vault_addresses: list[str], limit: int = 1000,
) -> Counter:
    """Counter{wallet_address: количество появлений} — сканирует последние
    `limit` подтверждённых транзакций на каждый vault-аккаунт
    (getSignaturesForAddress, тот же принцип, что в chains/solana/wallet_watcher.py,
    но ретроспективно вместо live-подписки) и извлекает владельцев из
    preTokenBalances/postTokenBalances — тот же DEX-агностичный
    balance-diff подход, не декодер конкретных DEX-инструкций."""
    counts: Counter = Counter()
    for vault_address in vault_addresses:
        pubkey = Pubkey.from_string(vault_address)
        sigs_resp = await client.get_signatures_for_address(pubkey, limit=limit, commitment=Confirmed)
        for sig_info in sigs_resp.value:
            if sig_info.err is not None:
                continue
            try:
                tx_resp = await client.get_transaction(
                    sig_info.signature, encoding="jsonParsed", commitment=Confirmed, max_supported_transaction_version=0
                )
            except Exception as exc:
                logger.warning("не удалось получить транзакцию %s (%s)", sig_info.signature, type(exc).__name__)
                continue
            if tx_resp.value is None:
                continue
            meta = tx_resp.value.transaction.meta
            if meta is None or meta.pre_token_balances is None or meta.post_token_balances is None:
                continue
            owners = {str(b.owner) for b in meta.pre_token_balances} | {str(b.owner) for b in meta.post_token_balances}
            for owner in owners:
                counts[owner] += 1
    return counts
