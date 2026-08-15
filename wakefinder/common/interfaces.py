from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class PendingSwap:
    """Расшифрованное намерение свопа, замеченное в мемпуле."""

    tx_hash: str
    pool_address: str
    token_in: str
    token_out: str
    amount_in: int
    sender: str = ""  # отправитель, если известен (нужен для watchlist/консенсус-логики; Solana-вариант не всегда может его дать)


@dataclass
class SimResult:
    """Результат симуляции стратегии по конкретному pending-свопу.

    Несёт всё необходимое, чтобы собрать сделку точно такой, какой она была
    просимулирована — исполнитель не должен сам заново выводить суммы/направление/
    площадки (именно такое расхождение приводит к тому, что бот подписывает
    не ту сделку, которую оценивал)."""

    profitable: bool
    expected_profit_wei: int
    amount_in: int = 0
    bought_amount: int = 0  # выход ноги 1 = вход ноги 2
    buy_router: str = ""
    sell_router: str = ""
    reason: str = ""


@dataclass
class Bundle:
    """Последовательность подписанных сырых транзакций для атомарной отправки."""

    raw_txs: list[str]
    target_block: int


class MempoolWatcher(ABC):
    @abstractmethod
    async def watch(self) -> AsyncIterator[PendingSwap]:
        """Отдаёт расшифрованные свопы китов по мере появления в мемпуле."""


class Simulator(ABC):
    @abstractmethod
    async def simulate(self, swap: PendingSwap) -> SimResult:
        """Оценивает прибыль от реакции на этот своп, до траты газа."""


class BundleSender(ABC):
    @abstractmethod
    async def send(self, bundle: Bundle) -> bool:
        """Отправляет bundle в relay. Возвращает True, если он попал в блок."""
