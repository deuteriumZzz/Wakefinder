"""Фильтр торгуемости для только что созданных SPL-минтов (см. docstring
mint_watcher.py про то, почему детекция идёт через минт, а не через
конкретный AMM). Единственная проверка "можно ли реально купить и продать" —
через Jupiter: если `quote()` в обе стороны возвращает маршрут, у токена
есть реальная, агрегированная по всем DEX ликвидность ПРЯМО СЕЙЧАС.

ЧЕСТНО те же ограничения, что у ETH `check_new_pool` (см. её docstring):
не round-trip исполнение (не ловит transfer-блокирующий honeypot — на
Solana это менее типичный паттерн, чем на ETH, но не невозможный), не
гарантия на будущее, только "маршрут существует сейчас"."""

from dataclasses import dataclass

NO_ROUTE = "Jupiter не нашёл маршрут (в одну или обе стороны) — у токена ещё нет ликвидного пула или он не проиндексирован"
THIN_LIQUIDITY = "котируемый выход ниже минимума — маршрут есть, но ликвидность слишком тонкая"


@dataclass
class SnipeCheckResult:
    passed: bool
    reason: str = ""
    mint: str = ""
    quoted_buy_amount: int = 0


async def check_mint_tradeable(
    jupiter,
    mint_address: str,
    wsol_address: str,
    test_amount_lamports: int,
    min_output_lamports: int,
) -> SnipeCheckResult:
    try:
        buy_quote = await jupiter.quote(
            input_mint=wsol_address, output_mint=mint_address, amount=test_amount_lamports,
            slippage_bps=300, only_direct_routes=True,  # тот же режим, что реальный вход/выход в chains/solana/copytrade.py:_swap_via_jupiter_and_send
        )
        bought = int(buy_quote["outAmount"])
    except Exception:
        return SnipeCheckResult(passed=False, reason=NO_ROUTE, mint=mint_address)

    if bought <= 0:
        return SnipeCheckResult(passed=False, reason=NO_ROUTE, mint=mint_address)

    try:
        sell_quote = await jupiter.quote(
            input_mint=mint_address, output_mint=wsol_address, amount=bought,
            slippage_bps=300, only_direct_routes=True,  # тот же режим, что реальный вход/выход в chains/solana/copytrade.py:_swap_via_jupiter_and_send
        )
        sell_back = int(sell_quote["outAmount"])
    except Exception:
        return SnipeCheckResult(passed=False, reason=NO_ROUTE, mint=mint_address)

    if sell_back < min_output_lamports:
        return SnipeCheckResult(passed=False, reason=THIN_LIQUIDITY, mint=mint_address)

    return SnipeCheckResult(passed=True, mint=mint_address, quoted_buy_amount=bought)
