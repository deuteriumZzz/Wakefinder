from functools import lru_cache

from eth_account import Account
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Routers this bot is allowed to point at. eth_router_address is still
# env-configurable (different networks/forks need different addresses), but an
# arbitrary env override — e.g. from a typo or a compromised .env — must not
# silently point fund-moving transactions at an unknown contract.
KNOWN_ROUTERS = {
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D".lower(),  # Uniswap V2 Router02 (mainnet)
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F".lower(),  # Sushiswap Router (mainnet)
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Separate WS/HTTP URLs (not derived from one another) — providers use
    # incompatible path conventions (Infura /ws/v3/KEY vs /v3/KEY, Alchemy same
    # path different scheme, QuickNode different subdomains), so string-munging
    # one into the other silently breaks depending on provider. SecretStr on
    # both: the URL embeds the API key, and w3 puts endpoint URIs into exception
    # messages/repr — an uncaught traceback must not leak it to stdout/logs.
    eth_rpc_ws_url: SecretStr
    eth_rpc_http_url: SecretStr
    eth_router_address: str = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"  # Uniswap V2 Router02
    eth_private_key: SecretStr
    flashbots_signer_key: SecretStr

    max_gas_gwei: float = 50
    max_capital_per_bundle_eth: float = 0.05

    @model_validator(mode="after")
    def _check_router_allowlisted(self) -> "Settings":
        if self.eth_router_address.lower() not in KNOWN_ROUTERS:
            raise ValueError(
                f"eth_router_address {self.eth_router_address} is not in KNOWN_ROUTERS — "
                "add it deliberately in mevbot/common/config.py if this is intentional"
            )
        return self

    @model_validator(mode="after")
    def _check_keys_are_distinct_wallets(self) -> "Settings":
        exec_address = Account.from_key(self.eth_private_key.get_secret_value()).address
        signer_address = Account.from_key(self.flashbots_signer_key.get_secret_value()).address
        if exec_address == signer_address:
            raise ValueError(
                "eth_private_key and flashbots_signer_key resolve to the same wallet — "
                "these must be different keys. flashbots_signer_key only signs bundle "
                "submissions to build relay reputation; it must never hold funds."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    # Lazy + cached: constructing Settings() at import time would make every
    # module that touches config unimportable (including for tests) without a
    # real .env — required secrets should fail at first use, not at import.
    return Settings()
