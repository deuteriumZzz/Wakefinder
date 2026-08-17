"""Тест wakefinder.chains.solana.backtest.run_backtest с фейковым AsyncClient
— проверяет склейку (реконструкция свопа из pre/postTokenBalances одной
транзакции -> optimal_arb), не сеть."""

import asyncio
import os

os.environ.setdefault("ETH_RPC_WS_URL", "wss://example/ws")
os.environ.setdefault("ETH_RPC_HTTP_URL", "https://example/http")
os.environ.setdefault("ETH_PRIVATE_KEY", "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")
os.environ.setdefault("FLASHBOTS_SIGNER_KEY", "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99")

from solders.keypair import Keypair  # noqa: E402

from wakefinder.chains.solana.backtest import run_backtest  # noqa: E402

# Pubkey.from_string() внутри run_backtest требует настоящий валидный base58 —
# генерируем реальные (но случайные, не привязанные ни к чему) ключи вместо
# строковых заглушек.
TARGET_BASE = str(Keypair().pubkey())
TARGET_QUOTE = str(Keypair().pubkey())
REF_BASE = str(Keypair().pubkey())
REF_QUOTE = str(Keypair().pubkey())
BASE_MINT = "So11111111111111111111111111111111111111112"
QUOTE_MINT = str(Keypair().pubkey())


class _Key:
    def __init__(self, pubkey):
        self.pubkey = pubkey


class _Balance:
    def __init__(self, account_index, amount):
        self.account_index = account_index
        self.ui_token_amount = type("U", (), {"amount": str(amount)})()


class _Meta:
    def __init__(self, pre, post):
        self.pre_token_balances = pre
        self.post_token_balances = post


class _Message:
    def __init__(self, account_keys):
        self.account_keys = [_Key(k) for k in account_keys]


class _InnerTx:
    def __init__(self, account_keys):
        self.message = _Message(account_keys)


class _TxMid:
    def __init__(self, meta, account_keys):
        self.meta = meta
        self.transaction = _InnerTx(account_keys)


class _TxOuter:
    def __init__(self, meta, account_keys):
        self.transaction = _TxMid(meta, account_keys)


class _Resp:
    def __init__(self, value):
        self.value = value


class _SigEntry:
    def __init__(self, signature, slot=1):
        self.signature = signature
        self.slot = slot


class FakeAsyncClient:
    def __init__(self, signatures: dict, transactions: dict, slots: dict | None = None):
        """signatures: {vault_address: [sig, ...]} (порядок — от новых к старым, как реальный RPC).
        transactions: {sig: (meta, account_keys)}. slots: {sig: slot}, по умолчанию 1 для всех —
        нужен только тестам, которые проверяют contested_opportunities (см. ниже)."""
        self._signatures = signatures
        self._transactions = transactions
        self._slots = slots or {}

    async def get_signatures_for_address(self, pubkey, before=None, limit=None):
        # Реальный RPC фильтрует по слоту/времени сигнатуры `before`, не по
        # принадлежности её истории ЭТОГО аккаунта — здесь фильтруем только
        # когда сигнатура действительно из истории этого же vault'а
        # (единственный случай, который нужно моделировать в тестах ниже).
        sigs = self._signatures.get(str(pubkey), [])
        if before is not None and before in sigs:
            idx = sigs.index(before)
            sigs = sigs[idx + 1:]
        return _Resp([_SigEntry(s, slot=self._slots.get(s, 1)) for s in sigs[:limit]])

    async def get_transaction(self, sig, encoding=None, max_supported_transaction_version=None):
        entry = self._transactions.get(sig)
        if entry is None:
            return _Resp(None)
        meta, account_keys = entry
        return _Resp(_TxOuter(meta, account_keys))


def _account_keys():
    return [TARGET_BASE, TARGET_QUOTE, REF_BASE, REF_QUOTE]


def test_run_backtest_finds_opportunity_from_historical_swap():
    keys = _account_keys()
    # своп цели: base вырос (1000 -> 1010), quote упал (800 -> 790) -> token_in = BASE_MINT
    target_meta = _Meta(
        pre=[_Balance(0, 1_000_000_000_000), _Balance(1, 800_000_000_000)],
        post=[_Balance(0, 1_010_000_000_000), _Balance(1, 790_000_000_000)],
    )
    # референсный пул на момент до свопа цели — дёшево купить BASE_MINT относительно target
    ref_meta = _Meta(pre=[], post=[_Balance(2, 1_000_000_000_000), _Balance(3, 1_000_000_000_000)])

    client = FakeAsyncClient(
        signatures={TARGET_BASE: ["SIG1"], REF_BASE: ["REFSIG"]},
        transactions={"SIG1": (target_meta, keys), "REFSIG": (ref_meta, keys)},
    )

    result = asyncio.run(run_backtest(
        client,
        reference_pools={
            "pool1": {
                "base_vault": REF_BASE, "quote_vault": REF_QUOTE,
                "base_mint": BASE_MINT, "quote_mint": QUOTE_MINT,
                "target_base_vault": TARGET_BASE, "target_quote_vault": TARGET_QUOTE,
            },
        },
    ))

    assert result.swaps_scanned == 1
    assert result.opportunities_found == 1
    assert result.total_simulated_profit_lamports > 0


def test_run_backtest_flags_contested_opportunity_with_multiple_txs_same_slot():
    keys = _account_keys()
    target_meta_1 = _Meta(
        pre=[_Balance(0, 1_000_000_000_000), _Balance(1, 800_000_000_000)],
        post=[_Balance(0, 1_010_000_000_000), _Balance(1, 790_000_000_000)],
    )
    target_meta_2 = _Meta(
        pre=[_Balance(0, 1_010_000_000_000), _Balance(1, 790_000_000_000)],
        post=[_Balance(0, 1_020_000_000_000), _Balance(1, 780_000_000_000)],
    )
    ref_meta = _Meta(pre=[], post=[_Balance(2, 1_000_000_000_000), _Balance(3, 1_000_000_000_000)])

    client = FakeAsyncClient(
        signatures={TARGET_BASE: ["SIG1", "SIG2"], REF_BASE: ["REFSIG"]},
        transactions={"SIG1": (target_meta_1, keys), "SIG2": (target_meta_2, keys), "REFSIG": (ref_meta, keys)},
        slots={"SIG1": 5, "SIG2": 5},  # оба в одном слоте -> "оживлённо"
    )

    result = asyncio.run(run_backtest(
        client,
        reference_pools={
            "pool1": {
                "base_vault": REF_BASE, "quote_vault": REF_QUOTE,
                "base_mint": BASE_MINT, "quote_mint": QUOTE_MINT,
                "target_base_vault": TARGET_BASE, "target_quote_vault": TARGET_QUOTE,
            },
        },
    ))

    assert result.swaps_scanned == 2
    assert result.opportunities_found == 2
    assert result.contested_opportunities == 2


def test_run_backtest_skips_non_swap_transaction():
    keys = _account_keys()
    # обе стороны выросли -> не своп (например, добавление ликвидности)
    target_meta = _Meta(
        pre=[_Balance(0, 1_000_000_000_000), _Balance(1, 800_000_000_000)],
        post=[_Balance(0, 1_010_000_000_000), _Balance(1, 810_000_000_000)],
    )
    client = FakeAsyncClient(
        signatures={TARGET_BASE: ["SIG1"]},
        transactions={"SIG1": (target_meta, keys)},
    )

    result = asyncio.run(run_backtest(
        client,
        reference_pools={
            "pool1": {
                "base_vault": REF_BASE, "quote_vault": REF_QUOTE,
                "base_mint": BASE_MINT, "quote_mint": QUOTE_MINT,
                "target_base_vault": TARGET_BASE, "target_quote_vault": TARGET_QUOTE,
            },
        },
    ))

    assert result.swaps_scanned == 0
    assert result.opportunities_found == 0


def test_run_backtest_skips_swap_without_reference_history():
    keys = _account_keys()
    target_meta = _Meta(
        pre=[_Balance(0, 1_000_000_000_000), _Balance(1, 800_000_000_000)],
        post=[_Balance(0, 1_010_000_000_000), _Balance(1, 790_000_000_000)],
    )
    client = FakeAsyncClient(
        signatures={TARGET_BASE: ["SIG1"], REF_BASE: []},  # у референсного пула нет истории
        transactions={"SIG1": (target_meta, keys)},
    )

    result = asyncio.run(run_backtest(
        client,
        reference_pools={
            "pool1": {
                "base_vault": REF_BASE, "quote_vault": REF_QUOTE,
                "base_mint": BASE_MINT, "quote_mint": QUOTE_MINT,
                "target_base_vault": TARGET_BASE, "target_quote_vault": TARGET_QUOTE,
            },
        },
    ))

    assert result.swaps_scanned == 1
    assert result.opportunities_found == 0


if __name__ == "__main__":
    test_run_backtest_finds_opportunity_from_historical_swap()
    test_run_backtest_flags_contested_opportunity_with_multiple_txs_same_slot()
    test_run_backtest_skips_non_swap_transaction()
    test_run_backtest_skips_swap_without_reference_history()
    print("ok")
