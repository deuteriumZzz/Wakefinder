"""Регрессионный тест на реальный баг: web3.py 6.20.4 неверно сопоставляет
перегрузку функции, когда позиционно переданы ОБА аргумента — `address[]` и
`address` — в одном вызове (`swapExactTokensForTokens`/`swapExactETHForTokens`/
`swapExactTokensForETH`), выбрасывая `Web3ValidationError` вместо построения
транзакции. Аргументы по имени (`path=..., to=...`) обходят проблему.

Ни один из существующих тестов не вызывал эти функции через РЕАЛЬНЫЙ
`Web3()`-энкодер (везде использовались Fake-объекты `w3`, которые не строят
транзакции по-настоящему) — баг был бы невидим для всего проекта, пока
кто-то не попытался бы реально отправить сделку. Эти тесты дёргают
_sign_leg/_sign_swap напрямую (синхронные, без RPC) и напрямую строят
swapExactTokensForETH тем же способом, что и chains/eth/snipe.py._sell —
третий паттерн вызова, которого нет в snipe_filter.py."""

import os

os.environ.setdefault("ETH_RPC_WS_URL", "wss://example/ws")
os.environ.setdefault("ETH_RPC_HTTP_URL", "https://example/http")
os.environ.setdefault("ETH_PRIVATE_KEY", "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")
os.environ.setdefault("FLASHBOTS_SIGNER_KEY", "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99")

import asyncio  # noqa: E402

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

from wakefinder.chains.eth.abi import ROUTER_ABI  # noqa: E402
from wakefinder.chains.eth.copytrade import _sign_swap  # noqa: E402
from wakefinder.chains.eth.main import _sign_leg  # noqa: E402
from wakefinder.chains.eth.snipe import _buy_backrun  # noqa: E402

ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TOKEN = "0x1111111111111111111111111111111111111111"
ACCOUNT = Account.from_key("0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")


def test_sign_leg_builds_real_transaction_without_raising():
    raw = _sign_leg(ROUTER, ACCOUNT, chain_id=1, nonce=0, max_fee=10**10, priority_fee=10**9, path=[WETH, TOKEN], amount_in=10**18, amount_out_min=1)
    assert isinstance(raw, bytes)
    assert len(raw) > 0


def test_sign_swap_builds_real_transaction_without_raising():
    raw = _sign_swap(ROUTER, ACCOUNT, 1, 0, 10**10, 10**9, [WETH, TOKEN], 10**18, 1)
    assert isinstance(raw, bytes)
    assert len(raw) > 0


def test_swap_exact_tokens_for_eth_builds_without_raising():
    """Тот же паттерн, что chains/eth/snipe.py:_sell — единственная функция
    с этой конкретной перегрузкой, не покрытая _sign_leg/_sign_swap выше."""
    encoder = Web3()
    router = encoder.eth.contract(address=ROUTER, abi=ROUTER_ABI)
    tx = router.functions.swapExactTokensForETH(
        amountIn=10**18, amountOutMin=1, path=[TOKEN, WETH], to=ACCOUNT.address, deadline=9999999999,
    ).build_transaction({
        "from": ACCOUNT.address, "nonce": 0, "gas": 250_000,
        "maxFeePerGas": 10**10, "maxPriorityFeePerGas": 10**9, "chainId": 1,
    })
    raw = ACCOUNT.sign_transaction(tx).rawTransaction
    assert isinstance(raw, bytes)
    assert len(raw) > 0


class _Awaitable:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _coro():
            return self.value
        return _coro().__await__()


class FakeW3:
    """Только то, что нужно _buy_backrun: nonce + latest block для fee-расчёта."""

    @property
    def eth(self):
        return self

    def get_transaction_count(self, address, block_identifier=None):
        return _Awaitable(0)

    def get_block(self, identifier):
        return _Awaitable({"baseFeePerGas": 10**9})


class FakeSender:
    def __init__(self, included: bool):
        self._included = included
        self.sent_bundle = None

    async def send(self, bundle):
        self.sent_bundle = bundle
        return self._included


def test_buy_backrun_signs_real_transaction_and_orders_bundle_victim_first():
    """_buy_backrun — новый путь этой сессии (backrun-снайпинг, ETH same-block
    entry), собирает бандл [victim_raw, buy_raw] реальным Web3()-энкодером и
    отправляет его через sender.send() — тот же класс риска, что и баги,
    из-за которых появился этот файл: нужно исполнить РЕАЛЬНОЕ подписание,
    не Fake."""
    w3 = FakeW3()
    sender = FakeSender(included=True)
    victim_raw = b"\x01\x02\x03"

    included, tx_hash, expected_out = asyncio.run(_buy_backrun(
        w3, sender, ACCOUNT, ROUTER, chain_id=1, weth_address=WETH, token=TOKEN,
        amount_in_wei=10**17, victim_raw=victim_raw, reserve_weth=10 * 10**18, reserve_token=1000 * 10**18,
        target_block=101,
    ))

    assert included is True
    assert expected_out > 0
    assert tx_hash.startswith("0x")

    assert sender.sent_bundle is not None
    assert sender.sent_bundle.target_block == 101
    assert len(sender.sent_bundle.raw_txs) == 2
    assert sender.sent_bundle.raw_txs[0] == "0x010203"  # victim_raw идёт ПЕРВЫМ
    assert sender.sent_bundle.raw_txs[1].startswith("0x")
    assert len(sender.sent_bundle.raw_txs[1]) > 10  # реально подписанная транзакция, не заглушка


if __name__ == "__main__":
    test_sign_leg_builds_real_transaction_without_raising()
    test_sign_swap_builds_real_transaction_without_raising()
    test_swap_exact_tokens_for_eth_builds_without_raising()
    test_buy_backrun_signs_real_transaction_and_orders_bundle_victim_first()
    print("ok")
