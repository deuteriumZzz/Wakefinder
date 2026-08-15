"""Копитрейдинг: зеркалим ВХОД и ВЫХОД watchlist-кошельков, а не арбитражим
ценовой перекос от их сделки (сама арбитражная стратегия — в simulator.py).
Принципиально другой профиль риска: здесь бот реально держит позицию с
открытым направленным риском между входом и выходом — не атомарная сделка с
гарантированной на момент исполнения прибылью, как арбитраж.

Размер входа — ДОЛЯ ОТ НАШЕГО баланса (COPYTRADE_SIZE_PCT), не сумма кита:
у кита может быть бюджет в миллионы, у нас — сколько есть; повторяем
направление ставки, не её абсолютный размер.

Вход требует КОНСЕНСУСА: >= COPYTRADE_MIN_CONSENSUS_WALLETS разных watched-
кошельков должны купить один и тот же токен в течение
COPYTRADE_CONSENSUS_WINDOW_SECONDS — один кит может ошибаться, несколько
независимых китов почти одновременно — сильнее сигнал.

Осознанное отличие от арбитража: наши собственные сделки (вход и выход) идут
в ПУБЛИЧНЫЙ мемпул (`send_raw_transaction`), не через Flashbots. Watcher уже
реагирует на PENDING-транзакцию кита (не дожидаясь подтверждения), и здесь
нужна максимальная скорость входа, пока рынок ещё не отреагировал — relay-хоп
Flashbots этому не помогает. Плата за это: наша транзакция видна в публичном
мемпуле и теоретически может быть засэндвичена другими MEV-ботами — та же
атака, которую применяет арбитражная часть этого проекта против чужих сделок.

Выход по двум независимым триггерам:
- зеркальный: любой watched-кошелёк продаёт токен, который мы держим -> продаём
- стоп-лосс (фоновая задача): цена позиции упала на COPYTRADE_STOP_LOSS_PCT от
  цены входа -> продаём независимо от китов (защита на случай, если все они
  держат "до нуля" или мы упустили их выход)

ponytail: позиции хранятся в плоском JSON-файле, не в БД — при таком масштабе
(один бот, одна пара кошелёк/сеть) этого достаточно, и рестарт не теряет
открытые позиции молча.

Требует ОТДЕЛЬНОГО процесса от wakefinder.chains.eth.main (арбитраж) или
запуска не одновременно с ним при общем ETH_PRIVATE_KEY — иначе оба процесса
независимо считают nonce и конфликтуют друг с другом.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass

from eth_account import Account
from web3 import AsyncWeb3, Web3, WebsocketProviderV2

from wakefinder.chains.eth.abi import PAIR_ABI, ROUTER_ABI
from wakefinder.chains.eth.watcher import UniswapV2Watcher
from wakefinder.common import killswitch, trade_log
from wakefinder.common.alerts import send_telegram_alert
from wakefinder.common.amm import get_amount_out
from wakefinder.common.config import get_settings
from wakefinder.common.consensus import ConsensusTracker
from wakefinder.common.drawdown import check_drawdown

SLIPPAGE_BPS = 100
GAS_LIMIT = 200_000
RECEIPT_TIMEOUT_SECONDS = 60
RECEIPT_POLL_SECONDS = 2
_ENCODER = Web3()

logger = logging.getLogger("wakefinder.eth.copytrade")


@dataclass
class Position:
    token: str
    token_in: str
    pool_address: str
    amount_held: int
    entry_amount_in: int
    watched_wallet: str
    opened_at: float


def _load_positions(path: str) -> dict[str, Position]:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {k: Position(**v) for k, v in raw.items()}


def _save_positions(path: str, positions: dict[str, Position]) -> None:
    with open(path, "w") as f:
        json.dump({k: asdict(v) for k, v in positions.items()}, f, indent=2)


def _sign_swap(router_address, account, chain_id, nonce, max_fee, priority_fee, path, amount_in, amount_out_min) -> bytes:
    router = _ENCODER.eth.contract(address=router_address, abi=ROUTER_ABI)
    tx = router.functions.swapExactTokensForTokens(
        amount_in, amount_out_min, path, account.address, int(time.time()) + 60,
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gas": GAS_LIMIT,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "chainId": chain_id,
        }
    )
    return account.sign_transaction(tx).raw_transaction


async def _reserves(w3: AsyncWeb3, pool_address: str, token_in: str) -> tuple[int, int]:
    pool = w3.eth.contract(address=pool_address, abi=PAIR_ABI)
    r0, r1, _ = await pool.functions.getReserves().call()
    token0 = await pool.functions.token0().call()
    if token0.lower() == token_in.lower():
        return r0, r1
    return r1, r0


async def _wait_for_receipt(w3: AsyncWeb3, tx_hash, timeout_seconds: float = RECEIPT_TIMEOUT_SECONDS) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            receipt = await w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            receipt = None
        if receipt is not None:
            return receipt.get("status") == 1
        await asyncio.sleep(RECEIPT_POLL_SECONDS)
    return False  # не дождались — считаем неудачей, а не зависаем навсегда


async def _send_single_swap(
    w3: AsyncWeb3, account, router_address: str, chain_id: int,
    path: list[str], amount_in: int, amount_out_min: int,
) -> tuple[bool, str]:
    """Отправляет в публичный мемпул напрямую (не через Flashbots — см. docstring
    модуля про компромисс скорости против защиты от сэндвича). Возвращает
    (успех, tx_hash)."""
    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    raw = _sign_swap(router_address, account, chain_id, nonce, max_fee, priority_fee, path, amount_in, amount_out_min)
    tx_hash = await w3.eth.send_raw_transaction(raw)
    ok = await _wait_for_receipt(w3, tx_hash)
    return ok, tx_hash.hex()


async def _try_enter(
    w3, account, router_address, chain_id, token_in, token_out, pool_address, watched_wallet,
    size_pct, max_total_exposure_pct, positions, positions_lock, positions_file, trade_log_file,
):
    balance = await w3.eth.get_balance(account.address)

    async with positions_lock:
        if token_out.lower() in positions:
            return  # уже держим этот токен, не наращиваем позицию повторно
        current_exposure = sum(p.entry_amount_in for p in positions.values())

    # ponytail: тот же приём, что и в арбитраже — предполагаем token_in 18-decimal (WETH)
    amount_in = int(balance * size_pct / 100)
    exposure_cap = int(balance * max_total_exposure_pct / 100)
    if current_exposure + amount_in > exposure_cap:
        logger.info(
            "пропуск входа: суммарная экспозиция %d + новый вход %d превысили бы кэп %d",
            current_exposure, amount_in, exposure_cap,
        )
        return
    if amount_in <= 0:
        return

    reserve_in, reserve_out = await _reserves(w3, pool_address, token_in)
    expected_out = get_amount_out(amount_in, reserve_in, reserve_out)
    if expected_out <= 0:
        return
    amount_out_min = expected_out * (10_000 - SLIPPAGE_BPS) // 10_000

    included, tx_hash = await _send_single_swap(w3, account, router_address, chain_id, [token_in, token_out], amount_in, amount_out_min)
    logger.info("копитрейд-вход (консенсус): токен=%s триггер-кошелёк=%s amount_in=%d included=%s", token_out, watched_wallet, amount_in, included)
    trade_log.log_attempt(trade_log_file, "eth", pool_address, amount_in, included, [tx_hash], strategy="copytrade_entry", wallet=watched_wallet)
    if not included:
        return

    async with positions_lock:
        positions[token_out.lower()] = Position(
            token=token_out, token_in=token_in, pool_address=pool_address,
            amount_held=expected_out, entry_amount_in=amount_in,
            watched_wallet=watched_wallet, opened_at=time.time(),
        )
        _save_positions(positions_file, positions)


async def _exit_position(w3, account, router_address, chain_id, token: str, reason: str, positions, positions_lock, positions_file, trade_log_file):
    async with positions_lock:
        pos = positions.get(token.lower())
        if pos is None:
            return
        del positions[token.lower()]
        _save_positions(positions_file, positions)

    reserve_in, reserve_out = await _reserves(w3, pos.pool_address, pos.token)  # pos.token — то, что продаём
    expected_out = get_amount_out(pos.amount_held, reserve_in, reserve_out)
    amount_out_min = expected_out * (10_000 - SLIPPAGE_BPS) // 10_000

    included, tx_hash = await _send_single_swap(
        w3, account, router_address, chain_id, [pos.token, pos.token_in], pos.amount_held, amount_out_min
    )
    logger.info("копитрейд-выход (%s): токен=%s included=%s", reason, token, included)
    trade_log.log_attempt(trade_log_file, "eth", pos.pool_address, expected_out, included, [tx_hash], strategy="copytrade_exit", wallet=pos.watched_wallet)
    if reason == "стоп-лосс":
        settings = get_settings()
        send_telegram_alert(settings.telegram_bot_token, settings.telegram_chat_id, f"[wakefinder/eth copytrade] стоп-лосс: токен={token} included={included}")


async def _stop_loss_loop(w3, account, router_address, chain_id, positions, positions_lock, positions_file, trade_log_file, stop_loss_pct, interval_seconds):
    while True:
        await asyncio.sleep(interval_seconds)
        async with positions_lock:
            snapshot = dict(positions)
        for token, pos in snapshot.items():
            try:
                reserve_in, reserve_out = await _reserves(w3, pos.pool_address, pos.token)
                current_value = get_amount_out(pos.amount_held, reserve_in, reserve_out)
            except Exception as exc:
                logger.warning("не удалось проверить цену позиции %s (%s)", token, type(exc).__name__)
                continue
            floor = pos.entry_amount_in * (100 - stop_loss_pct) // 100
            if current_value < floor:
                await _exit_position(w3, account, router_address, chain_id, token, "стоп-лосс", positions, positions_lock, positions_file, trade_log_file)


async def run(watched_wallets: frozenset[str], token_allowlist: frozenset[str] = frozenset()):
    settings = get_settings()
    account = Account.from_key(settings.eth_private_key.get_secret_value())

    positions = _load_positions(settings.copytrade_positions_file)
    positions_lock = asyncio.Lock()
    consensus = ConsensusTracker(settings.copytrade_min_consensus_wallets, settings.copytrade_consensus_window_seconds)
    last_drawdown_check = 0.0

    provider = WebsocketProviderV2(settings.eth_rpc_ws_url.get_secret_value())
    async with AsyncWeb3.persistent_websocket(provider) as w3:
        chain_id = await w3.eth.chain_id
        watcher = UniswapV2Watcher(
            w3, settings.eth_router_address, pool_registry={}, min_amount_in=2**256 - 1,
            watched_wallets=watched_wallets, factory_address=settings.eth_factory_address,
        )

        stop_loss_task = asyncio.create_task(
            _stop_loss_loop(
                w3, account, settings.eth_router_address, chain_id, positions, positions_lock,
                settings.copytrade_positions_file, settings.trade_log_file, settings.copytrade_stop_loss_pct,
                settings.copytrade_stop_loss_check_interval_seconds,
            )
        )

        try:
            async for swap in watcher.watch():
                if killswitch.is_engaged(settings.kill_switch_file):
                    logger.warning("kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
                    send_telegram_alert(settings.telegram_bot_token, settings.telegram_chat_id, "[wakefinder/eth copytrade] kill switch присутствует — бот остановлен")
                    return

                now = time.time()
                if now - last_drawdown_check >= settings.drawdown_check_interval_seconds:
                    last_drawdown_check = now
                    status = check_drawdown(settings.trade_log_file, "eth", settings.drawdown_window_seconds, int(settings.max_drawdown_eth * 10**18))
                    if status.breached:
                        logger.critical("просадка за окно %d wei превысила лимит — включаю kill switch", status.realized_pnl)
                        send_telegram_alert(
                            settings.telegram_bot_token, settings.telegram_chat_id,
                            f"[wakefinder/eth copytrade] просадка {status.realized_pnl} wei превысила лимит — kill switch",
                        )
                        killswitch.engage(settings.kill_switch_file, "drawdown breach: eth copytrade")
                        return

                if token_allowlist and swap.token_out.lower() not in {t.lower() for t in token_allowlist}:
                    continue

                async with positions_lock:
                    holds_token_in = swap.token_in.lower() in positions

                if holds_token_in:
                    await _exit_position(
                        w3, account, settings.eth_router_address, chain_id, swap.token_in,
                        "зеркальный выход за китом", positions, positions_lock, settings.copytrade_positions_file, settings.trade_log_file,
                    )
                    consensus.clear(swap.token_in)
                    continue

                # найден сигнал покупки от watched-кошелька -> копим консенсус,
                # входим только когда наберётся нужное число РАЗНЫХ кошельков
                reached = consensus.record_buy(swap.token_out, swap.sender)
                if not reached:
                    continue
                consensus.clear(swap.token_out)

                await _try_enter(
                    w3, account, settings.eth_router_address, chain_id, swap.token_in, swap.token_out,
                    swap.pool_address, swap.sender, settings.copytrade_size_pct, settings.copytrade_max_total_exposure_pct,
                    positions, positions_lock, settings.copytrade_positions_file, settings.trade_log_file,
                )
        finally:
            stop_loss_task.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(watched_wallets=frozenset()))
