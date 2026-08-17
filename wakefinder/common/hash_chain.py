"""Hash-chain для append-only JSONL-логов (trade_log.jsonl, pnl_ledger.jsonl)
— каждая запись несёт sha256(prev_hash + канонический JSON остальных полей),
поэтому изменение/удаление/вставка задним числом любой строки рвёт цепочку
для ВСЕХ записей после неё (verify_chain это обнаруживает). Честная
граница: файл на диске по-прежнему можно отредактировать текстовым
редактором, как и раньше — это про "видно, что подделали", не про
"нельзя подделать". Для настоящей защиты от переписывания истории нужна
запись в append-only хранилище вне контроля процесса (WORM-бакет,
внешний лог-агрегатор) — вне периметра этого проекта.

Записи, созданные ДО внедрения hash-chain (без полей hash/prev_hash),
считаются легаси и пропускаются при верификации, не разрывом цепочки —
они никогда не были защищены, это не regression."""

import hashlib
import json
import os

GENESIS_HASH = "0" * 64


def _record_hash(record_without_hash: dict, prev_hash: str) -> str:
    canonical = json.dumps(record_without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def _last_hash(path: str) -> str:
    """Полное чтение файла на каждый append — тот же принцип, что уже
    используют wallet_stats.py/metrics.py (полное сканирование trade_log на
    каждый вызов), не новый паттерн: логи этого проекта не рассчитаны на
    десятки миллионов строк, а запись сделки и так стоит сотни мс на RPC."""
    if not os.path.exists(path):
        return GENESIS_HASH
    last_hash = GENESIS_HASH
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "hash" in record:
                last_hash = record["hash"]
    return last_hash


def append_chained(path: str, record: dict) -> None:
    prev_hash = _last_hash(path)
    chained = {**record, "prev_hash": prev_hash}
    chained["hash"] = _record_hash(chained, prev_hash)
    with open(path, "a") as f:
        f.write(json.dumps(chained) + "\n")


def verify_chain(path: str) -> tuple[bool, int | None]:
    """(валидна ли цепочка целиком, номер строки где порвалась или None)."""
    if not os.path.exists(path):
        return True, None
    last_seen_hash = GENESIS_HASH
    with open(path) as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return False, i
            if "hash" not in record or "prev_hash" not in record:
                continue  # легаси-запись до внедрения hash-chain
            stored_hash = record["hash"]
            stored_prev = record["prev_hash"]
            # Своя внутренняя честность записи (её hash реально считается из
            # её же полей и её prev_hash)...
            check_record = {k: v for k, v in record.items() if k != "hash"}
            if _record_hash(check_record, stored_prev) != stored_hash:
                return False, i
            # ...И связность с ФАКТИЧЕСКИ предыдущей записью в файле — без
            # этой проверки удаление записи из середины не обнаруживалось бы:
            # оставшиеся записи всё ещё внутренне честны сами по себе, только
            # ссылаются на hash уже отсутствующей строки.
            if stored_prev != last_seen_hash:
                return False, i
            last_seen_hash = stored_hash
    return True, None


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "log.jsonl")
        append_chained(path, {"a": 1})
        append_chained(path, {"a": 2})
        append_chained(path, {"a": 3})
        valid, broken_at = verify_chain(path)
        assert valid and broken_at is None

        # подделываем среднюю запись
        with open(path) as f:
            lines = f.readlines()
        tampered = json.loads(lines[1])
        tampered["a"] = 999
        lines[1] = json.dumps(tampered) + "\n"
        with open(path, "w") as f:
            f.writelines(lines)
        valid, broken_at = verify_chain(path)
        assert not valid and broken_at == 2

        # легаси-файл без hash-полей -> валиден целиком (не regression)
        legacy_path = os.path.join(d, "legacy.jsonl")
        with open(legacy_path, "w") as f:
            f.write(json.dumps({"a": 1}) + "\n")
        valid, broken_at = verify_chain(legacy_path)
        assert valid and broken_at is None

        print("ok")
