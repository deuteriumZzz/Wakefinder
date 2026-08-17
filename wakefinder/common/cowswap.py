"""Опциональный exit-путь через CoW Protocol (batch-аукцион, intent-based)
для стоп-лосс/trailing-stop выходов в chains/eth/{copytrade,snipe}.py —
Tier D сохранённого MEV-роадмапа, СУЖЕННЫЙ до выходов после feasibility-
анализа: batch/Dutch-аукцион ждёт solver'ов ради лучшей цены, что прямо
противоречит скорости ВХОДА (там остаётся прямой AMM-своп + опционально
MEV-protect RPC, см. common/protected_rpc.py). На ВЫХОДЕ, где решает НАША
СОБСТВЕННАЯ проверка цены по таймеру, а не гонка с чужой pending-
транзакцией, торможение ради лучшей цены и MEV-защиты — уместный
компромисс, не потеря конкурентного преимущества.

Только CoWSwap, не 1inch Fusion — у CoWSwap публичный REST API БЕЗ
API-ключа и явная поддержка sell-ордеров (продать РОВНО X, получить НЕ
МЕНЬШЕ Y), что прямо соответствует задаче выхода из позиции; у 1inch Fusion
нет официального Python SDK, интеграция потребовала бы больше кода ради
того же результата.

ЧЕСТНО: EIP-712 домен (GPv2Settlement) и VaultRelayer-адрес — из
документированной публичной спецификации CoW Protocol
(docs.cow.fi/cow-protocol/reference/contracts/core), НЕ проверены вживую —
нет сетевого доступа к Ethereum/CoW API в этой песочнице. ПРОВЕРЬТЕ САМИ
перед использованием.

При ЛЮБОМ сбое (нет котировки/API недоступен/не исполнилось за таймаут/
cancelled/expired) — вызывающий код (copytrade.py/snipe.py) откатывается на
существующий прямой AMM-своп: невозможность выйти из позиции хуже, чем
выйти по неоптимальной, но гарантированной цене."""

import asyncio
import logging
import time

import requests
from eth_account.messages import encode_typed_data
from web3 import Web3

logger = logging.getLogger("wakefinder.cowswap")

COWSWAP_API_URL = "https://api.cow.fi/mainnet/api/v1"
GPV2_SETTLEMENT_ADDRESS = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
VAULT_RELAYER_ADDRESS = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"

_ORDER_TYPES = {
    "Order": [
        {"name": "sellToken", "type": "address"},
        {"name": "buyToken", "type": "address"},
        {"name": "receiver", "type": "address"},
        {"name": "sellAmount", "type": "uint256"},
        {"name": "buyAmount", "type": "uint256"},
        {"name": "validTo", "type": "uint32"},
        {"name": "appData", "type": "bytes32"},
        {"name": "feeAmount", "type": "uint256"},
        {"name": "kind", "type": "string"},
        {"name": "partiallyFillable", "type": "bool"},
        {"name": "sellTokenBalance", "type": "string"},
        {"name": "buyTokenBalance", "type": "string"},
    ]
}


def _to_0x_hex(raw: bytes) -> str:
    hex_str = bytes(raw).hex()
    return hex_str if hex_str.startswith("0x") else "0x" + hex_str


def _domain(chain_id: int) -> dict:
    return {"name": "Gnosis Protocol", "version": "v2", "chainId": chain_id, "verifyingContract": GPV2_SETTLEMENT_ADDRESS}


def build_order(sell_token: str, buy_token: str, receiver: str, sell_amount: int, buy_amount: int, valid_to: int, fee_amount: int = 0) -> dict:
    return {
        "sellToken": Web3.to_checksum_address(sell_token),
        "buyToken": Web3.to_checksum_address(buy_token),
        "receiver": Web3.to_checksum_address(receiver),
        "sellAmount": str(sell_amount),
        "buyAmount": str(buy_amount),
        "validTo": valid_to,
        "appData": "0x" + "00" * 32,
        "feeAmount": str(fee_amount),
        "kind": "sell",
        "partiallyFillable": False,
        "sellTokenBalance": "erc20",
        "buyTokenBalance": "erc20",
    }


def sign_order(order: dict, chain_id: int, account) -> str:
    """account — eth_account LocalAccount, тот же тип, что account везде в
    проекте. EIP-712 подпись самого ордера (GPv2Order.Data) — не транзакция,
    газ за это не платится, платит solver при сеттлменте."""
    message = {
        "sellToken": order["sellToken"], "buyToken": order["buyToken"], "receiver": order["receiver"],
        "sellAmount": int(order["sellAmount"]), "buyAmount": int(order["buyAmount"]), "validTo": order["validTo"],
        "appData": bytes.fromhex(order["appData"][2:]), "feeAmount": int(order["feeAmount"]), "kind": order["kind"],
        "partiallyFillable": order["partiallyFillable"],
        "sellTokenBalance": order["sellTokenBalance"], "buyTokenBalance": order["buyTokenBalance"],
    }
    signable = encode_typed_data(domain_data=_domain(chain_id), message_types=_ORDER_TYPES, message_data=message)
    signed = account.sign_message(signable)
    return _to_0x_hex(signed.signature)


def _get_quote_sync(sell_token: str, buy_token: str, sell_amount: int, from_address: str) -> dict | None:
    try:
        resp = requests.post(
            f"{COWSWAP_API_URL}/quote",
            json={"sellToken": sell_token, "buyToken": buy_token, "from": from_address, "kind": "sell", "sellAmountBeforeFee": str(sell_amount)},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("CoWSwap котировка не удалась (%s)", type(exc).__name__)
        return None


def _submit_order_sync(order: dict, signature: str, from_address: str) -> str | None:
    try:
        resp = requests.post(
            f"{COWSWAP_API_URL}/orders",
            json={**order, "signature": signature, "signingScheme": "eip712", "from": from_address},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("CoWSwap отправка ордера не удалась (%s)", type(exc).__name__)
        return None


def _order_status_sync(order_uid: str) -> dict | None:
    try:
        resp = requests.get(f"{COWSWAP_API_URL}/orders/{order_uid}", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("CoWSwap статус ордера не удалось получить (%s)", type(exc).__name__)
        return None


async def get_quote(sell_token: str, buy_token: str, sell_amount: int, from_address: str) -> dict | None:
    return await asyncio.to_thread(_get_quote_sync, sell_token, buy_token, sell_amount, from_address)


async def submit_order(order: dict, signature: str, from_address: str) -> str | None:
    return await asyncio.to_thread(_submit_order_sync, order, signature, from_address)


async def wait_for_fill(order_uid: str, timeout_seconds: float, poll_interval_seconds: float = 5.0) -> tuple[bool, int]:
    """Возвращает (filled, buy_amount). НЕ дождались/expired/cancelled/
    ошибка API -> (False, 0) — вызывающий код решает, откатываться ли на
    прямой AMM-своп (см. docstring модуля)."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = await asyncio.to_thread(_order_status_sync, order_uid)
        if status is not None:
            order_status = status.get("status")
            if order_status == "fulfilled":
                return True, int(status.get("executedBuyAmount", 0))
            if order_status in ("cancelled", "expired"):
                return False, 0
        await asyncio.sleep(poll_interval_seconds)
    return False, 0


async def place_and_wait_for_exit_order(
    account, chain_id: int, sell_token: str, buy_token: str, sell_amount: int,
    min_buy_amount: int, valid_seconds: float, poll_timeout_seconds: float,
) -> tuple[bool, int]:
    """Высокоуровневая оркестрация: quote -> build -> sign -> submit -> poll.
    Возвращает (filled, buy_amount) — (False, 0) при ЛЮБОМ сбое, чтобы
    вызывающий код мог откатиться на прямой AMM-своп без разбора причины."""
    quote = await get_quote(sell_token, buy_token, sell_amount, account.address)
    if quote is None:
        return False, 0
    quote_data = quote.get("quote", {})
    try:
        buy_amount = int(quote_data["buyAmount"])
        fee_amount = int(quote_data["feeAmount"])
    except (KeyError, ValueError, TypeError):
        logger.warning("CoWSwap котировка пришла в неожиданном формате: %s", quote_data)
        return False, 0
    if buy_amount < min_buy_amount:
        logger.info("CoWSwap котировка хуже минимума (%d < %d) — откат на прямой AMM", buy_amount, min_buy_amount)
        return False, 0

    valid_to = int(time.time() + valid_seconds)
    order = build_order(sell_token, buy_token, account.address, sell_amount, buy_amount, valid_to, fee_amount)
    signature = sign_order(order, chain_id, account)
    order_uid = await submit_order(order, signature, account.address)
    if order_uid is None:
        return False, 0

    return await wait_for_fill(order_uid, poll_timeout_seconds)


async def ensure_vault_relayer_approved(w3, account, chain_id: int, token: str, amount: int) -> bool:
    from wakefinder.chains.eth.abi import ERC20_ABI

    erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    allowance = await erc20.functions.allowance(account.address, VAULT_RELAYER_ADDRESS).call()
    if allowance >= amount:
        return True

    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    tx = erc20.functions.approve(VAULT_RELAYER_ADDRESS, 2**256 - 1).build_transaction(
        {
            "from": account.address, "nonce": nonce, "gas": 60_000,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        }
    )
    raw = account.sign_transaction(tx).rawTransaction
    tx_hash = await w3.eth.send_raw_transaction(raw)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return receipt.get("status") == 1
