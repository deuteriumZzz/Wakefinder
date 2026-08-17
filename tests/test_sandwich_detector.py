import asyncio

from wakefinder.common.sandwich_detector import check_block_position, check_own_tx_for_sandwich


def test_detects_same_address_front_and_back_run():
    txs = [
        {"hash": "0xaaa", "from": "0xFRONTRUNNER"},
        {"hash": "0xbbb", "from": "0xFRONTRUNNER"},
        {"hash": "0xOUR", "from": "0xUS"},
        {"hash": "0xccc", "from": "0xFRONTRUNNER"},
    ]
    result = check_block_position(txs, "0xOUR")
    assert result.likely_sandwiched is True
    assert result.front_run_from == "0xFRONTRUNNER"
    assert result.back_run_from == "0xFRONTRUNNER"


def test_no_sandwich_when_neighbors_differ():
    txs = [
        {"hash": "0xaaa", "from": "0xSOMEONE"},
        {"hash": "0xOUR", "from": "0xUS"},
        {"hash": "0xccc", "from": "0xSOMEONE_ELSE"},
    ]
    assert check_block_position(txs, "0xOUR").likely_sandwiched is False


def test_no_neighbors_when_first_in_block():
    txs = [{"hash": "0xOUR", "from": "0xUS"}, {"hash": "0xccc", "from": "0xSOMEONE"}]
    assert check_block_position(txs, "0xOUR").likely_sandwiched is False


def test_no_neighbors_when_last_in_block():
    txs = [{"hash": "0xaaa", "from": "0xSOMEONE"}, {"hash": "0xOUR", "from": "0xUS"}]
    assert check_block_position(txs, "0xOUR").likely_sandwiched is False


def test_our_tx_not_found_returns_false():
    txs = [{"hash": "0xaaa", "from": "0xSOMEONE"}]
    assert check_block_position(txs, "0xMISSING").likely_sandwiched is False


def test_hash_matching_is_case_insensitive():
    txs = [
        {"hash": "0xAAA", "from": "0xFRONTRUNNER"},
        {"hash": "0xOUR", "from": "0xUS"},
        {"hash": "0xCCC", "from": "0xFRONTRUNNER"},
    ]
    assert check_block_position(txs, "0XOUR").likely_sandwiched is True


class _FakeHash:
    def __init__(self, hex_value):
        self._hex = hex_value

    def hex(self):
        return self._hex


def test_handles_hexbytes_style_hash_objects():
    txs = [
        {"hash": _FakeHash("aaa"), "from": "0xFRONTRUNNER"},
        {"hash": _FakeHash("our"), "from": "0xUS"},
        {"hash": _FakeHash("ccc"), "from": "0xFRONTRUNNER"},
    ]
    assert check_block_position(txs, "0xour").likely_sandwiched is True


def test_own_tx_for_sandwich_swallows_rpc_errors():
    class ExplodingW3:
        class eth:
            @staticmethod
            async def get_transaction_receipt(tx_hash):
                raise RuntimeError("rpc down")

    result = asyncio.run(check_own_tx_for_sandwich(ExplodingW3(), "0xOUR"))
    assert result.likely_sandwiched is False
