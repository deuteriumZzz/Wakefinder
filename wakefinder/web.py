"""Лёгкий веб-дашборд (FastAPI, серверный HTML, без SPA/сборки) — визуальный
слой поверх уже существующих данных, ничего нового не хранит:
common/wallet_stats.py, common/price_feed.py, common/metrics.py,
common/killswitch.py, common/heartbeat.py, файлы позиций.

Опциональная зависимость — не часть основного пакета (`pip install -e
".[web]"`). Торговые процессы не знают о существовании этого файла и не
зависят от него.

Запуск: uvicorn wakefinder.web:app --reload
"""

import html
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from wakefinder.common import heartbeat, killswitch
from wakefinder.common.config import get_settings
from wakefinder.common.metrics import compute_chain_metrics
from wakefinder.common.price_feed import fetch_usd_prices
from wakefinder.common.wallet_stats import compute_wallet_stats
from wakefinder.dashboard import _usd_estimate

security = HTTPBasic(auto_error=False)
logger = logging.getLogger("wakefinder.web")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    settings = get_settings()
    if not (settings.dashboard_username and settings.dashboard_password):
        logger.warning(
            "DASHBOARD_USERNAME/DASHBOARD_PASSWORD не заданы — дашборд отдаётся БЕЗ аутентификации, "
            "не открывайте порт дальше localhost"
        )
    yield


app = FastAPI(title="Wakefinder dashboard", lifespan=_lifespan)


def _check_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    settings = get_settings()
    if not (settings.dashboard_username and settings.dashboard_password):
        return  # без DASHBOARD_USERNAME/DASHBOARD_PASSWORD — дашборд открыт, см. _lifespan
    expected_password = settings.dashboard_password.get_secret_value()
    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, settings.dashboard_username)
        and secrets.compare_digest(credentials.password, expected_password)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"},
        )

HEARTBEAT_STALE_SECONDS = 90
HEARTBEAT_FILES = {
    "eth_arb": "eth_arb.heartbeat",
    "eth_copytrade": "eth_copytrade.heartbeat",
    "solana_arb": "solana_arb.heartbeat",
    "solana_copytrade": "solana_copytrade.heartbeat",
}


def _load_positions(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _render_positions_table(label: str, positions: dict) -> str:
    if not positions:
        return f"<h3>{html.escape(label)}</h3><p class='muted'>открытых позиций нет</p>"
    rows = "".join(
        f"<tr><td>{html.escape(token[:12])}...</td><td>{pos.get('entry_amount_in', '?')}</td>"
        f"<td>{html.escape(str(pos.get('watched_wallet', ''))[:12])}...</td></tr>"
        for token, pos in positions.items()
    )
    return f"""
    <h3>{html.escape(label)} ({len(positions)})</h3>
    <table><tr><th>Токен</th><th>entry_amount_in</th><th>Кошелёк</th></tr>{rows}</table>
    """


def _render_metrics_table(metrics: dict) -> str:
    if not metrics:
        return "<p class='muted'>нет данных в trade_log</p>"
    rows = ""
    for chain, m in sorted(metrics.items()):
        realized = f"{m.avg_realized_profit:,.0f}" if m.avg_realized_profit is not None else "—"
        accuracy = f"{m.simulation_accuracy:.0%}" if m.simulation_accuracy is not None else "—"
        rows += (
            f"<tr><td>{html.escape(chain)}</td><td>{m.total_attempts}</td><td>{m.included}</td>"
            f"<td>{m.fill_rate:.0%}</td><td>{m.avg_expected_profit:,.0f}</td>"
            f"<td>{realized}</td><td>{accuracy}</td></tr>"
        )
    return f"""
    <table>
      <tr><th>Сеть</th><th>Попыток</th><th>Included</th><th>Fill rate</th>
          <th>Avg expected</th><th>Avg realized</th><th>Точность симуляции</th></tr>
      {rows}
    </table>
    """


def _render_wallet_stats_table(trade_log_path: str, prices: dict) -> str:
    stats = compute_wallet_stats(trade_log_path)
    if not stats:
        return "<p class='muted'>нет данных в trade_log</p>"
    rows = ""
    for s in sorted(stats.values(), key=lambda s: s.net_pnl_estimate, reverse=True):
        usd = _usd_estimate(s.net_pnl_estimate, s.chain, prices).strip()
        rows += (
            f"<tr><td>{html.escape(s.wallet[:14])}...</td><td>{html.escape(s.chain)}</td>"
            f"<td>{s.entries}</td><td>{s.exits}</td><td>{s.net_pnl_estimate}{html.escape(usd)}</td>"
            f"<td>{s.win_rate:.0%}</td></tr>"
        )
    return f"""
    <table>
      <tr><th>Кошелёк</th><th>Сеть</th><th>Входы</th><th>Выходы</th><th>net PnL~</th><th>Win rate</th></tr>
      {rows}
    </table>
    """


def _render_heartbeats(heartbeat_dir: str) -> str:
    rows = ""
    for label, filename in HEARTBEAT_FILES.items():
        path = os.path.join(heartbeat_dir, filename)
        beat = heartbeat.last_beat(path)
        if beat is None:
            status, css = "нет данных", "muted"
        else:
            age = time.time() - beat
            stale = age > HEARTBEAT_STALE_SECONDS
            status = f"{age:.0f}с назад"
            css = "bad" if stale else "ok"
        rows += f"<tr><td>{html.escape(label)}</td><td class='{css}'>{status}</td></tr>"
    return f"<table><tr><th>Процесс</th><th>Heartbeat</th></tr>{rows}</table>"


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(_check_auth)])
async def index() -> str:
    settings = get_settings()

    kill_engaged = killswitch.is_engaged(settings.kill_switch_file)
    kill_html = (
        "<p class='bad'>KILL SWITCH ВКЛЮЧЁН — все процессы остановлены</p>"
        if kill_engaged else "<p class='ok'>kill switch не активен</p>"
    )

    prices = fetch_usd_prices()
    metrics = compute_chain_metrics(settings.trade_log_file)

    eth_positions = _load_positions(settings.copytrade_positions_file)
    sol_positions = _load_positions(settings.solana_copytrade_positions_file)

    return f"""
    <html>
    <head>
      <title>Wakefinder dashboard</title>
      <meta http-equiv="refresh" content="30">
      <style>
        body {{ font-family: -apple-system, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
        h1 {{ font-size: 1.4rem; }}
        h3 {{ font-size: 1rem; margin-top: 2rem; }}
        table {{ border-collapse: collapse; width: 100%; margin: 0.5rem 0 1.5rem; }}
        th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; font-size: 0.9rem; }}
        th {{ color: #666; font-weight: 600; }}
        .muted {{ color: #888; }}
        .ok {{ color: #1a7f37; }}
        .bad {{ color: #c00; font-weight: 600; }}
      </style>
    </head>
    <body>
      <h1>Wakefinder dashboard</h1>
      {kill_html}

      <h3>Метрики (fill rate, точность симуляции)</h3>
      {_render_metrics_table(metrics)}

      <h3>Heartbeat процессов</h3>
      {_render_heartbeats(settings.heartbeat_dir)}

      {_render_positions_table("ETH copytrade — открытые позиции", eth_positions)}
      {_render_positions_table("Solana copytrade — открытые позиции", sol_positions)}

      <h3>Статистика по watched-кошелькам</h3>
      {_render_wallet_stats_table(settings.trade_log_file, prices)}
    </body>
    </html>
    """


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
