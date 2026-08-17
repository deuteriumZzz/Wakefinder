"""Суммарная экспозиция по ОДНОМУ токену через РАЗНЫЕ стратегии одной сети.

copytrade_max_total_exposure_pct/snipe_max_concurrent_positions каждая
считают лимит ТОЛЬКО в рамках своей собственной стратегии/файла позиций —
если копитрейд и снайпинг (разными путями, возможно разными кошельками)
случайно оба набрали позицию в одном и том же токене, суммарный риск на
этот токен нигде не виден и не ограничен ни одной из них по отдельности.
Rug/дамп такого токена бьёт по обеим позициям сразу.

Честная граница: копитрейд и снайпинг МОГУТ (и по умолчанию должны, см.
README "Операционные требования") использовать РАЗНЫЕ кошельки — эта
проверка НЕ про баланс одного кошелька, а про то, что один и тот же токен
оказался достаточно рискованным, чтобы бот сам, по двум независимым
сигналам, решил в него зайти дважды. Порог — абсолютный, в нативных
единицах сети, не % от чьего-то конкретного баланса (нет единого "баланса",
от которого считать процент, если кошельки разные)."""

import json
import os


def _load_position_amounts(path: str, token_field: str, amount_field: str) -> dict[str, int]:
    """token(lower) -> сумма entry-сумм всех открытых позиций в нём, из
    ОДНОГО файла позиций (несколько позиций одного токена в одном файле не
    ожидаются на практике, но суммируем на случай)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, int] = {}
    for pos in raw.values():
        token = pos.get(token_field)
        amount = pos.get(amount_field)
        if token and amount:
            key = token.lower()
            out[key] = out.get(key, 0) + amount
    return out


def total_token_exposure_eth(token: str, settings) -> int:
    """Суммарная экспозиция (wei) по token через copytrade_positions_file И
    snipe_positions_file вместе."""
    token = token.lower()
    total = _load_position_amounts(settings.copytrade_positions_file, "token", "entry_amount_in").get(token, 0)
    total += _load_position_amounts(settings.snipe_positions_file, "token", "entry_amount_in_wei").get(token, 0)
    return total


def total_token_exposure_solana(token: str, settings) -> int:
    """Суммарная экспозиция (lamports) по token/mint через
    solana_copytrade_positions_file И solana_snipe_positions_file вместе."""
    token = token.lower()
    total = _load_position_amounts(settings.solana_copytrade_positions_file, "token", "entry_amount_in").get(token, 0)
    total += _load_position_amounts(settings.solana_snipe_positions_file, "mint", "entry_amount_in").get(token, 0)
    return total
