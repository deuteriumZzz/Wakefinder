"""Fork-тест против РЕАЛЬНОГО состояния mainnet через anvil (Foundry) — не
заглушки, как в остальных интеграционных тестах, а настоящие Uniswap V2/
Sushiswap контракты на форкнутом состоянии. Ловит расхождение между нашими
ABI-фрагментами (chains/eth/abi.py) и реальным задеплоенным байткодом — то,
что моки в остальных тестах структурно поймать не могут (мок по определению
ведёт себя так, как мы сами его написали в test_eth_simulator_integration.py).

Требует anvil в PATH (`curl -L https://foundry.paradigm.xyz | bash && foundryup`)
и доступ в интернет к публичному RPC — весь модуль пропускается, если anvil
не найден или не поднялся за FORK_STARTUP_TIMEOUT_SECONDS (см. README
"Fork-тесты"). Не влияет на остальной `pytest tests/`: без anvil эти тесты
просто помечаются skipped, не failed.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest
from web3 import AsyncHTTPProvider, AsyncWeb3

from wakefinder.chains.eth.abi import PAIR_ABI
from wakefinder.chains.eth.simulator import KNOWN_DEX_FACTORIES, TwoPoolArbSimulator
from wakefinder.common.interfaces import PendingSwap

os.environ.setdefault("ETH_RPC_WS_URL", "wss://example/ws")
os.environ.setdefault("ETH_RPC_HTTP_URL", "https://example/http")
os.environ.setdefault("ETH_PRIVATE_KEY", "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")
os.environ.setdefault("FLASHBOTS_SIGNER_KEY", "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99")

ANVIL_AVAILABLE = shutil.which("anvil") is not None
FORK_RPC_URL = os.environ.get("FORK_RPC_URL", "https://ethereum-rpc.publicnode.com")
FORK_STARTUP_TIMEOUT_SECONDS = 25

# Реальные, проверенные через cast/getPair() на форке (не угаданные) адреса.
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
UNISWAP_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
UNISWAP_WETH_USDC_PAIR = "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc"
SUSHI_ROUTER = "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F"
SUSHI_WETH_USDC_PAIR = "0x397FF1542f962076d0BFE58eA045FfA2d347ACa0"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def anvil_rpc():
    if not ANVIL_AVAILABLE:
        pytest.skip("anvil (Foundry) не найден в PATH — см. README 'Fork-тесты'")

    port = _find_free_port()
    proc = subprocess.Popen(
        ["anvil", "--fork-url", FORK_RPC_URL, "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + FORK_STARTUP_TIMEOUT_SECONDS
    ready = False
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url, data=b'{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}',
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.5)

    if not ready:
        proc.terminate()
        pytest.skip("anvil не поднялся вовремя — нет сети до FORK_RPC_URL?")

    yield url
    proc.terminate()
    proc.wait(timeout=10)


def test_fork_real_pair_reserves_and_token_order(anvil_rpc):
    async def _run():
        w3 = AsyncWeb3(AsyncHTTPProvider(anvil_rpc))
        pool = w3.eth.contract(address=UNISWAP_WETH_USDC_PAIR, abi=PAIR_ABI)
        token0 = await pool.functions.token0().call()
        token1 = await pool.functions.token1().call()
        reserve0, reserve1, _ = await pool.functions.getReserves().call()

        assert {token0.lower(), token1.lower()} == {WETH.lower(), USDC.lower()}
        assert reserve0 > 0
        assert reserve1 > 0

    asyncio.run(_run())


def test_fork_simulator_runs_against_real_contracts_without_error(anvil_rpc):
    """Не проверяет profitable (реальный рынок обычно эффективен, гарантии
    возможности нет) — проверяет, что весь пайплайн (getReserves/token0/
    getAmountsOut на РЕАЛЬНЫХ контрактах) отрабатывает без исключений,
    то есть наши ABI-фрагменты байт-в-байт совместимы с задеплоенным кодом."""
    async def _run():
        w3 = AsyncWeb3(AsyncHTTPProvider(anvil_rpc))
        simulator = TwoPoolArbSimulator(
            w3, target_router=UNISWAP_ROUTER,
            reference_pools={UNISWAP_WETH_USDC_PAIR: {"pool": SUSHI_WETH_USDC_PAIR, "router": SUSHI_ROUTER}},
            weth_address=WETH,
        )
        swap = PendingSwap(
            tx_hash="0xfork-test", pool_address=UNISWAP_WETH_USDC_PAIR,
            token_in=WETH, token_out=USDC, amount_in=10**18,  # 1 WETH — заведомо маленький своп
        )
        sim = await simulator.simulate(swap)
        assert sim is not None  # дошли до конца без исключения — это и есть цель теста

    asyncio.run(_run())


def test_fork_auto_discovers_real_sushiswap_pool(anvil_rpc):
    """Без предзаданного reference_pools вообще — только auto_discover_factories.
    Подтверждает, что getPair() на РЕАЛЬНОЙ Sushiswap-фабрике находит именно
    тот пул, который мы независимо проверили через cast call при написании
    этого файла (SUSHI_WETH_USDC_PAIR), не какой-то другой/нулевой адрес."""
    async def _run():
        w3 = AsyncWeb3(AsyncHTTPProvider(anvil_rpc))
        simulator = TwoPoolArbSimulator(
            w3, target_router=UNISWAP_ROUTER, reference_pools={},
            weth_address=WETH, auto_discover_factories=KNOWN_DEX_FACTORIES,
        )
        block_number = await w3.eth.block_number
        discovered = await simulator._discover_pool(
            KNOWN_DEX_FACTORIES["sushiswap"][0], WETH, USDC, block_number,
        )
        assert discovered is not None
        assert discovered.lower() == SUSHI_WETH_USDC_PAIR.lower()

    asyncio.run(_run())


if __name__ == "__main__":
    print("run via pytest (требует anvil в PATH и сеть до FORK_RPC_URL)")
