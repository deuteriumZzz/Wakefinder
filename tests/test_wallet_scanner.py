import asyncio

from solders.pubkey import Pubkey

from wakefinder.wallet_scanner import (
    filter_by_etherscan_activity,
    find_candidate_wallets_eth,
    find_candidate_wallets_solana,
)


# --- ETH: find_candidate_wallets_eth ---

class _SwapEvents:
    def __init__(self, logs):
        self._logs = logs

    async def get_logs(self, fromBlock, toBlock):  # имена как в реальном web3.py AsyncContractEvent.get_logs
        return [log for log in self._logs if fromBlock <= log["blockNumber"] <= toBlock]


class _Events:
    def __init__(self, logs):
        self.Swap = _SwapEvents(logs)


class FakePoolContract:
    def __init__(self, logs):
        self.events = _Events(logs)


class FakeEth:
    def __init__(self, contracts):
        self._contracts = contracts

    def contract(self, address, abi):
        return self._contracts[address.lower()]


class FakeW3:
    def __init__(self, eth):
        self.eth = eth


def _swap_log(block_number, to):
    return {"blockNumber": block_number, "args": {"to": to}}


def test_find_candidate_wallets_eth_counts_by_recipient():
    pool = "0xPOOL"
    contract = FakePoolContract(logs=[
        _swap_log(101, "0xAAA"),
        _swap_log(102, "0xBBB"),
        _swap_log(103, "0xAAA"),
    ])
    w3 = FakeW3(FakeEth({pool.lower(): contract}))

    counts = asyncio.run(find_candidate_wallets_eth(w3, [pool], from_block=100, to_block=110))

    assert counts["0xaaa"] == 2
    assert counts["0xbbb"] == 1


def test_find_candidate_wallets_eth_respects_block_range():
    pool = "0xPOOL"
    contract = FakePoolContract(logs=[_swap_log(500, "0xAAA")])  # вне диапазона
    w3 = FakeW3(FakeEth({pool.lower(): contract}))

    counts = asyncio.run(find_candidate_wallets_eth(w3, [pool], from_block=100, to_block=110))

    assert counts == {}


# --- ETH: filter_by_etherscan_activity ---

class _FakeResponse:
    def __init__(self, result):
        self._result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self._result}


def test_filter_by_etherscan_activity_no_key_returns_all():
    result = filter_by_etherscan_activity(["0xA", "0xB"], api_key="", min_tx_count=10)
    assert result == ["0xA", "0xB"]


def test_filter_by_etherscan_activity_filters_low_activity(monkeypatch):
    import wakefinder.wallet_scanner as scanner

    def fake_get(url, params, timeout):
        # "0xA" активный (10 tx), "0xB" — свежий (2 tx)
        tx_count = 10 if params["address"] == "0xA" else 2
        return _FakeResponse(result=[{}] * tx_count)

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    result = filter_by_etherscan_activity(["0xA", "0xB"], api_key="fake-key", min_tx_count=10)
    assert result == ["0xA"]


def test_filter_by_etherscan_activity_request_failure_excludes_address(monkeypatch):
    import wakefinder.wallet_scanner as scanner

    def fake_get(url, params, timeout):
        raise ConnectionError("rate limited")

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    result = filter_by_etherscan_activity(["0xA"], api_key="fake-key", min_tx_count=10)
    assert result == []


# --- Solana: find_candidate_wallets_solana ---

class _SigInfo:
    def __init__(self, signature, err=None):
        self.signature = signature
        self.err = err


class _SigsResp:
    def __init__(self, value):
        self.value = value


class _Balance:
    def __init__(self, owner):
        self.owner = owner


class _Meta:
    def __init__(self, owners_pre, owners_post):
        self.pre_token_balances = [_Balance(o) for o in owners_pre]
        self.post_token_balances = [_Balance(o) for o in owners_post]


class _Transaction:
    def __init__(self, meta):
        self.meta = meta


class _TxValue:
    def __init__(self, meta):
        self.transaction = _Transaction(meta)


class _TxResp:
    def __init__(self, value):
        self.value = value


class FakeSolanaClient:
    def __init__(self, signatures, tx_by_sig):
        self._signatures = signatures
        self._tx_by_sig = tx_by_sig

    async def get_signatures_for_address(self, pubkey, limit=None, commitment=None):
        return _SigsResp(self._signatures)

    async def get_transaction(self, signature, encoding=None, commitment=None, max_supported_transaction_version=None):
        return self._tx_by_sig[signature]


def test_find_candidate_wallets_solana_counts_owners():
    vault = str(Pubkey.new_unique())
    client = FakeSolanaClient(
        signatures=[_SigInfo("sig1"), _SigInfo("sig2")],
        tx_by_sig={
            "sig1": _TxResp(_TxValue(_Meta(owners_pre=["walletA"], owners_post=["walletA", "walletB"]))),
            "sig2": _TxResp(_TxValue(_Meta(owners_pre=["walletA"], owners_post=["walletA"]))),
        },
    )
    counts = asyncio.run(find_candidate_wallets_solana(client, [vault], limit=10))
    assert counts["walletA"] == 2
    assert counts["walletB"] == 1


def test_find_candidate_wallets_solana_skips_failed_transactions():
    vault = str(Pubkey.new_unique())
    client = FakeSolanaClient(
        signatures=[_SigInfo("sig1", err={"InstructionError": []})],
        tx_by_sig={},
    )
    counts = asyncio.run(find_candidate_wallets_solana(client, [vault], limit=10))
    assert counts == {}


if __name__ == "__main__":
    test_find_candidate_wallets_eth_counts_by_recipient()
    test_find_candidate_wallets_eth_respects_block_range()
    test_filter_by_etherscan_activity_no_key_returns_all()
    test_find_candidate_wallets_solana_counts_owners()
    test_find_candidate_wallets_solana_skips_failed_transactions()
    print("run monkeypatch-based tests via pytest")
