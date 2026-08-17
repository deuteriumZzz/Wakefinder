"""Сверка positions.json с РЕАЛЬНЫМ on-chain балансом при старте процесса —
ловит расхождение между "что бот думает, что держит" и "что реально на
кошельке": краш между подтверждённой покупкой и записью файла (тот класс
гонки, который отдельно закрыт правильным порядком save/_approve в
chains/eth/snipe.py), ручная продажа вне бота, второй процесс на том же
кошельке вопреки wallet_lock.py и т.п.

ЧЕСТНАЯ ГРАНИЦА: не пытается автоматически восстановить позицию, которая
вообще не попала в файл (обратный случай) — trade_log.py хранит pool_address,
не адрес токена, так что реконструкция amount_in/token для потерянной записи
означала бы гадание, а не факт. Эта проверка ловит только противоположный,
безопасный для автоматизации случай: позиция ЕСТЬ в файле, балансов под неё
не хватает — и просто алертит оператора, не трогает файл сама."""

from dataclasses import dataclass


@dataclass
class PositionMismatch:
    token: str
    recorded_amount: int
    actual_balance: int


def find_mismatches(recorded: dict[str, int], balances: dict[str, int], tolerance_pct: float = 1.0) -> list[PositionMismatch]:
    """recorded: {token: amount_held по данным positions.json}.
    balances: {token: реальный on-chain баланс} — вызывающий код сам делает
    RPC-запросы под конкретную сеть/SDK, эта функция чистая и без сетевого
    I/O для тестируемости. tolerance_pct — допуск на округление/decimals
    (по умолчанию 1%: реальный баланс ниже recorded*(1-1%) считается
    расхождением, не любое дробление co слиппеджем на споте)."""
    out = []
    for token, amount_held in recorded.items():
        if amount_held <= 0:
            continue
        actual = balances.get(token, 0)
        floor = amount_held * (100 - tolerance_pct) / 100
        if actual < floor:
            out.append(PositionMismatch(token=token, recorded_amount=amount_held, actual_balance=actual))
    return out


def demo() -> None:
    mismatches = find_mismatches({"TOKEN_A": 1000, "TOKEN_B": 500}, {"TOKEN_A": 1000, "TOKEN_B": 0})
    assert len(mismatches) == 1
    assert mismatches[0].token == "TOKEN_B"
    assert find_mismatches({"TOKEN_A": 1000}, {"TOKEN_A": 995}) == []  # в пределах допуска
    print("OK")


if __name__ == "__main__":
    demo()
