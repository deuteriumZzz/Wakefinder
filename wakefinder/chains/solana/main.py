"""Полный цикл Solana: слежка за резервами -> симуляция -> сборка бандла (2 ноги
свопа + tip) -> отправка в Jito.

Ноги свопа строит Jupiter (`jupiter_python_sdk`) — проверенный агрегатор, а не
собственная кодировка инструкций Raydium (там ~18 аккаунтов и PDA-деривации,
которые нельзя проверить без devnet). Плата за это: таргетинг на конкретный
пул — на уровне DEX через `exclude_dexes`, не гарантированно на уровне
конкретного pool_id. Задокументированное ограничение, не тихая недоработка.

В отличие от Ethereum, здесь не нужно тащить чужую (victim) транзакцию в
бандл — арбитраж строится поверх уже подтверждённого состояния, которое видит
watcher. Бандл = [buy_leg, sell_leg, tip] — три подписанные транзакции,
атомарность обеспечивает Jito (bundle либо весь исполняется, либо не попадает
в блок вообще).

Требует, чтобы кошелёк SOLANA_PRIVATE_KEY уже имел ассоциированные token-аккаунты
и достаточный SPL-баланс token_in — Jupiter сам не создаёт ATA на лету для
каждого маршрута.
"""

import asyncio
import base64
import logging
import os

from jupiter_python_sdk.jupiter import Jupiter
from solana.rpc.async_api import AsyncClient
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address

from wakefinder.chains.solana.sender import JitoBundleSender, to_base64
from wakefinder.chains.solana.simulator import TwoPoolArbSimulator
from wakefinder.chains.solana.watcher import RaydiumVaultWatcher
from wakefinder.common.config import get_settings
from wakefinder.common.interfaces import Bundle, PendingSwap, SimResult

SLIPPAGE_BPS = 100  # допуск 1%, тот же принцип что и в ETH-версии
FEE_RESERVE_LAMPORTS = 50_000  # запас на fee + tip + rent, консервативная оценка

logger = logging.getLogger("wakefinder.solana")


def _sign_unsigned_tx(unsigned_b64: str, keypair: Keypair) -> VersionedTransaction:
    raw = VersionedTransaction.from_bytes(base64.b64decode(unsigned_b64))
    signature = keypair.sign_message(to_bytes_versioned(raw.message))
    return VersionedTransaction.populate(raw.message, [signature])


def _build_tip_tx(keypair: Keypair, tip_account: str, tip_lamports: int, blockhash: Hash) -> VersionedTransaction:
    ix = transfer(
        TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=Pubkey.from_string(tip_account), lamports=tip_lamports)
    )
    msg = Message.new_with_blockhash([ix], keypair.pubkey(), blockhash)
    return VersionedTransaction.populate(msg, [keypair.sign_message(to_bytes_versioned(msg))])


def _tip_lamports(expected_profit_wei: int, profit_share_bps: int) -> int:
    return max(1000, expected_profit_wei * profit_share_bps // 10_000)


async def _has_sufficient_balance(client: AsyncClient, owner: Pubkey, token_mint: str, amount_in: int) -> bool:
    sol_balance = (await client.get_balance(owner)).value
    if sol_balance < FEE_RESERVE_LAMPORTS:
        logger.warning("недостаточно SOL на комиссии: есть=%d нужно=%d", sol_balance, FEE_RESERVE_LAMPORTS)
        return False

    ata = get_associated_token_address(owner, Pubkey.from_string(token_mint))
    try:
        resp = await client.get_token_account_balance(ata)
    except Exception:
        logger.warning("нет token-аккаунта для %s у кошелька — пропускаем возможность", token_mint)
        return False
    if int(resp.value.amount) < amount_in:
        logger.warning("недостаточно баланса token_in: нужно=%d токен=%s", amount_in, token_mint)
        return False
    return True


async def run(pools: dict[str, dict[str, str]], reference_pools: dict[str, dict[str, str]], min_amount_in: int):
    settings = get_settings()
    if not (settings.solana_rpc_ws_url and settings.solana_rpc_http_url and settings.solana_private_key):
        raise RuntimeError(
            "SOLANA_RPC_WS_URL / SOLANA_RPC_HTTP_URL / SOLANA_PRIVATE_KEY не настроены — "
            "Solana-путь не может стартовать без них"
        )

    keypair = Keypair.from_base58_string(settings.solana_private_key.get_secret_value())
    client = AsyncClient(settings.solana_rpc_http_url.get_secret_value())
    jupiter = Jupiter(client, keypair)

    watcher = RaydiumVaultWatcher(settings.solana_rpc_ws_url.get_secret_value(), pools, min_amount_in)
    simulator = TwoPoolArbSimulator(client, reference_pools)
    sender = JitoBundleSender(settings.jito_block_engine_url, keypair)

    async for swap in watcher.watch():
        if os.path.exists(settings.kill_switch_file):
            logger.warning("файл kill switch %s присутствует — останавливаемся", settings.kill_switch_file)
            return

        sim = await simulator.simulate(swap)
        if not sim.profitable:
            continue

        if not await _has_sufficient_balance(client, keypair.pubkey(), swap.token_in, sim.amount_in):
            continue

        try:
            buy_unsigned = await jupiter.swap(
                input_mint=swap.token_in,
                output_mint=swap.token_out,
                amount=sim.amount_in,
                slippage_bps=SLIPPAGE_BPS,
                only_direct_routes=True,
                exclude_dexes=[sim.sell_router] if sim.sell_router else None,
            )
            sell_unsigned = await jupiter.swap(
                input_mint=swap.token_out,
                output_mint=swap.token_in,
                amount=sim.bought_amount,
                slippage_bps=SLIPPAGE_BPS,
                only_direct_routes=True,
                exclude_dexes=[sim.buy_router] if sim.buy_router else None,
            )
        except Exception as exc:
            logger.error("Jupiter не смог построить транзакции ног (%s) — пропускаем", type(exc).__name__)
            continue

        tip_account = await sender.get_tip_account()
        tip_lamports = _tip_lamports(sim.expected_profit_wei, settings.profit_share_bps)
        blockhash = (await client.get_latest_blockhash()).value.blockhash

        buy_tx = _sign_unsigned_tx(buy_unsigned, keypair)
        sell_tx = _sign_unsigned_tx(sell_unsigned, keypair)
        tip_tx = _build_tip_tx(keypair, tip_account, tip_lamports, blockhash)

        bundle = Bundle(
            raw_txs=[to_base64(bytes(buy_tx)), to_base64(bytes(sell_tx)), to_base64(bytes(tip_tx))],
            target_block=0,  # у Jito-бандлов нет обязательного target slot, как у Flashbots target_block
        )
        included = await sender.send(bundle)
        logger.info("swap=%s profit_lamports=%d included=%s", swap.tx_hash, sim.expected_profit_wei, included)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(pools={}, reference_pools={}, min_amount_in=10**9))
