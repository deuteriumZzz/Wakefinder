"""Тест RaydiumVaultWatcher._sync_subscriptions/_vault_index — до/отписка под
live-конфиг (self.pools) без реконнекта. Полный watch() (реальный WebSocket-
протокол) не мокается целиком — фокус на новой логике синхронизации подписок
(тот же подход, что tests/test_solana_wallet_watcher.py для WalletSwapWatcher)."""

import asyncio

from solders.keypair import Keypair

from wakefinder.chains.solana.watcher import RaydiumVaultWatcher

# Pubkey.from_string() внутри _sync_subscriptions требует настоящий валидный
# base58 — генерируем реальные (но случайные) ключи вместо строковых заглушек.
POOL1_BASE = str(Keypair().pubkey())
POOL1_QUOTE = str(Keypair().pubkey())
POOL2_BASE = str(Keypair().pubkey())
POOL2_QUOTE = str(Keypair().pubkey())


class FakeWs:
    def __init__(self):
        self._next_sub_id = 1000  # выше любых вручную заданных id в тестах ниже
        self.subscribe_calls = []
        self.unsubscribe_calls = []

    async def account_subscribe(self, pubkey, encoding=None):
        self.subscribe_calls.append(str(pubkey))

    async def recv(self):
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        return [type("Confirmation", (), {"result": sub_id})()]

    async def account_unsubscribe(self, subscription):
        self.unsubscribe_calls.append(subscription)


def _pool1_cfg():
    return {"base_vault": POOL1_BASE, "quote_vault": POOL1_QUOTE, "base_mint": "MINT_A", "quote_mint": "MINT_B"}


def _pool2_cfg():
    return {"base_vault": POOL2_BASE, "quote_vault": POOL2_QUOTE, "base_mint": "MINT_C", "quote_mint": "MINT_D"}


def test_vault_index_covers_both_sides_of_each_pool():
    watcher = RaydiumVaultWatcher("wss://example", {"pool1": _pool1_cfg()}, min_amount_in=0)
    index = watcher._vault_index()
    assert index[POOL1_BASE] == ("pool1", "base")
    assert index[POOL1_QUOTE] == ("pool1", "quote")


def test_sync_subscriptions_subscribes_all_vaults_of_new_pool():
    watcher = RaydiumVaultWatcher("wss://example", {"pool1": _pool1_cfg()}, min_amount_in=0)
    ws = FakeWs()
    sub_ids: dict[int, str] = {}
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert len(ws.subscribe_calls) == 2
    assert set(sub_ids.values()) == {POOL1_BASE, POOL1_QUOTE}
    assert ws.unsubscribe_calls == []


def test_sync_subscriptions_removes_vaults_of_dropped_pool():
    pools = {"pool1": _pool1_cfg()}
    watcher = RaydiumVaultWatcher("wss://example", pools, min_amount_in=0)
    ws = FakeWs()
    sub_ids = {1: POOL1_BASE, 2: POOL1_QUOTE}
    pools.clear()  # пул удалён из live-конфига
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert set(ws.unsubscribe_calls) == {1, 2}
    assert sub_ids == {}


def test_sync_subscriptions_clears_last_balance_for_removed_vault():
    pools = {"pool1": _pool1_cfg()}
    watcher = RaydiumVaultWatcher("wss://example", pools, min_amount_in=0)
    watcher._last_balance[POOL1_BASE] = 12345
    ws = FakeWs()
    sub_ids = {1: POOL1_BASE, 2: POOL1_QUOTE}
    pools.clear()
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert POOL1_BASE not in watcher._last_balance


def test_sync_subscriptions_reflects_live_mutation_of_pools():
    """live_config.sync_dict мутирует watcher.pools IN PLACE (тот же объект)
    — эта проверка воспроизводит именно этот путь, а не пересоздание."""
    pools = {"pool1": _pool1_cfg()}
    watcher = RaydiumVaultWatcher("wss://example", pools, min_amount_in=0)
    ws = FakeWs()
    sub_ids: dict[int, str] = {}
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert set(sub_ids.values()) == {POOL1_BASE, POOL1_QUOTE}

    pools.clear()
    pools["pool2"] = _pool2_cfg()
    asyncio.run(watcher._sync_subscriptions(ws, sub_ids))
    assert set(sub_ids.values()) == {POOL2_BASE, POOL2_QUOTE}


if __name__ == "__main__":
    test_vault_index_covers_both_sides_of_each_pool()
    test_sync_subscriptions_subscribes_all_vaults_of_new_pool()
    test_sync_subscriptions_removes_vaults_of_dropped_pool()
    test_sync_subscriptions_clears_last_balance_for_removed_vault()
    test_sync_subscriptions_reflects_live_mutation_of_pools()
    print("ok")
