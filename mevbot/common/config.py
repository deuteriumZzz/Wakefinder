from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    eth_rpc_ws_url: str = ""
    eth_private_key: SecretStr = SecretStr("")
    flashbots_signer_key: SecretStr = SecretStr("")

    max_gas_gwei: float = 50
    max_capital_per_bundle_eth: float = 0.05


settings = Settings()
