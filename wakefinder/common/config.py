from functools import lru_cache

from eth_account import Account
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Роутеры, на которые боту разрешено указывать. eth_router_address всё равно
# настраивается через env (разным сетям/форкам нужны разные адреса), но
# произвольное переопределение через env — например, опечатка или скомпрометированный
# .env — не должно молча направлять транзакции, двигающие деньги, на неизвестный контракт.
KNOWN_ROUTERS = {
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D".lower(),  # Uniswap V2 Router02 (mainnet)
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F".lower(),  # Sushiswap Router (mainnet)
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Отдельные WS/HTTP URL (не выводятся друг из друга) — у провайдеров
    # несовместимые схемы путей (Infura /ws/v3/KEY против /v3/KEY, у Alchemy тот же
    # путь но другая схема, у QuickNode вообще другие поддомены), так что
    # текстовое преобразование одного в другой молча ломается в зависимости от
    # провайдера. Оба поля — SecretStr: URL содержит API-ключ, а w3 подставляет
    # endpoint URI в сообщения исключений/repr — необработанный traceback не
    # должен слить его в stdout/логи.
    eth_rpc_ws_url: SecretStr
    eth_rpc_http_url: SecretStr
    eth_router_address: str = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"  # Uniswap V2 Router02
    eth_private_key: SecretStr
    flashbots_signer_key: SecretStr

    max_gas_gwei: float = 50
    max_capital_per_bundle_eth: float = 0.05

    # Файл-сигнал: если он существует, бот останавливается перед следующим
    # действием. `touch` — чтобы остановить без передеплоя; удалить — чтобы
    # продолжить.
    kill_switch_file: str = ".kill"

    # Доля чистой прибыли, которую ставим сверху как priority fee, чтобы
    # конкурировать в аукционе block builder'а за включение — билдеры
    # сортируют бандлы по суммарной ценности (priority fee + любые явные
    # переводы), так что бандл, не предлагающий цену близкую к своей реальной
    # выгоде, обычно проигрывает тому, кто предлагает.
    profit_share_bps: int = Field(default=9000, ge=0, le=10_000)

    @model_validator(mode="after")
    def _check_router_allowlisted(self) -> "Settings":
        if self.eth_router_address.lower() not in KNOWN_ROUTERS:
            raise ValueError(
                f"eth_router_address {self.eth_router_address} отсутствует в KNOWN_ROUTERS — "
                "добавьте его осознанно в wakefinder/common/config.py, если это намеренно"
            )
        return self

    @model_validator(mode="after")
    def _check_keys_are_distinct_wallets(self) -> "Settings":
        exec_address = Account.from_key(self.eth_private_key.get_secret_value()).address
        signer_address = Account.from_key(self.flashbots_signer_key.get_secret_value()).address
        if exec_address == signer_address:
            raise ValueError(
                "eth_private_key и flashbots_signer_key указывают на один и тот же кошелёк — "
                "это должны быть разные ключи. flashbots_signer_key только подписывает отправку "
                "бандлов для репутации у relay; на нём никогда не должно быть средств."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    # Ленивая инициализация + кэш: создание Settings() на этапе импорта сделало
    # бы неимпортируемым (в том числе для тестов) любой модуль, касающийся
    # конфига, без реального .env — обязательные секреты должны падать при
    # первом использовании, а не при импорте.
    return Settings()
