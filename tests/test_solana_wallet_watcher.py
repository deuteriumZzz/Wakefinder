"""Тест WalletSwapWatcher._sync_subscriptions — до/отписка под live-конфиг
без реконнекта. Полный watch() (реальный WebSocket-протокол) не мокается
целиком — фокус на новой логике синхронизации подписок."""

import asyncio

from solders.keypair import Keypair

from wakefinder.chains.solana.wallet_watcher import WalletSwapWatcher

# Pubkey.from_string() внутри _sync_subscriptions требует настоящий валидный
# base58 — генерируем реальные (но случайные) ключи вместо строковых заглушек.
WALLET_A = str(Keypair().pubkey())
WALLET_B = str(Keypair().pubkey())


class FakeWs:
    def __init__(self):
        self._next_sub_id = 1000  # выше любых вручную заданных id в тестах ниже — избегаем коллизий
        self.subscribe_calls = []
        self.unsubscribe_calls = []

    async def logs_subscribe(self, filter_, commitment=None):
        self.subscribe_calls.append(filter_)

    async def recv(self):
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        return [type("Confirmation", (), {"result": sub_id})()]

    async def logs_unsubscribe(self, subscription):
        self.unsubscribe_calls.append(subscription)


def _watcher(watched_wallets):
    return WalletSwapWatcher(ws_url="wss://example", client=None, watched_wallets=watched_wallets)


def test_sync_subscriptions_adds_new_wallet():
    watcher = _watcher({WALLET_A})
    ws = FakeWs()
    sub_ids: dict[int, str] = {}
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert len(ws.subscribe_calls) == 1
    assert list(sub_ids.values()) == [WALLET_A]
    assert ws.unsubscribe_calls == []


def test_sync_subscriptions_removes_stale_wallet():
    watcher = _watcher(set())
    ws = FakeWs()
    sub_ids = {1: WALLET_A}
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert ws.unsubscribe_calls == [1]
    assert sub_ids == {}
    assert ws.subscribe_calls == []


def test_sync_subscriptions_leaves_unchanged_wallet_alone():
    watcher = _watcher({WALLET_A})
    ws = FakeWs()
    sub_ids = {1: WALLET_A}
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert ws.subscribe_calls == []
    assert ws.unsubscribe_calls == []
    assert sub_ids == {1: WALLET_A}


def test_sync_subscriptions_swaps_one_wallet_for_another():
    watcher = _watcher({WALLET_B})
    ws = FakeWs()
    sub_ids = {1: WALLET_A}
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert ws.unsubscribe_calls == [1]
    assert len(ws.subscribe_calls) == 1
    assert list(sub_ids.values()) == [WALLET_B]


def test_sync_subscriptions_reflects_live_mutation_of_watched_wallets():
    """live_config.sync_set мутирует watcher.watched_wallets IN PLACE (тот же
    объект) — эта проверка воспроизводит именно этот путь, а не пересоздание."""
    watched_wallets = {WALLET_A}
    watcher = _watcher(watched_wallets)
    ws = FakeWs()
    sub_ids: dict[int, str] = {}
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert list(sub_ids.values()) == [WALLET_A]

    watched_wallets.clear()
    watched_wallets.add(WALLET_B)
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert list(sub_ids.values()) == [WALLET_B]


if __name__ == "__main__":
    test_sync_subscriptions_adds_new_wallet()
    test_sync_subscriptions_removes_stale_wallet()
    test_sync_subscriptions_leaves_unchanged_wallet_alone()
    test_sync_subscriptions_swaps_one_wallet_for_another()
    test_sync_subscriptions_reflects_live_mutation_of_watched_wallets()
    print("ok")
