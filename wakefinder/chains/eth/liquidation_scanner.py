"""Активное сканирование ликвидируемых позиций на Aave V3 — дополняет
РЕАКТИВНЫЙ liquidation_watcher.py (который видит только ЧУЖИЕ pending
liquidationCall в мемпуле, т.е. конкурирует только за уже кем-то найденную
возможность). Этот модуль ищет возможности сам, двумя шагами:

1. discover_borrowers() — кандидаты через историю событий Borrow (тот же
   принцип, что wallet_scanner.py для копитрейд-кошельков: чанкованный
   eth_getLogs за диапазон блоков, не archive-нода/сторонний subgraph).
2. scan_for_liquidatable() — для каждого кандидата: healthFactor через
   Pool.getUserAccountData() (АГРЕГАТ по всем резервам в base currency, это
   единственное, что даёт эта функция); для просевших (healthFactor < 1.0)
   — конкретный debt/collateral актив через
   PoolDataProvider.getUserReserveData(), перебором по сконфигурированным
   LIQUIDATION_DEBT_ASSETS/LIQUIDATION_COLLATERAL_ASSETS — getUserAccountData
   не даёт разбивку по активам, только сумму, поэтому без этого второго
   прохода нечем заполнить liquidationCall(collateralAsset, debtAsset, ...).

ЧЕСТНАЯ ГРАНИЦА №1 (debt_to_cover): консервативная оценка — 50% найденного
долга (дефолтный Aave V3 close factor), НЕ точный расчёт под
CLOSE_FACTOR_HF_THRESHOLD (полная логика требует ещё протокол-уровневых
параметров, которые здесь не переопределяются для конкретного резерва).
Если оценка завышена — Flashbots bundle-симуляция отклонит транзакцию ДО
broadcast, тот же fail-safe принцип, что у остальных стратегий проекта
(риск впустую потраченного тика сканирования, не риск денег).

ЧЕСТНАЯ ГРАНИЦА №2 (охват): borrowers — только те, кто брал Borrow ПОСЛЕ
from_block (обычно "с момента старта бота" за вычетом lookback-окна), не
полная история с генезиса контракта. Тот же trade-off, что у отсутствующего
Solscan-эквивалента в wallet_scanner.py: archive-нода или сторонний subgraph
— внешняя зависимость, которую проект последовательно избегает."""

import logging

from web3 import AsyncWeb3

from wakefinder.common.interfaces import PendingLiquidation

logger = logging.getLogger("wakefinder.eth.liquidation_scanner")

HEALTH_FACTOR_ONE = 10**18
DEFAULT_CLOSE_FACTOR_BPS = 5000  # Aave V3 дефолт: до 50% непогашенного долга за одну liquidationCall


async def discover_borrowers(w3: AsyncWeb3, pool, from_block: int, to_block: int, chunk_size: int = 2000) -> set[str]:
    """Уникальные onBehalfOf из событий Borrow за диапазон блоков."""
    borrowers: set[str] = set()
    block = from_block
    while block <= to_block:
        chunk_end = min(block + chunk_size - 1, to_block)
        logs = await pool.events.Borrow.get_logs(fromBlock=block, toBlock=chunk_end)
        for log in logs:
            borrowers.add(log["args"]["onBehalfOf"])
        block = chunk_end + 1
    return borrowers


async def _find_debt_and_collateral(
    data_provider, user: str, debt_assets: set[str], collateral_assets: set[str],
) -> tuple[str, str, int] | None:
    """Первая пара (ненулевой долг в debt_assets) x (ненулевой aToken-баланс,
    используемый как обеспечение, в collateral_assets) — первое совпадение,
    не оптимальный по размеру перебор, та же простота, что у остальных
    discovery-эвристик проекта (см. wallet_scanner.py)."""
    debt_asset = None
    debt_amount = 0
    for asset in debt_assets:
        data = await data_provider.functions.getUserReserveData(asset, user).call()
        current_stable_debt, current_variable_debt = data[1], data[2]
        total_debt = current_stable_debt + current_variable_debt
        if total_debt > 0:
            debt_asset = asset
            debt_amount = total_debt
            break
    if debt_asset is None:
        return None

    collateral_asset = None
    for asset in collateral_assets:
        data = await data_provider.functions.getUserReserveData(asset, user).call()
        a_token_balance, usage_as_collateral = data[0], data[8]
        if a_token_balance > 0 and usage_as_collateral:
            collateral_asset = asset
            break
    if collateral_asset is None:
        return None

    debt_to_cover = debt_amount * DEFAULT_CLOSE_FACTOR_BPS // 10_000
    return debt_asset, collateral_asset, debt_to_cover


async def scan_for_liquidatable(
    pool, data_provider, borrowers: set[str], debt_assets: set[str], collateral_assets: set[str],
) -> list[PendingLiquidation]:
    """healthFactor < 1.0 -> ищем debt/collateral пару -> PendingLiquidation
    (тот же тип, что и у реактивного watcher'а — значит
    chains/eth/liquidate.py:_handle_pending_liquidation обрабатывает
    самостоятельно найденные и подсмотренные в мемпуле возможности ОДНОЙ и
    той же логикой, без дублирования)."""
    candidates = []
    for user in borrowers:
        try:
            account_data = await pool.functions.getUserAccountData(user).call()
        except Exception as exc:
            logger.warning("getUserAccountData не удался для %s (%s)", user, type(exc).__name__)
            continue
        health_factor = account_data[5]
        if health_factor >= HEALTH_FACTOR_ONE:
            continue

        match = await _find_debt_and_collateral(data_provider, user, debt_assets, collateral_assets)
        if match is None:
            continue
        debt_asset, collateral_asset, debt_to_cover = match
        logger.info(
            "найдена ликвидируемая позиция user=%s healthFactor=%.4f debt_asset=%s collateral_asset=%s",
            user, health_factor / HEALTH_FACTOR_ONE, debt_asset, collateral_asset,
        )
        candidates.append(PendingLiquidation(
            tx_hash="", collateral_asset=collateral_asset, debt_asset=debt_asset, user=user, debt_to_cover=debt_to_cover,
        ))
    return candidates
