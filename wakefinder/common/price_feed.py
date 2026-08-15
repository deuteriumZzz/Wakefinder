"""USD-цены ETH/SOL через публичный CoinGecko endpoint (без API-ключа) —
ТОЛЬКО для дашборда/отчётности, не участвует в торговой логике (drawdown/PnL
внутри бота остаются в нативных единицах сети, см. docstring drawdown.py:
конвертация добавила бы внешнюю зависимость на горячий путь ради цифры,
которая там не нужна для принятия решений).

ponytail: сырой HTTP-запрос (`requests` уже транзитивная зависимость, см.
alerts.py) вместо SDK — один эндпоинт, SDK не нужен. Сбой запроса -> {}, не
исключение: дашборд должен показать нативные единицы, а не упасть, если
CoinGecko недоступен/rate-limit'ит.
"""

import logging

import requests

logger = logging.getLogger("wakefinder.price_feed")

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
COIN_IDS = {"eth": "ethereum", "sol": "solana"}


def fetch_usd_prices(chains: tuple[str, ...] = ("eth", "sol")) -> dict[str, float]:
    """Возвращает {chain: usd_price} для запрошенных сетей. Сеть, для которой
    не удалось получить цену, просто отсутствует в результате — не 0.0 (0.0
    выглядело бы как валидная, но нулевая цена, а не "не знаем")."""
    ids = ",".join(COIN_IDS[c] for c in chains if c in COIN_IDS)
    if not ids:
        return {}
    try:
        resp = requests.get(COINGECKO_URL, params={"ids": ids, "vs_currencies": "usd"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("не удалось получить USD-цены с CoinGecko (%s)", type(exc).__name__)
        return {}

    prices: dict[str, float] = {}
    for chain in chains:
        coin_id = COIN_IDS.get(chain)
        if coin_id and coin_id in data and "usd" in data[coin_id]:
            prices[chain] = data[coin_id]["usd"]
    return prices
