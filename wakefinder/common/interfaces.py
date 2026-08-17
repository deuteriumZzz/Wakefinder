import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar


class HasTxHash(Protocol):
    """Общий контракт для всего, что common/race.py умеет ставить в гонку
    между провайдерами — PendingSwap/NewPool/PendingLiquidityAdd/NewMint не
    имеют общего базового класса (разные по смыслу события), но race_watchers
    трогает только .tx_hash для дедупликации, так что Protocol честнее
    искусственной общей иерархии наследования."""

    tx_hash: str


TxHashEvent = TypeVar("TxHashEvent", bound=HasTxHash)


@dataclass
class PendingSwap:
    """Расшифрованное намерение свопа, замеченное в мемпуле."""

    tx_hash: str
    pool_address: str
    token_in: str
    token_out: str
    amount_in: int
    sender: str = ""  # отправитель, если известен (нужен для watchlist/консенсус-логики; Solana-вариант не всегда может его дать)
    # Момент, когда watcher РЕШИЛ, что это оно — не момент попадания в мемпул
    # ноды (то мы не видим), а начало НАШЕГО пайплайна реакции. Используется
    # для замера latency "детекция -> отправка" (см. common/latency.py) —
    # честная нижняя граница задержки, не полная картина от появления в сети.
    detected_at: float = field(default_factory=time.time)


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
    detected_at: float = field(default_factory=time.time)


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
    detected_at: float = field(default_factory=time.time)


@dataclass
class NewMint:
    """Только что созданный SPL-минт на Solana (InitializeMint/InitializeMint2
    Token Program) — аналог NewPool для ETH, но БЕЗ гарантии, что у минта
    вообще будет ликвидность (mint != пул) — см. chains/solana/mint_watcher.py
    и snipe_filter.py:check_mint_tradeable (подтверждение через Jupiter)."""

    tx_hash: str
    mint_address: str
    slot: int
    detected_at: float = field(default_factory=time.time)


@dataclass
class PendingLiquidation:
    """PENDING (не подтверждённый) вызов Aave V3 Pool.liquidationCall(),
    замеченный в публичном мемпуле — см. chains/eth/liquidation_watcher.py.
    Aave допускает НЕСКОЛЬКО одновременных вызовов liquidationCall() на одну
    и ту же недообеспеченную позицию (первый замайненный получает discount,
    остальные ревертят) — значит увидеть чужую pending-транзакцию означает
    "эта позиция liquidatable ПРЯМО СЕЙЧАС", и можно попробовать
    сконкурировать за то же самое включение своей копией того же вызова с
    более высоким tip, а не обязательно искать позиции самостоятельно."""

    tx_hash: str
    collateral_asset: str
    debt_asset: str
    user: str
    debt_to_cover: int
    detected_at: float = field(default_factory=time.time)


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


class MempoolWatcher(ABC, Generic[TxHashEvent]):
    @abstractmethod
    async def watch(self) -> AsyncIterator[TxHashEvent]:
        """Отдаёт расшифрованные события по мере появления в мемпуле — тип
        события специфичен подклассу (PendingSwap/NewPool/PendingLiquidityAdd/
        NewMint), см. TxHashEvent выше."""
        yield None  # type: ignore[misc]  # pragma: no cover -- никогда не исполняется (@abstractmethod), нужен только чтобы mypy распознал сигнатуру как async generator, а не coroutine


class Simulator(ABC):
    @abstractmethod
    async def simulate(self, swap: PendingSwap) -> SimResult:
        """Оценивает прибыль от реакции на этот своп, до траты газа."""


class BundleSender(ABC):
    @abstractmethod
    async def send(self, bundle: Bundle) -> bool:
        """Отправляет bundle в relay. Возвращает True, если он попал в блок."""
