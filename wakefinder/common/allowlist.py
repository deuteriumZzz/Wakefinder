"""Allowlist токенов — не автоматический honeypot-детектор (ненадёжен без
анализа байткода/симуляции продажи для каждого нового токена), а формализация
того, что и так уже подразумевается: бот торгует только заранее сконфигурированными
парами (pool_registry/reference_pools), и это должно быть явной, проверяемой
границей, а не молчаливым побочным эффектом структуры конфига — тот же принцип,
что и KNOWN_ROUTERS в config.py."""


def validate_token_allowlist(configured_tokens: set[str], allowlist: frozenset[str]) -> None:
    if not allowlist:
        return  # пустой allowlist = проверка не запрошена (например, в тестах/деве)
    configured = {t.lower() for t in configured_tokens}
    allowed = {t.lower() for t in allowlist}
    unknown = configured - allowed
    if unknown:
        raise ValueError(
            f"токены отсутствуют в token_allowlist: {sorted(unknown)} — "
            "добавьте их явно, если торговля ими намеренна"
        )
