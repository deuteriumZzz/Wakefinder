"""Отправка ПОДПИСАННОЙ транзакции через MEV-protect RPC (Flashbots Protect
`https://rpc.flashbots.net`, MEV Blocker `https://rpc.mevblocker.io` и т.п.)
вместо обычного узла — опциональная ЗАЩИТА для entry-путей, которые
сознательно идут в публичный мемпул ради скорости
(chains/eth/copytrade.py, chains/eth/snipe.py mined-path) и поэтому уязвимы
к sandwich-атакам. common/sandwich_detector.py детектирует это ПОСТФАКТУМ;
этот модуль — ПРЕДОТВРАЩЕНИЕ, компромисс: чуть медленнее (лишний RPC-URL),
зато билдеры получают транзакцию напрямую, минуя публичный p2p-мемпул,
так что её физически не видно ботам, сканирующим мемпул в поисках жертв.

Протокол — обычный `eth_sendRawTransaction`, эти эндпоинты СОВМЕСТИМЫ по
интерфейсу с любым узлом, просто не транслируют транзакцию дальше в p2p.
Receipt всё ещё проверяется через ОСНОВНОЙ w3 — protect RPC нужен только
для отправки, не для чтения состояния (после включения в блок транзакция
видна как обычная на любом узле).

AsyncHTTPProvider кэширует aiohttp-сессию по URL на уровне модуля web3.py
(`web3/_utils/request.py:async_cache_and_return_session`, проверено чтением
исходника) — создавать новый `AsyncWeb3(AsyncHTTPProvider(url))` на каждый
вызов НЕ утечка (тот же паттерн уже используется в live_state.py для
разовых ETH-подключений, в отличие от Solana AsyncClient, который
кэширования не делает и поэтому требует явного `async with`)."""

from web3 import AsyncHTTPProvider, AsyncWeb3


async def send_raw_via_protected_rpc(protect_rpc_url: str, raw: bytes):
    protect_w3 = AsyncWeb3(AsyncHTTPProvider(protect_rpc_url))
    return await protect_w3.eth.send_raw_transaction(raw)
