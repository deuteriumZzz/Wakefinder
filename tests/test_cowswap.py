"""Тесты common/cowswap.py. sign_order проверяется РЕАЛЬНЫМ eth_account
(Account.recover_message) — тот же принцип, что test_tx_signing.py/
test_liquidation_watcher.py: подпись/ABI-кодирование проверяется настоящим
криптографическим примитивом, не заглушкой."""

import asyncio

from eth_account import Account

from wakefinder.common.cowswap import (
    VAULT_RELAYER_ADDRESS,
    build_order,
    ensure_vault_relayer_approved,
    get_quote,
    place_and_wait_for_exit_order,
    sign_order,
    submit_order,
    wait_for_fill,
)

KEY = "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004"
SELL_TOKEN = "0x1111111111111111111111111111111111111111"
BUY_TOKEN = "0x2222222222222222222222222222222222222222"


def test_build_order_shape():
    order = build_order(SELL_TOKEN, BUY_TOKEN, "0x3333333333333333333333333333333333333333", 1000, 900, 2000000000)
    assert order["kind"] == "sell"
    assert order["partiallyFillable"] is False
    assert order["sellAmount"] == "1000"
    assert order["buyAmount"] == "900"
    assert order["appData"] == "0x" + "00" * 32


def test_sign_order_recovers_to_signer_address():
    account = Account.from_key(KEY)
    order = build_order(SELL_TOKEN, BUY_TOKEN, account.address, 1000, 900, 2000000000)
    signature = sign_order(order, chain_id=1, account=account)

    from eth_account.messages import encode_typed_data
    from wakefinder.common.cowswap import _ORDER_TYPES, _domain

    message = {
        "sellToken": order["sellToken"], "buyToken": order["buyToken"], "receiver": order["receiver"],
        "sellAmount": int(order["sellAmount"]), "buyAmount": int(order["buyAmount"]), "validTo": order["validTo"],
        "appData": bytes.fromhex(order["appData"][2:]), "feeAmount": int(order["feeAmount"]), "kind": order["kind"],
        "partiallyFillable": order["partiallyFillable"],
        "sellTokenBalance": order["sellTokenBalance"], "buyTokenBalance": order["buyTokenBalance"],
    }
    signable = encode_typed_data(domain_data=_domain(1), message_types=_ORDER_TYPES, message_data=message)
    recovered = Account.recover_message(signable, signature=signature)
    assert recovered == account.address


def test_sign_order_different_chain_id_gives_different_signature():
    account = Account.from_key(KEY)
    order = build_order(SELL_TOKEN, BUY_TOKEN, account.address, 1000, 900, 2000000000)
    sig_mainnet = sign_order(order, chain_id=1, account=account)
    sig_other = sign_order(order, chain_id=137, account=account)
    assert sig_mainnet != sig_other  # домен входит в подпись — разные chainId должны давать разные подписи


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_get_quote_returns_none_on_error(monkeypatch):
    import wakefinder.common.cowswap as mod

    def fake_post(url, json, timeout):
        raise ConnectionError("boom")

    monkeypatch.setattr(mod.requests, "post", fake_post)
    result = asyncio.run(get_quote(SELL_TOKEN, BUY_TOKEN, 1000, "0xFROM"))
    assert result is None


def test_get_quote_returns_payload_on_success(monkeypatch):
    import wakefinder.common.cowswap as mod

    monkeypatch.setattr(mod.requests, "post", lambda url, json, timeout: _FakeResponse(200, {"quote": {"buyAmount": "900", "feeAmount": "10"}}))
    result = asyncio.run(get_quote(SELL_TOKEN, BUY_TOKEN, 1000, "0xFROM"))
    assert result["quote"]["buyAmount"] == "900"


def test_submit_order_returns_none_on_error(monkeypatch):
    import wakefinder.common.cowswap as mod

    monkeypatch.setattr(mod.requests, "post", lambda url, json, timeout: _FakeResponse(400, {}))
    result = asyncio.run(submit_order({}, "0xsig", "0xFROM"))
    assert result is None


def test_wait_for_fill_returns_true_on_fulfilled(monkeypatch):
    import wakefinder.common.cowswap as mod

    monkeypatch.setattr(mod.requests, "get", lambda url, timeout: _FakeResponse(200, {"status": "fulfilled", "executedBuyAmount": "950"}))
    filled, amount = asyncio.run(wait_for_fill("uid123", timeout_seconds=5, poll_interval_seconds=0.01))
    assert filled is True
    assert amount == 950


def test_wait_for_fill_returns_false_on_expired(monkeypatch):
    import wakefinder.common.cowswap as mod

    monkeypatch.setattr(mod.requests, "get", lambda url, timeout: _FakeResponse(200, {"status": "expired"}))
    filled, amount = asyncio.run(wait_for_fill("uid123", timeout_seconds=5, poll_interval_seconds=0.01))
    assert filled is False
    assert amount == 0


def test_wait_for_fill_times_out_when_still_open(monkeypatch):
    import wakefinder.common.cowswap as mod

    monkeypatch.setattr(mod.requests, "get", lambda url, timeout: _FakeResponse(200, {"status": "open"}))
    filled, amount = asyncio.run(wait_for_fill("uid123", timeout_seconds=0.05, poll_interval_seconds=0.01))
    assert filled is False
    assert amount == 0


# --- place_and_wait_for_exit_order orchestration ---

class _FakeAccount:
    address = "0x4444444444444444444444444444444444444444"


def test_place_and_wait_returns_false_when_no_quote(monkeypatch):
    import wakefinder.common.cowswap as mod

    async def fake_get_quote(*a, **k):
        return None

    monkeypatch.setattr(mod, "get_quote", fake_get_quote)
    filled, amount = asyncio.run(place_and_wait_for_exit_order(_FakeAccount(), 1, SELL_TOKEN, BUY_TOKEN, 1000, 900, 180, 60))
    assert filled is False
    assert amount == 0


def test_place_and_wait_returns_false_when_quote_below_minimum(monkeypatch):
    import wakefinder.common.cowswap as mod

    async def fake_get_quote(*a, **k):
        return {"quote": {"buyAmount": "500", "feeAmount": "0"}}

    monkeypatch.setattr(mod, "get_quote", fake_get_quote)
    filled, amount = asyncio.run(place_and_wait_for_exit_order(_FakeAccount(), 1, SELL_TOKEN, BUY_TOKEN, 1000, 900, 180, 60))
    assert filled is False
    assert amount == 0


def test_place_and_wait_returns_false_when_submit_fails(monkeypatch):
    import wakefinder.common.cowswap as mod

    async def fake_get_quote(*a, **k):
        return {"quote": {"buyAmount": "950", "feeAmount": "0"}}

    async def fake_submit_order(*a, **k):
        return None

    monkeypatch.setattr(mod, "get_quote", fake_get_quote)
    monkeypatch.setattr(mod, "sign_order", lambda *a, **k: "0xsig")
    monkeypatch.setattr(mod, "submit_order", fake_submit_order)
    filled, amount = asyncio.run(place_and_wait_for_exit_order(_FakeAccount(), 1, SELL_TOKEN, BUY_TOKEN, 1000, 900, 180, 60))
    assert filled is False
    assert amount == 0


def test_place_and_wait_happy_path_delegates_to_wait_for_fill(monkeypatch):
    import wakefinder.common.cowswap as mod

    async def fake_get_quote(*a, **k):
        return {"quote": {"buyAmount": "950", "feeAmount": "0"}}

    async def fake_submit_order(*a, **k):
        return "order-uid-abc"

    async def fake_wait_for_fill(order_uid, timeout_seconds):
        assert order_uid == "order-uid-abc"
        return True, 950

    monkeypatch.setattr(mod, "get_quote", fake_get_quote)
    monkeypatch.setattr(mod, "sign_order", lambda *a, **k: "0xsig")
    monkeypatch.setattr(mod, "submit_order", fake_submit_order)
    monkeypatch.setattr(mod, "wait_for_fill", fake_wait_for_fill)
    filled, amount = asyncio.run(place_and_wait_for_exit_order(_FakeAccount(), 1, SELL_TOKEN, BUY_TOKEN, 1000, 900, 180, 60))
    assert filled is True
    assert amount == 950


# --- ensure_vault_relayer_approved ---

class _Call:
    def __init__(self, value):
        self._value = value

    async def call(self):
        return self._value


class _ApproveBuilt:
    def build_transaction(self, tx):
        return {**tx, "to": VAULT_RELAYER_ADDRESS, "data": "0xapprove"}


class _ERC20Functions:
    def __init__(self, allowance_value):
        self._allowance = allowance_value

    def allowance(self, owner, spender):
        return _Call(self._allowance)

    def approve(self, spender, amount):
        return _ApproveBuilt()


class _FakeERC20:
    def __init__(self, allowance_value):
        self.functions = _ERC20Functions(allowance_value)


class _FakeEth:
    def __init__(self, allowance_value):
        self._erc20 = _FakeERC20(allowance_value)
        self.approve_sent = False

    def contract(self, address, abi):
        return self._erc20

    async def get_transaction_count(self, addr, tag):
        return 1

    async def get_block(self, tag):
        return {"baseFeePerGas": 10**9}

    async def send_raw_transaction(self, raw):
        self.approve_sent = True
        return b"\xaa" * 32

    async def wait_for_transaction_receipt(self, tx_hash, timeout):
        return {"status": 1}


class _FakeW3:
    def __init__(self, allowance_value):
        self.eth = _FakeEth(allowance_value)


class _SignAccount:
    address = "0x5555555555555555555555555555555555555555"

    def sign_transaction(self, tx):
        class _Signed:
            rawTransaction = b"\x01"
        return _Signed()


def test_ensure_vault_relayer_approved_skips_when_allowance_sufficient():
    w3 = _FakeW3(allowance_value=10**30)
    result = asyncio.run(ensure_vault_relayer_approved(w3, _SignAccount(), 1, SELL_TOKEN, amount=1000))
    assert result is True
    assert w3.eth.approve_sent is False


def test_ensure_vault_relayer_approved_sends_approve_when_insufficient():
    w3 = _FakeW3(allowance_value=0)
    result = asyncio.run(ensure_vault_relayer_approved(w3, _SignAccount(), 1, SELL_TOKEN, amount=1000))
    assert result is True
    assert w3.eth.approve_sent is True
