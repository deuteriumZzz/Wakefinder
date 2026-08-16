import logging
import os
import stat
from functools import lru_cache

from eth_account import Account
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from wakefinder.common.keystore import decrypt_from_file
from wakefinder.common.killswitch import DEFAULT_PATH as DEFAULT_KILL_SWITCH_PATH

logger = logging.getLogger("wakefinder.config")

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
    # Доп. WS-провайдеры через запятую — гонка за обнаружение pending-tx
    # (см. common/race.py): несколько узлов видят мемпул с разной задержкой,
    # первый увидевший конкретный tx "выигрывает". ETH_RPC_WS_URL выше всегда
    # используется как основной (для симуляции/отправки), это — ДОПОЛНИТЕЛЬНЫЕ
    # источники именно для watcher'а. Пусто по умолчанию — гонка выключена,
    # поведение как раньше (один провайдер).
    eth_rpc_ws_urls: str = ""
    eth_router_address: str = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"  # Uniswap V2 Router02
    eth_weth_address: str = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    # Список MEV-relay через запятую — один и тот же бандл уходит параллельно
    # во все (см. docstring chains/eth/sender.py). Дефолт — только Flashbots.
    eth_relay_urls: str = "https://relay.flashbots.net"
    # API-ключи через запятую, ПОЗИЦИОННО сопоставленные с eth_relay_urls —
    # relay помимо Flashbots (bloXroute, Eden и т.п.) обычно требуют свою
    # авторизацию. Пустая позиция ("," подряд или короче списка URL) = для
    # ЭТОГО relay заголовок не добавляется (Flashbots и раньше работал без
    # него). Отправляется как `Authorization: <ключ>` — общий случай
    # bearer-токена; relay с другой схемой авторизации сюда не впишутся
    # без доработки sender.py, честно не претендуем на универсальность.
    eth_relay_api_keys: str = ""
    # Ровно один источник на ключ: plaintext ИЛИ зашифрованный файл (см.
    # common/keystore.py) — не оба и не ни одного. *_KEY_FILE + пассфраза в
    # WALLET_KEY_PASSPHRASE — альтернатива голому ключу в .env; шифрует
    # `python -m wakefinder.common.keystore <path>`.
    eth_private_key: SecretStr | None = None
    eth_private_key_file: str | None = None
    flashbots_signer_key: SecretStr | None = None
    flashbots_signer_key_file: str | None = None
    wallet_key_passphrase: SecretStr | None = None

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
    # Доп. WS-провайдеры через запятую — тот же принцип гонки, что и
    # eth_rpc_ws_urls выше (см. common/race.py). SOLANA_RPC_WS_URL остаётся
    # основным. Пусто по умолчанию — гонка выключена.
    solana_rpc_ws_urls: str = ""
    solana_rpc_http_url: SecretStr | None = None
    solana_private_key: SecretStr | None = None
    solana_private_key_file: str | None = None
    solana_wsol_address: str = "So11111111111111111111111111111111111111112"
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

    # Авто-обнаружение доп. референсных пулов той же пары на других известных
    # DEX (см. KNOWN_DEX_FACTORIES в chains/eth/simulator.py) — опционально,
    # выключено по умолчанию (лишний RPC-вызов на своп за пул). Симулятор
    # берёт максимум прибыли среди явно заданного пула и всех найденных.
    eth_auto_discover_reference_pools: bool = False

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

    # Снайпинг новых пар (chains/eth/snipe.py, common/trailing_stop.py,
    # chains/eth/snipe_filter.py) — принципиально более рискованная стратегия,
    # чем арбитраж/копитрейдинг: новый токен не имеет истории, большинство
    # новых пар — rug/dead в первые минуты. Держите SNIPE_SIZE_PCT маленьким
    # и используйте canary (CANARY_START_FRACTION) на новых профилях.
    snipe_size_pct: float = Field(default=1.0, gt=0, le=100)
    # Тестовая сумма ETH для котировки/фильтра — НЕ реальный размер входа,
    # только для getAmountsOut в snipe_filter.py.
    snipe_test_amount_eth: float = 0.01
    snipe_min_liquidity_weth: float = 1.0
    snipe_trailing_stop_pct: float = Field(default=30.0, gt=0, le=100)
    snipe_trailing_stop_check_interval_seconds: float = 15
    snipe_max_concurrent_positions: int = Field(default=3, ge=1)
    # Round-trip симуляция покупки+продажи через flashbots.simulate перед
    # реальным входом (chains/eth/snipe_filter.py:check_round_trip_sellable)
    # — ловит honeypot'ы, которые check_new_pool не видит (см. её docstring).
    # Стоит дополнительных RPC-вызовов и задержки перед входом — выключайте,
    # если скорость важнее этой проверки.
    snipe_round_trip_check: bool = True
    snipe_positions_file: str = "positions_snipe.json"

    # Снайпинг на Solana (chains/solana/snipe.py, mint_watcher.py,
    # snipe_filter.py) — отдельный файл позиций от ETH-снайпинга (та же
    # причина, что у solana_copytrade_positions_file). snipe_size_pct/
    # trailing_stop_pct/check_interval/max_concurrent_positions выше —
    # общие с ETH (значения одного смысла: доля баланса, %, счётчик).
    # snipe_round_trip_check НЕ применяется здесь — Flashbots-specific.
    solana_snipe_positions_file: str = "positions_snipe_solana.json"
    solana_snipe_test_amount_sol: float = 0.05
    solana_snipe_min_liquidity_sol: float = 1.0

    # Telegram-алерты на критичные события (kill switch, стоп-лосс, серия
    # неудач). Пустые значения = алерты выключены, не обязательны.
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""  # не секрет сам по себе (не даёт доступа ни к чему без токена)

    # Telegram MiniApp (wakefinder/telegram_auth.py, /telegram в web.py) —
    # числовой Telegram user_id владельца, единственного, кому разрешено
    # управлять ботом через MiniApp. Подпись initData доказывает "запрос от
    # Telegram для этого user_id", НЕ "этому user_id можно управлять ботом" —
    # это отдельная проверка. Пусто по умолчанию = MiniApp-эндпоинты
    # отклоняют вообще все запросы (безопасный дефолт, не "открыто всем").
    telegram_allowed_user_id: str = ""

    # Живой конфиг (wakefinder/live_config.py) — файл, который дашборд/
    # MiniApp правят, а торговые процессы периодически перечитывают (тот же
    # принцип, что kill switch: единственный работающий канал между
    # разными процессами без общей памяти). "Динамически" = на следующем
    # опросе, не мгновенно в тот же тик.
    live_config_file: str = "live_config.json"
    live_config_check_interval_seconds: float = 10

    # История цены открытых позиций (common/price_history.py, для графиков
    # на дашборде) — пишется как побочный эффект уже выполняемых RPC-запросов
    # на /api/state, не отдельный источник нагрузки.
    price_history_file: str = "price_history.jsonl"

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

    # Heartbeat: каждый из 4 процессов пишет свой файл в этой директории на
    # интервале ниже — внешний мониторинг (cron/systemd) проверяет свежесть
    # через `python -m wakefinder.common.heartbeat <path> <max_age_seconds>`.
    # Ловит тихое зависание event loop, которое не даёт исключения (поэтому
    # его не ловит ни kill switch, ни with_reconnect).
    heartbeat_dir: str = "."
    heartbeat_interval_seconds: float = 30

    # HTTP Basic auth для веб-дашборда (wakefinder/web.py). Обязаны быть
    # заданы ВМЕСТЕ — иначе дашборд отдаётся без аутентификации и громко
    # предупреждает об этом при старте (см. web.py). Дашборд показывает
    # исторические позиции/метрики/статистику по watched-кошелькам — не
    # секрет уровня приватного ключа, но открывать его анонимно за пределами
    # localhost не стоит.
    dashboard_username: str | None = None
    dashboard_password: SecretStr | None = None

    # Поэтапный ввод капитала (см. common/canary.py) — по умолчанию выключен
    # (1.0 = сразу полный размер), включается явно в профиле для нового
    # watched_wallets-набора/пары, которым ещё не доверяете на полную.
    canary_start_fraction: float = Field(default=1.0, gt=0, le=1.0)
    canary_ramp_trades: int = Field(default=20, ge=0)

    # Корректировка размера по win rate конкретного watched-кошелька (см.
    # common/position_sizing.py) — множитель вокруг 1.0 поверх copytrade_size_pct.
    copytrade_sizing_min_trades: int = Field(default=5, ge=1)
    copytrade_sizing_min_multiplier: float = Field(default=0.25, gt=0)
    copytrade_sizing_max_multiplier: float = Field(default=1.5, gt=0)

    @model_validator(mode="after")
    def _check_router_allowlisted(self) -> "Settings":
        if self.eth_router_address.lower() not in KNOWN_ROUTERS:
            raise ValueError(
                f"eth_router_address {self.eth_router_address} отсутствует в KNOWN_ROUTERS — "
                "добавьте его осознанно в wakefinder/common/config.py, если это намеренно"
            )
        return self

    @model_validator(mode="after")
    def _check_key_sources(self) -> "Settings":
        if (self.eth_private_key_file or self.flashbots_signer_key_file or self.solana_private_key_file) and not self.wallet_key_passphrase:
            raise ValueError("WALLET_KEY_PASSPHRASE обязателен, если используется любой *_KEY_FILE")
        if bool(self.eth_private_key) == bool(self.eth_private_key_file):
            raise ValueError("укажите ровно один источник ключа: ETH_PRIVATE_KEY или ETH_PRIVATE_KEY_FILE")
        if bool(self.flashbots_signer_key) == bool(self.flashbots_signer_key_file):
            raise ValueError("укажите ровно один источник ключа: FLASHBOTS_SIGNER_KEY или FLASHBOTS_SIGNER_KEY_FILE")
        if self.solana_private_key and self.solana_private_key_file:
            raise ValueError("укажите не более одного источника ключа: SOLANA_PRIVATE_KEY или SOLANA_PRIVATE_KEY_FILE")
        return self

    def resolved_eth_private_key(self) -> str:
        if self.eth_private_key_file:
            return decrypt_from_file(self.eth_private_key_file, self.wallet_key_passphrase.get_secret_value())
        return self.eth_private_key.get_secret_value()

    def resolved_flashbots_signer_key(self) -> str:
        if self.flashbots_signer_key_file:
            return decrypt_from_file(self.flashbots_signer_key_file, self.wallet_key_passphrase.get_secret_value())
        return self.flashbots_signer_key.get_secret_value()

    def resolved_solana_private_key(self) -> str | None:
        if self.solana_private_key_file:
            return decrypt_from_file(self.solana_private_key_file, self.wallet_key_passphrase.get_secret_value())
        return self.solana_private_key.get_secret_value() if self.solana_private_key else None

    @model_validator(mode="after")
    def _check_keys_are_distinct_wallets(self) -> "Settings":
        exec_address = Account.from_key(self.resolved_eth_private_key()).address
        signer_address = Account.from_key(self.resolved_flashbots_signer_key()).address
        if exec_address == signer_address:
            raise ValueError(
                "eth_private_key и flashbots_signer_key указывают на один и тот же кошелёк — "
                "это должны быть разные ключи. flashbots_signer_key только подписывает отправку "
                "бандлов для репутации у relay; на нём никогда не должно быть средств."
            )
        return self


def _warn_if_permissive(path: str | None, label: str) -> None:
    """Предупреждает, если файл с секретами читаем/писуем группой или всеми —
    частая причина утечки на shared-серверах. Не блокирует старт (не всегда
    фатально — например, в контейнере под конкретным пользователем), но
    предупреждение должно быть заметным, не тихим. os.name != "posix"
    пропускается — Windows ACL не сводится к этим битам."""
    if not path or os.name != "posix" or not os.path.exists(path):
        return
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "%s (%s) доступен на чтение/запись группе или всем (режим %o) — "
            "выполните `chmod 600 %s`, файл содержит секреты",
            label, path, mode, path,
        )


@lru_cache
def get_settings() -> Settings:
    # Ленивая инициализация + кэш: создание Settings() на этапе импорта сделало
    # бы неимпортируемым (в том числе для тестов) любой модуль, касающийся
    # конфига, без реального .env — обязательные секреты должны падать при
    # первом использовании, а не при импорте.
    settings = Settings()
    _warn_if_permissive(".env", ".env")
    _warn_if_permissive(settings.eth_private_key_file, "ETH_PRIVATE_KEY_FILE")
    _warn_if_permissive(settings.flashbots_signer_key_file, "FLASHBOTS_SIGNER_KEY_FILE")
    _warn_if_permissive(settings.solana_private_key_file, "SOLANA_PRIVATE_KEY_FILE")
    return settings
