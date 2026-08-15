"""Интеграционный тест Solana TwoPoolArbSimulator с фейковым AsyncClient —
тот же принцип, что и tests/test_eth_simulator_integration.py: проверяет
реальную склейку (направление арбитража, guard на тонкий референсный пул),
не только чистую математику amm.py."""

import asyncio
import os

os.environ.setdefault("ETH_RPC_WS_URL", "wss://example/ws")
os.environ.setdefault("ETH_RPC_HTTP_URL", "https://example/http")
os.environ.setdefault("ETH_PRIVATE_KEY", "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")
os.environ.setdefault("FLASHBOTS_SIGNER_KEY", "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99")

from solders.pubkey import Pubkey  # noqa: E402

from wakefinder.chains.solana.simulator import TwoPoolArbSimulator  # noqa: E402
from wakefinder.common.interfaces import PendingSwap  # noqa: E402


class _Value:
    def __init__(self, amount: int):
        self.amount = str(amount)


class _Resp:
    def __init__(self, amount: int):
        self.value = _Value(amount)


class FakeClient:
    def __init__(self, balances: dict[str, int]):
        self._balances = balances

    async def get_token_account_balance(self, pubkey):
        return _Resp(self._balances[str(pubkey)])


class FakeJupiter:
    """converter=lambda _: None имитирует отсутствие маршрута wSOL/token_in."""

    def __init__(self, converter):
        self._converter = converter

    async def quote(self, input_mint, output_mint, amount, slippage_bps, only_direct_routes):
        result = self._converter(amount)
        if result is None:
            raise RuntimeError("нет маршрута wSOL/token_in")
        return {"outAmount": str(result)}


BASE_MINT = str(Pubkey.new_unique())
QUOTE_MINT = str(Pubkey.new_unique())
TARGET_BASE_VAULT = str(Pubkey.new_unique())
TARGET_QUOTE_VAULT = str(Pubkey.new_unique())
REF_BASE_VAULT = str(Pubkey.new_unique())
REF_QUOTE_VAULT = str(Pubkey.new_unique())


def _ref(target_base, target_quote, ref_base, ref_quote, target_dex="target-dex", dex="ref-dex"):
    cfg = {
        "target_base_vault": TARGET_BASE_VAULT,
        "target_quote_vault": TARGET_QUOTE_VAULT,
        "base_vault": REF_BASE_VAULT,
        "quote_vault": REF_QUOTE_VAULT,
        "base_mint": BASE_MINT,
        "quote_mint": QUOTE_MINT,
        "target_dex_label": target_dex,
        "dex_label": dex,
    }
    balances = {
        TARGET_BASE_VAULT: target_base,
        TARGET_QUOTE_VAULT: target_quote,
        REF_BASE_VAULT: ref_base,
        REF_QUOTE_VAULT: ref_quote,
    }
    return cfg, balances


def test_simulator_buys_in_reference_sells_in_target():
    scale = 10**9
    ref_cfg, balances = _ref(target_base=1_000 * scale, target_quote=800 * scale, ref_base=1_000 * scale, ref_quote=1_000 * scale)
    client = FakeClient(balances)
    # token_in == wsol_mint -> быстрый путь без Jupiter-конвертации
    simulator = TwoPoolArbSimulator(client, reference_pools={"pool-1": ref_cfg}, wsol_mint=BASE_MINT)

    swap = PendingSwap(tx_hash="slot-delta:pool-1:1", pool_address="pool-1", token_in=BASE_MINT, token_out=QUOTE_MINT, amount_in=10 * scale)
    sim = asyncio.run(simulator.simulate(swap))

    assert sim.profitable
    assert sim.expected_profit_wei > 0
    assert sim.buy_router == "ref-dex"
    assert sim.sell_router == "target-dex"


def test_simulator_rejects_thin_reference_pool():
    scale = 10**9
    # та же выгодная пропорция, что и выше, но референсный пул ниже
    # MIN_REFERENCE_LIQUIDITY_SOL (дефолт 10 SOL) — дёшево манипулируемый.
    ref_cfg, balances = _ref(target_base=1_000 * scale, target_quote=800 * scale, ref_base=int(0.5 * scale), ref_quote=int(0.5 * scale))
    client = FakeClient(balances)
    simulator = TwoPoolArbSimulator(client, reference_pools={"pool-1": ref_cfg}, wsol_mint=BASE_MINT)

    swap = PendingSwap(tx_hash="slot-delta:pool-1:1", pool_address="pool-1", token_in=BASE_MINT, token_out=QUOTE_MINT, amount_in=10 * scale)
    sim = asyncio.run(simulator.simulate(swap))

    assert not sim.profitable
    assert "тонкий" in sim.reason


def test_simulator_converts_gas_and_cap_for_non_wsol_token_in():
    scale = 10**9
    wsol = str(Pubkey.new_unique())  # НЕ BASE_MINT -> должен пойти через Jupiter-конвертацию
    ref_cfg, balances = _ref(target_base=1_000 * scale, target_quote=800 * scale, ref_base=1_000 * scale, ref_quote=1_000 * scale)
    client = FakeClient(balances)
    jupiter = FakeJupiter(converter=lambda lamports: lamports * 2)  # некий курс wSOL/token_in

    simulator = TwoPoolArbSimulator(client, reference_pools={"pool-1": ref_cfg}, wsol_mint=wsol, jupiter=jupiter)
    swap = PendingSwap(tx_hash="slot-delta:pool-1:1", pool_address="pool-1", token_in=BASE_MINT, token_out=QUOTE_MINT, amount_in=10 * scale)

    sim = asyncio.run(simulator.simulate(swap))

    assert sim.profitable
    assert sim.expected_profit_wei > 0


def test_simulator_rejects_when_no_jupiter_route():
    scale = 10**9
    wsol = str(Pubkey.new_unique())
    ref_cfg, balances = _ref(target_base=1_000 * scale, target_quote=800 * scale, ref_base=1_000 * scale, ref_quote=1_000 * scale)
    client = FakeClient(balances)
    jupiter = FakeJupiter(converter=lambda lamports: None)  # нет маршрута

    simulator = TwoPoolArbSimulator(client, reference_pools={"pool-1": ref_cfg}, wsol_mint=wsol, jupiter=jupiter)
    swap = PendingSwap(tx_hash="slot-delta:pool-1:1", pool_address="pool-1", token_in=BASE_MINT, token_out=QUOTE_MINT, amount_in=10 * scale)

    sim = asyncio.run(simulator.simulate(swap))

    assert not sim.profitable
    assert "сконвертировать" in sim.reason


if __name__ == "__main__":
    test_simulator_buys_in_reference_sells_in_target()
    test_simulator_rejects_thin_reference_pool()
    test_simulator_converts_gas_and_cap_for_non_wsol_token_in()
    test_simulator_rejects_when_no_jupiter_route()
    print("ok")
