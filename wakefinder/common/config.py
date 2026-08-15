from functools import lru_cache

from eth_account import Account
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wakefinder.common.killswitch import DEFAULT_PATH as DEFAULT_KILL_SWITCH_PATH

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
    # действием. Единый на все 4 процесса (ETH/Solana × arb/copytrade) —
    # дефолт АБСОЛЮТНЫЙ (см. killswitch.py), чтобы не зависеть от рабочей
    # директории каждого процесса. `python -m wakefinder.common.killswitch
    # stop|resume|status` — операционный CLI, не "не забудьте touch .kill".
    kill_switch_file: str = DEFAULT_KILL_SWITCH_PATH

    # Доля чистой прибыли, которую ставим сверху как priority fee (ETH) или
    # Jito-tip (Solana), чтобы конкурировать за включение — билдеры сортируют
    # бандлы по суммарной ценности, так что бандл, не предлагающий цену близкую
    # к своей реальной выгоде, обычно проигрывает тому, кто предлагает.
    profit_share_bps: int = Field(default=9000, ge=0, le=10_000)

    # Solana — опционально: можно гонять только ETH-путь без этих полей.
    # solana_private_key — base58, отдельное keyspace от ETH-ключей, не
    # взаимозаменяемы в принципе (не тот же кошелёк технически невозможен).
    solana_rpc_ws_url: SecretStr | None = None
    solana_rpc_http_url: SecretStr | None = None
    solana_private_key: SecretStr | None = None
    jito_block_engine_url: str = "https://mainnet.block-engine.jito.wtf/api/v1"
    max_capital_per_bundle_sol: float = 0.5

    # Автостоп: после стольких подряд неотправленных/непопавших в блок бандлов
    # бот сам ставит kill switch и останавливается — не столько защита от
    # прямой потери денег (неотправленный бандл ничего не стоит), сколько
    # сигнал "что-то системно не так" (логическая ошибка, вечный проигрыш
    # аукциона), которую нельзя оставлять человеку заметить самому.
    max_consecutive_failures: int = 5

    # Append-only лог каждой попытки (даже неприбыльной — на будущее для
    # анализа) — не полноценный PnL-дашборд, а сырые данные для него.
    trade_log_file: str = "trades.jsonl"

    # Factory нужна только копитрейдингу — арбитраж работает по заранее
    # заданному pool_registry, а копитрейдинг обязан следовать за watchlist-
    # кошельком в токены, которые никто заранее не регистрировал.
    eth_factory_address: str = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"  # Uniswap V2 Factory

    # Копитрейдинг: доля ТЕКУЩЕГО баланса нашего кошелька (не сумма кита!) на
    # каждый вход — киты бывают с бюджетом в миллионы, мы физически не можем
    # и не должны повторять их абсолютный размер сделки, только направление.
    copytrade_size_pct: float = Field(default=2.0, gt=0, le=100)
    copytrade_stop_loss_pct: float = Field(default=20.0, gt=0, le=100)
    copytrade_stop_loss_check_interval_seconds: int = 60
    copytrade_positions_file: str = "positions.json"
    solana_copytrade_positions_file: str = "positions_solana.json"

    # Консенсус: вход только если >= N разных watched-кошельков купили один и
    # тот же токен в течение окна — один кит может ошибаться, несколько
    # независимых китов, сходящихся почти одновременно, сильнее как сигнал.
    copytrade_min_consensus_wallets: int = Field(default=2, ge=1)
    copytrade_consensus_window_seconds: float = 120

    # Кэп на СУММАРНУЮ экспозицию по всем открытым копитрейд-позициям сразу
    # (не только на одну сделку) — иначе несколько последовательных входов по
    # COPYTRADE_SIZE_PCT каждый могут незаметно съесть большую часть баланса.
    copytrade_max_total_exposure_pct: float = Field(default=20.0, gt=0, le=100)

    # Telegram-алерты на критичные события (kill switch, стоп-лосс, серия
    # неудач). Пустые значения = алерты выключены, не обязательны.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Портфельный circuit breaker по РЕАЛИЗОВАННОЙ просадке за скользящее
    # окно, агрегированной по всем стратегиям одной сети (arb + copytrade
    # вместе) — независим от MAX_CONSECUTIVE_FAILURES, который ловит только
    # "бандл не попадает в блок", не "стратегия работает и теряет деньги".
    # В нативных единицах сети (нет USD price feed).
    max_drawdown_eth: float = 0.5
    max_drawdown_sol: float = 5.0
    drawdown_window_seconds: float = 86_400  # 24 часа
    drawdown_check_interval_seconds: float = 300  # throttle — полное сканирование trade_log на каждой проверке

    # Минимальная ликвидность референсного пула (в единицах token_in — см.
    # ponytail-заметку в simulator.py про допущение token_in≈WETH/wSOL) —
    # тонкий референсный пул дёшево сдвинуть в том же блоке/слоте, что и
    # атакуемый, подсунув боту фиктивно прибыльную котировку. Порог не
    # ловит манипуляцию саму по себе, просто отказывается доверять пулам,
    # которые для этого достаточно дёшевы.
    min_reference_liquidity_eth: float = 1.0
    min_reference_liquidity_sol: float = 10.0

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
