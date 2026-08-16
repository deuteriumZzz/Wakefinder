"""Фильтр безопасности для снайпинга свежесозданных пар.

ЧЕСТНО НЕ детектор honeypot'ов: настоящая проверка "можно ли продать этот
токен обратно" требует либо реального round-trip исполнения (купить и сразу
продать в одной симуляции — например через `flashbots.simulate` с двумя
подписанными транзакциями подряд), либо анализа байткода токена на
transfer-блокировки/blacklist. И то и другое — отдельный по объёму кусок
работы (round-trip требует nonce-последовательных подписанных транзакций,
байткод-анализ ненадёжен даже как эвристика — та же причина, по которой в
этом проекте нет автоматического honeypot-детектора уровня allowlist/
denylist, см. их docstring). Здесь — дешёвые проверки, которые отсекают
явно нежизнеспособные пары ДО траты капитала: минимальная ликвидность и то,
что AMM-математика вообще считается в обе стороны.

ponytail: round-trip симуляция через flashbots.simulate — естественное
следующее усиление; сознательно не входит в v1 из-за объёма (сборка двух
подписанных транзакций с последовательными nonce только ради проверки).
"""

from dataclasses import dataclass

from web3 import AsyncWeb3

from wakefinder.chains.eth.abi import PAIR_ABI, ROUTER_ABI

WETH_PATH_UNSUPPORTED = "пара не содержит WETH — снайпинг поддерживает только WETH-котируемые пары"
THIN_LIQUIDITY = "ликвидность WETH-стороны ниже минимума"
RESERVES_FAILED = "getReserves() не удался"
NO_QUOTE = "не удалось получить котировку в обе стороны — AMM-математика не считается для этой пары"


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
