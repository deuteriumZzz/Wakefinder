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


def validate_not_denylisted(configured_tokens: set[str], denylist: frozenset[str]) -> None:
    """denylist — курируемый вручную список известных fee-on-transfer/rebasing
    токенов (автоматический анализ байткода ненадёжен, см. docstring модуля).
    Такие токены ломают допущение apply_swap()/get_amount_out() о сохранении
    суммы при переводе — списка по умолчанию нет (не хотим выдавать угаданные
    адреса за проверенный факт), заполняйте на вызывающей стороне по мере
    того, как узнаёте о конкретных токенах."""
    if not denylist:
        return
    configured = {t.lower() for t in configured_tokens}
    denied = {t.lower() for t in denylist}
    hit = configured & denied
    if hit:
        raise ValueError(
            f"токены в denylist (известные fee-on-transfer/rebasing): {sorted(hit)} — "
            "уберите их из pool_registry/reference_pools или из denylist, если это ошибка"
        )
