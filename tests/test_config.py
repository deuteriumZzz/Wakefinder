import pytest
from pydantic import ValidationError

from wakefinder.common.config import Settings

KEY_A = "0x7413d6e6fe53a10645335f03b3fae74eaff8a21e65a0e7cbedcd53e8c1951004"
KEY_B = "0xdb4622826f6ff3c67bac64e5417152afc8bd6a58a28318fd3be75a3d6c6d6e99"
VALID_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"


def _settings(**overrides):
    defaults = dict(
        eth_rpc_ws_url="wss://example/ws",
        eth_rpc_http_url="https://example/http",
        eth_private_key=KEY_A,
        flashbots_signer_key=KEY_B,
        eth_router_address=VALID_ROUTER,
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_distinct_keys_allowed():
    s = _settings()
    assert s.eth_private_key.get_secret_value() == KEY_A


def test_same_key_for_both_wallets_is_rejected():
    with pytest.raises(ValidationError, match="один и тот же кошелёк"):
        _settings(flashbots_signer_key=KEY_A)


def test_unknown_router_is_rejected():
    with pytest.raises(ValidationError, match="KNOWN_ROUTERS"):
        _settings(eth_router_address="0x0000000000000000000000000000000000000000")


if __name__ == "__main__":
    test_distinct_keys_allowed()
    test_same_key_for_both_wallets_is_rejected()
    test_unknown_router_is_rejected()
    print("ok")
