import json

from wakefinder.common.hash_chain import GENESIS_HASH, append_chained, verify_chain


def test_verify_missing_file_returns_valid():
    assert verify_chain("/nonexistent/path.jsonl") == (True, None)


def test_single_record_chains_from_genesis(tmp_path):
    path = str(tmp_path / "log.jsonl")
    append_chained(path, {"a": 1})
    with open(path) as f:
        record = json.loads(f.readline())
    assert record["prev_hash"] == GENESIS_HASH
    assert "hash" in record


def test_chain_of_multiple_records_is_valid(tmp_path):
    path = str(tmp_path / "log.jsonl")
    append_chained(path, {"a": 1})
    append_chained(path, {"a": 2})
    append_chained(path, {"a": 3})
    assert verify_chain(path) == (True, None)


def test_tampering_a_record_breaks_the_chain(tmp_path):
    path = tmp_path / "log.jsonl"
    append_chained(str(path), {"a": 1})
    append_chained(str(path), {"a": 2})
    append_chained(str(path), {"a": 3})

    with open(path) as f:
        lines = f.readlines()
    tampered = json.loads(lines[0])
    tampered["a"] = 999
    lines[0] = json.dumps(tampered) + "\n"
    with open(path, "w") as f:
        f.writelines(lines)

    valid, broken_at = verify_chain(str(path))
    assert valid is False
    assert broken_at == 1


def test_deleting_a_record_breaks_the_chain(tmp_path):
    path = tmp_path / "log.jsonl"
    append_chained(str(path), {"a": 1})
    append_chained(str(path), {"a": 2})
    append_chained(str(path), {"a": 3})

    with open(path) as f:
        lines = f.readlines()
    del lines[1]  # удаляем среднюю запись
    with open(path, "w") as f:
        f.writelines(lines)

    valid, broken_at = verify_chain(str(path))
    assert valid is False


def test_legacy_records_without_hash_fields_are_not_a_break(tmp_path):
    path = tmp_path / "log.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"a": "legacy, до hash-chain"}) + "\n")
    assert verify_chain(str(path)) == (True, None)


def test_new_records_after_legacy_ones_chain_from_genesis(tmp_path):
    path = tmp_path / "log.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"a": "legacy"}) + "\n")
    append_chained(str(path), {"a": "new"})

    with open(path) as f:
        lines = f.readlines()
    new_record = json.loads(lines[1])
    assert new_record["prev_hash"] == GENESIS_HASH  # легаси-строка не участвует в цепочке
    assert verify_chain(str(path)) == (True, None)


def test_malformed_line_breaks_chain(tmp_path):
    path = tmp_path / "log.jsonl"
    append_chained(str(path), {"a": 1})
    with open(path, "a") as f:
        f.write("not valid json\n")
    valid, broken_at = verify_chain(str(path))
    assert valid is False
    assert broken_at == 2


if __name__ == "__main__":
    test_verify_missing_file_returns_valid()
    print("ok (остальные тесты используют tmp_path — запускайте через pytest)")
