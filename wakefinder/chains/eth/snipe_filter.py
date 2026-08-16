"""Фильтр безопасности для снайпинга свежесозданных пар.

Два независимых уровня проверки:

1. `check_new_pool` — дешёвые проверки без единой подписанной транзакции:
   минимальная ликвидность и то, что AMM-математика вообще считается в обе
   стороны. НЕ ловит honeypot, где сама математика в порядке, а `transfer`/
   `transferFrom` токена блокирует продажу для не-владельца (blacklist,
   pausable-transfer и т.п.) — именно так работает большинство реальных
   honeypot-контрактов.

2. `check_round_trip_sellable` — РЕАЛЬНАЯ round-trip симуляция через
   `flashbots.simulate`: подписанные [buy, approve, sell] транзакции с
   последовательными nonce, выполненные ПОСЛЕДОВАТЕЛЬНО против одного
   снапшота состояния (то же свойство, на котором держится backrun-арбитраж
   в main.py) — если токен блокирует продажу, sell-нога вернёт ошибку в
   результате симуляции. Ничего не отправляется по-настоящему —
   `FlashbotsBundleSender.simulate()` только считает, не исполняет в сети.
   Всё ещё НЕ гарантия: time-locked honeypot (продажа блокируется только
   через N блоков/часов после запуска) это не поймает — round-trip
   проверяет "можно продать ПРЯМО СЕЙЧАС", не "можно продать всегда".

Байткод-анализ токена (третий, более глубокий уровень) сознательно не
реализован — ненадёжен даже как эвристика, та же причина, по которой в
этом проекте нет автоматического honeypot-детектора уровня allowlist/
denylist, см. их docstring.
"""

import time
from dataclasses import dataclass

from web3 import AsyncWeb3, Web3

from wakefinder.chains.eth.abi import ERC20_ABI, PAIR_ABI, ROUTER_ABI

WETH_PATH_UNSUPPORTED = "пара не содержит WETH — снайпинг поддерживает только WETH-котируемые пары"
THIN_LIQUIDITY = "ликвидность WETH-стороны ниже минимума"
RESERVES_FAILED = "getReserves() не удался"
NO_QUOTE = "не удалось получить котировку в обе стороны — AMM-математика не считается для этой пары"
ROUND_TRIP_SIM_FAILED = "round-trip симуляция покупки+продажи не прошла — похоже на honeypot (продажа заблокирована)"

_ROUND_TRIP_GAS_LIMIT = 250_000


@dataclass
class SnipeCheckResult:
    passed: bool
    reason: str = ""
    token: str = ""
    quoted_buy_amount: int = 0


async def check_new_pool(
    w3: AsyncWeb3,
    router_address: str,
    pool_address: str,
    token0: str,
    token1: str,
    weth_address: str,
    test_amount_wei: int,
    min_liquidity_weth: int,
) -> SnipeCheckResult:
    if token0.lower() == weth_address.lower():
        token = token1
    elif token1.lower() == weth_address.lower():
        token = token0
    else:
        return SnipeCheckResult(passed=False, reason=WETH_PATH_UNSUPPORTED)

    pool = w3.eth.contract(address=pool_address, abi=PAIR_ABI)
    try:
        r0, r1, _ = await pool.functions.getReserves().call()
    except Exception:
        return SnipeCheckResult(passed=False, reason=RESERVES_FAILED, token=token)

    weth_reserve = r0 if token0.lower() == weth_address.lower() else r1
    if weth_reserve < min_liquidity_weth:
        return SnipeCheckResult(passed=False, reason=THIN_LIQUIDITY, token=token)

    router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
    try:
        buy_amounts = await router.functions.getAmountsOut(test_amount_wei, [weth_address, token]).call()
        bought = buy_amounts[-1]
        await router.functions.getAmountsOut(bought, [token, weth_address]).call()
    except Exception:
        return SnipeCheckResult(passed=False, reason=NO_QUOTE, token=token)

    return SnipeCheckResult(passed=True, token=token, quoted_buy_amount=bought)


async def check_round_trip_sellable(
    w3: AsyncWeb3,
    sender,
    account,
    router_address: str,
    weth_address: str,
    token: str,
    chain_id: int,
    test_amount_wei: int,
) -> SnipeCheckResult:
    """[buy, approve, sell] одним flashbots-бандлом, ТОЛЬКО симуляция (см.
    docstring модуля) — approve нужен между buy и sell, потому что после
    покупки внутри симуляции токен реально у нас на балансе, и роутеру
    нужен allowance для transferFrom на sell-ноге, как и в реальной сделке."""
    router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
    try:
        buy_amounts = await router.functions.getAmountsOut(test_amount_wei, [weth_address, token]).call()
    except Exception:
        return SnipeCheckResult(passed=False, reason=NO_QUOTE, token=token)
    expected_out = buy_amounts[-1]
    sell_amount = expected_out * 95 // 100  # запас на расхождение симуляции/налог на покупку, не строгая котировка

    nonce = await w3.eth.get_transaction_count(account.address, "pending")
    latest = await w3.eth.get_block("latest")
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = latest["baseFeePerGas"] * 2 + priority_fee
    deadline = int(time.time()) + 60

    encoder = Web3()
    router_enc = encoder.eth.contract(address=router_address, abi=ROUTER_ABI)
    erc20_enc = encoder.eth.contract(address=token, abi=ERC20_ABI)

    def _sign(fn, value: int = 0) -> str:
        nonlocal nonce
        tx = fn.build_transaction({
            "from": account.address, "value": value, "nonce": nonce, "gas": _ROUND_TRIP_GAS_LIMIT,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority_fee, "chainId": chain_id,
        })
        nonce += 1
        return account.sign_transaction(tx).rawTransaction

    buy_raw = _sign(
        router_enc.functions.swapExactETHForTokens(amountOutMin=0, path=[weth_address, token], to=account.address, deadline=deadline),
        value=test_amount_wei,
    )
    approve_raw = _sign(erc20_enc.functions.approve(router_address, 2**256 - 1))
    sell_raw = _sign(
        router_enc.functions.swapExactTokensForTokens(amountIn=sell_amount, amountOutMin=0, path=[token, weth_address], to=account.address, deadline=deadline)
    )

    try:
        simulation = await sender.simulate([buy_raw, approve_raw, sell_raw], latest["number"] + 1)
    except Exception:
        return SnipeCheckResult(passed=False, reason=ROUND_TRIP_SIM_FAILED, token=token)

    if simulation.get("error"):
        return SnipeCheckResult(passed=False, reason=ROUND_TRIP_SIM_FAILED, token=token)
    for leg, result in zip(["buy", "approve", "sell"], simulation.get("results", [])):
        if result.get("error"):
            return SnipeCheckResult(passed=False, reason=f"{ROUND_TRIP_SIM_FAILED} ({leg}: {result.get('error')})", token=token)

    return SnipeCheckResult(passed=True, token=token, quoted_buy_amount=expected_out)
