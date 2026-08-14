from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class PendingSwap:
    """A decoded swap intent seen in the mempool."""

    tx_hash: str
    pool_address: str
    token_in: str
    token_out: str
    amount_in: int


@dataclass
class SimResult:
    """Result of simulating a strategy against a pending swap.

    Carries everything needed to build the trade as-simulated — the executor
    must not re-derive amounts/direction/venues itself (that mismatch is exactly
    how a bot signs a different trade than the one it priced)."""

    profitable: bool
    expected_profit_wei: int
    amount_in: int = 0
    bought_amount: int = 0  # leg 1 output = leg 2 input
    buy_router: str = ""
    sell_router: str = ""
    reason: str = ""


@dataclass
class Bundle:
    """A sequence of raw signed transactions to submit atomically."""

    raw_txs: list[str]
    target_block: int


class MempoolWatcher(ABC):
    @abstractmethod
    async def watch(self) -> AsyncIterator[PendingSwap]:
        """Yield decoded whale swaps as they appear in the mempool."""


class Simulator(ABC):
    @abstractmethod
    async def simulate(self, swap: PendingSwap) -> SimResult:
        """Estimate profit for reacting to this swap, before spending gas."""


class BundleSender(ABC):
    @abstractmethod
    async def send(self, bundle: Bundle) -> bool:
        """Submit a bundle to the relay. Returns True if included."""
