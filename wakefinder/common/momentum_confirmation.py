"""On-chain momentum-подтверждение входа для снайпинга — ОПЦИОНАЛЬНО
(по умолчанию выключено): вместо входа СРАЗУ после detection+safety-фильтра,
ждём N подтверждающих свопов (реальных покупок, не просто факт создания
пула/минта) перед входом.

ЧЕСТНЫЙ КОМПРОМИСС СО СКОРОСТЬЮ: в отличие от momentum-выхода
(common/trailing_stop.py, не трогает скорость входа вообще), эта проверка
ДОБАВЛЯЕТ RPC-раунд-трип перед покупкой — окно, в котором другие снайпер-
боты могут войти первыми. Ловит меньше rug (реальные покупки — более сильный
сигнал, чем просто факт существования пула), но проигрывает в скорости
конкурентам без этой проверки. См. README "Momentum-сигналы"."""

from dataclasses import dataclass


@dataclass
class MomentumConfirmationResult:
    passed: bool
    buy_count: int
    reason: str = ""


def evaluate_buy_count(buy_count: int, min_buys: int) -> MomentumConfirmationResult:
    """Чистая функция — решение по уже посчитанному количеству
    подтверждающих покупок. Подсчёт под конкретную сеть — отдельно, см.
    check_eth_pool_momentum/check_solana_mint_momentum ниже (оба делают
    RPC-запросы, здесь тестируется решение без сети)."""
    if buy_count >= min_buys:
        return MomentumConfirmationResult(passed=True, buy_count=buy_count)
    return MomentumConfirmationResult(
        passed=False, buy_count=buy_count,
        reason=f"недостаточно подтверждающих покупок: {buy_count} < {min_buys}",
    )


async def check_eth_pool_momentum(w3, pool_address: str, from_block: int, min_buys: int) -> MomentumConfirmationResult:
    """Считает Swap-события пула (PAIR_ABI) с блока создания пула — каждый
    Swap = реальная покупка/продажа, не просто наличие пула. Не различает
    направление (buy vs sell) — на свежесозданном пуле в первые секунды
    практически все свопы это покупки (продавать пока некому), различение
    потребовало бы декодировать amount0In/amount1In относительно token0/
    token1 ради небольшой точности прироста."""
    from wakefinder.chains.eth.abi import PAIR_ABI

    pair = w3.eth.contract(address=pool_address, abi=PAIR_ABI)
    latest_block = await w3.eth.block_number
    logs = await pair.events.Swap.get_logs(fromBlock=from_block, toBlock=latest_block)
    return evaluate_buy_count(len(logs), min_buys)


async def check_solana_mint_momentum(client, mint: str, min_buys: int, limit: int = 50) -> MomentumConfirmationResult:
    """DEX-агностичный подсчёт (тот же принцип, что и wallet_watcher.py —
    не декодируем конкретный AMM): количество подписей транзакций,
    затрагивающих сам mint-адрес, с момента его создания — грубый прокси
    активности (свопы, но и не только они), не точный buy-count, как на
    ETH. Честный компромисс: Solana не даёт EVM-style event-логов пула."""
    from solders.pubkey import Pubkey

    sigs = await client.get_signatures_for_address(Pubkey.from_string(mint), limit=limit)
    return evaluate_buy_count(len(sigs.value), min_buys)


def demo() -> None:
    assert evaluate_buy_count(3, 2).passed is True
    assert evaluate_buy_count(1, 2).passed is False
    assert evaluate_buy_count(2, 2).passed is True  # ровно порог — проходит
    print("OK")


if __name__ == "__main__":
    demo()
