"""Регрессия на реальный баг: wakefinder.chains.solana.mint_watcher импортировал
NewMint из common/interfaces.py, а класс там не был определён — ImportError
делал весь chains/solana/snipe.py (и, соответственно, солана-снайпинг целиком)
неимпортируемым. Ни один существующий тест это не поймал, потому что все тесты
на solana-снайпинг мокали snipe_filter.py напрямую, не проходя через реальную
цепочку импортов run() -> mint_watcher -> interfaces. Этот тест — не логика,
а именно "весь пакет вообще импортируется", тот класс проверки, которого не
хватало."""

import importlib
import pkgutil
import os

os.environ.setdefault("ETH_RPC_WS_URL", "wss://example/ws")
os.environ.setdefault("ETH_RPC_HTTP_URL", "https://example/http")
os.environ.setdefault("ETH_PRIVATE_KEY", "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004")
os.environ.setdefault("FLASHBOTS_SIGNER_KEY", "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99")

import wakefinder  # noqa: E402


def test_every_wakefinder_module_imports_without_error():
    failed = []
    for module_info in pkgutil.walk_packages(wakefinder.__path__, prefix="wakefinder."):
        try:
            importlib.import_module(module_info.name)
        except Exception as exc:
            failed.append(f"{module_info.name}: {type(exc).__name__}: {exc}")
    assert not failed, "модули не импортируются:\n" + "\n".join(failed)


if __name__ == "__main__":
    test_every_wakefinder_module_imports_without_error()
    print("ok")
