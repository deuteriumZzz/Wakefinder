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
class NewPool:
    """Только что созданная пара на DEX (Factory PairCreated) — снайпинг
    реагирует на САМ ФАКТ появления пула, а не на своп внутри него, поэтому
    не переиспользует PendingSwap (нет token_in/token_out/amount_in до
    первого реального свопа)."""

    tx_hash: str
    pool_address: str
    token0: str
    token1: str
    block_number: int


@dataclass
class PendingLiquidityAdd:
    """PENDING (не подтверждённый) вызов addLiquidityETH на роутере — в
    отличие от NewPool (уже смайненное событие PairCreated), это сигнал ДО
    того, как пара реально появилась в блоке, нужный для backrun-снайпинга
    (chains/eth/liquidity_watcher.py, снайпинг в ТОМ ЖЕ блоке через Flashbots-
    бандл [victim_raw, buy_raw], а не после того, как пара уже смайнена).
    amount_token_desired/amount_eth — это НАМЕРЕНИЕ создателя пула из
    calldata, не гарантированный факт: транзакция может не попасть в блок
    вообще, или router может добавить МЕНЬШЕ (см. docstring watcher'а)."""

    tx_hash: str
    token: str
    amount_token_desired: int
    amount_eth: int


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
