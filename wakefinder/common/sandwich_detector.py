"""Детекция sandwich-атаки на СОБСТВЕННУЮ транзакцию бота в публичном
мемпуле — chains/eth/copytrade.py и chains/eth/snipe.py (обычный, не
backrun, режим) сознательно идут в публичный мемпул напрямую (не через
Flashbots) ради скорости входа и уже документируют этот риск в своих
docstring'ах ("теоретически может быть засэндвичена другими MEV-ботами"),
но раньше никак его не измеряли — этот модуль закрывает именно измерение.

Эвристика — позиция в блоке: если СРАЗУ ДО и СРАЗУ ПОСЛЕ нашей транзакции
в том же блоке стоят транзакции от ОДНОГО И ТОГО ЖЕ адреса — классический
паттерн front-run + back-run одним и тем же ботом (тот же принцип, что
используют публичные MEV-дашборды вроде Flashbots MEV-Explore).

ЧЕСТНАЯ ГРАНИЦА: это ДЕТЕКЦИЯ ПОСТФАКТУМ, не защита — наша транзакция уже
исполнена и её нельзя отменить/защитить задним числом. Годится для
измерения масштаба проблемы (сколько наших входов реально сэндвичат) и
принятия решения сменить relay/приоритеты, не для блокирующей проверки
перед отправкой."""

import logging

logger = logging.getLogger("wakefinder.sandwich_detector")


class SandwichCheckResult:
    def __init__(self, likely_sandwiched: bool, front_run_from: str | None = None, back_run_from: str | None = None):
        self.likely_sandwiched = likely_sandwiched
        self.front_run_from = front_run_from
        self.back_run_from = back_run_from


def check_block_position(block_transactions: list, our_tx_hash: str) -> SandwichCheckResult:
    """block_transactions — транзакции блока в порядке индекса (как
    w3.eth.get_block(block_number, full_transactions=True)["transactions"]),
    каждый элемент dict-подобен с ключами "hash"/"from". Чистая функция без
    RPC — вызывающий код сам получает блок."""
    our_tx_hash = our_tx_hash.lower()
    our_index = None
    for i, tx in enumerate(block_transactions):
        tx_hash = tx["hash"]
        tx_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        if tx_hash.lower() == our_tx_hash:
            our_index = i
            break
    if our_index is None or our_index == 0 or our_index == len(block_transactions) - 1:
        return SandwichCheckResult(likely_sandwiched=False)

    before = block_transactions[our_index - 1]
    after = block_transactions[our_index + 1]
    if before["from"].lower() == after["from"].lower():
        return SandwichCheckResult(likely_sandwiched=True, front_run_from=before["from"], back_run_from=after["from"])
    return SandwichCheckResult(likely_sandwiched=False)


async def check_own_tx_for_sandwich(w3, tx_hash: str) -> SandwichCheckResult:
    """RPC-обёртка над check_block_position — получает receipt (номер
    блока), затем сам блок с полными транзакциями."""
    try:
        receipt = await w3.eth.get_transaction_receipt(tx_hash)
        block = await w3.eth.get_block(receipt["blockNumber"], full_transactions=True)
        return check_block_position(block["transactions"], tx_hash)
    except Exception as exc:
        logger.warning("не удалось проверить транзакцию %s на sandwich (%s)", tx_hash, type(exc).__name__)
        return SandwichCheckResult(likely_sandwiched=False)


def demo() -> None:
    txs = [
        {"hash": "0xaaa", "from": "0xFRONTRUNNER"},
        {"hash": "0xbbb", "from": "0xFRONTRUNNER"},
        {"hash": "0xOUR", "from": "0xUS"},
        {"hash": "0xccc", "from": "0xFRONTRUNNER"},
    ]
    result = check_block_position(txs, "0xOUR")
    assert result.likely_sandwiched is True
    assert result.front_run_from == "0xFRONTRUNNER"

    txs_safe = [
        {"hash": "0xaaa", "from": "0xSOMEONE"},
        {"hash": "0xOUR", "from": "0xUS"},
        {"hash": "0xccc", "from": "0xSOMEONE_ELSE"},
    ]
    assert check_block_position(txs_safe, "0xOUR").likely_sandwiched is False

    # наша транзакция первая/последняя в блоке — соседей с одной стороны нет
    assert check_block_position([{"hash": "0xOUR", "from": "0xUS"}], "0xOUR").likely_sandwiched is False
    print("OK")


if __name__ == "__main__":
    demo()
