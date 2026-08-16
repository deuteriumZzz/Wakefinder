"""Веб-дашборд (FastAPI, без SPA/сборки) — визуальный слой поверх
common/wallet_stats.py, common/price_feed.py, common/metrics.py,
common/killswitch.py, common/heartbeat.py и live_state.py (баланс кошелька и
текущая оценка открытых позиций — единственная часть, которая реально ходит
в RPC, см. docstring live_state.py).

Опциональная зависимость — не часть основного пакета (`pip install -e
".[web]"`). Торговые процессы не знают о существовании этого файла и не
зависят от него.

Живой рендер: страница — статичный HTML-каркас, все данные приходят через
`/api/state` (тот же JSON, что использовало бы Telegram MiniApp или другой
клиент) и опрашиваются JS каждые 3с, перерисовывая DOM без перезагрузки
страницы — не WebSocket (не нужен broadcast нескольким клиентам, поллинг
проще и для одного локального пользователя более чем достаточен).

Запуск: uvicorn wakefinder.web:app --reload
Или как отдельное приложение: python -m wakefinder.launcher
"""

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from wakefinder.common.config import get_settings
from wakefinder.live_state import gather_state

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


@app.get("/api/state", dependencies=[Depends(_check_auth)])
async def api_state() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(await gather_state(settings))


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(_check_auth)])
async def index() -> str:
    return _PAGE


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


_PAGE = """
<html>
<head>
  <title>Wakefinder dashboard</title>
  <meta charset="utf-8">
  <style>
    :root {
      --bg: #0b0f0e; --panel: #111715; --border: #1f2b27; --text: #d7e5df; --muted: #6f8a80;
      --accent: #35d68a; --bad: #ff5d5d; --warn: #ffb454;
    }
    * { box-sizing: border-box; }
    body {
      font-family: "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
      background: var(--bg); color: var(--text); max-width: 1100px; margin: 2rem auto; padding: 0 1rem;
    }
    h1 { font-size: 1.2rem; letter-spacing: 0.05em; text-transform: uppercase; color: var(--accent); }
    h3 { font-size: 0.85rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); margin: 2rem 0 0.5rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.75rem; }
    .card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 0.9rem 1rem; }
    .card .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; }
    .card .value { font-size: 1.3rem; margin-top: 0.25rem; font-variant-numeric: tabular-nums; }
    table { border-collapse: collapse; width: 100%; margin: 0.5rem 0 1rem; overflow-x: auto; display: block; }
    th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); font-size: 0.85rem; font-variant-numeric: tabular-nums; }
    th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.04em; }
    .muted { color: var(--muted); }
    .ok { color: var(--accent); }
    .bad { color: var(--bad); font-weight: 600; }
    .warn { color: var(--warn); }
    #kill-banner { padding: 0.6rem 1rem; border-radius: 6px; margin: 0.75rem 0; font-weight: 600; }
  </style>
</head>
<body>
  <h1>Wakefinder dashboard</h1>
  <div id="kill-banner"></div>

  <div class="grid">
    <div class="card"><div class="label">ETH баланс</div><div class="value" id="eth-balance">…</div><div class="muted" id="eth-address"></div></div>
    <div class="card"><div class="label">SOL баланс</div><div class="value" id="sol-balance">…</div><div class="muted" id="sol-address"></div></div>
  </div>

  <h3>ETH copytrade — открытые позиции</h3>
  <table id="eth-copytrade-table"><thead><tr><th>Токен</th><th>Вход</th><th>Сейчас</th><th>PnL</th><th>Кошелёк</th></tr></thead><tbody></tbody></table>

  <h3>ETH snipe — открытые позиции</h3>
  <table id="eth-snipe-table"><thead><tr><th>Токен</th><th>Вход</th><th>Сейчас</th><th>PnL</th></tr></thead><tbody></tbody></table>

  <h3>Solana copytrade — открытые позиции</h3>
  <table id="sol-copytrade-table"><thead><tr><th>Токен</th><th>Вход</th><th>Сейчас</th><th>PnL</th><th>Кошелёк</th></tr></thead><tbody></tbody></table>

  <h3>Метрики (fill rate, точность симуляции)</h3>
  <table id="metrics-table"><thead><tr><th>Сеть</th><th>Попыток</th><th>Included</th><th>Fill rate</th><th>Avg expected</th><th>Avg realized</th><th>Точность</th></tr></thead><tbody></tbody></table>

  <h3>Heartbeat процессов</h3>
  <table id="heartbeats-table"><thead><tr><th>Процесс</th><th>Heartbeat</th></tr></thead><tbody></tbody></table>

  <h3>Статистика по watched-кошелькам</h3>
  <table id="wallet-stats-table"><thead><tr><th>Кошелёк</th><th>Сеть</th><th>Входы</th><th>Выходы</th><th>net PnL~</th><th>Win rate</th></tr></thead><tbody></tbody></table>

<script>
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function short(a) { return a ? esc(a).slice(0, 12) + "…" : ""; }
function pnlCell(pct) {
  if (pct === null || pct === undefined) return '<span class="muted">—</span>';
  const cls = pct >= 0 ? "ok" : "bad";
  const sign = pct >= 0 ? "+" : "";
  return `<span class="${cls}">${sign}${pct.toFixed(1)}%</span>`;
}
function num(v, digits) { return (v === null || v === undefined) ? '<span class="muted">—</span>' : v.toFixed(digits ?? 4); }

function renderPositions(tbodyId, rows, withWallet) {
  const tbody = document.querySelector(`#${tbodyId} tbody`);
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="${withWallet ? 5 : 4}" class="muted">открытых позиций нет</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(p => `
    <tr><td>${short(p.token)}</td><td>${num(p.entry_amount_in)}</td><td>${num(p.current_value)}</td>
    <td>${pnlCell(p.pnl_pct)}</td>${withWallet ? `<td>${short(p.watched_wallet)}</td>` : ""}</tr>
  `).join("");
}

async function refresh() {
  let state;
  try {
    const resp = await fetch("/api/state");
    if (!resp.ok) return;
    state = await resp.json();
  } catch (e) { return; }

  const banner = document.getElementById("kill-banner");
  if (state.kill_switch_engaged) {
    banner.className = "bad"; banner.textContent = "KILL SWITCH ВКЛЮЧЁН — все процессы остановлены";
  } else {
    banner.className = "ok"; banner.textContent = "kill switch не активен";
  }

  document.getElementById("eth-balance").textContent = state.eth.balance !== null ? state.eth.balance.toFixed(4) + " ETH" : "—";
  document.getElementById("eth-address").textContent = short(state.eth.address);
  document.getElementById("sol-balance").textContent = state.solana.balance !== null ? state.solana.balance.toFixed(4) + " SOL" : "—";
  document.getElementById("sol-address").textContent = short(state.solana.address);

  renderPositions("eth-copytrade-table", state.eth.copytrade_positions, true);
  renderPositions("eth-snipe-table", state.eth.snipe_positions, false);
  renderPositions("sol-copytrade-table", state.solana.copytrade_positions, true);

  const metricsBody = document.querySelector("#metrics-table tbody");
  const chains = Object.keys(state.metrics);
  metricsBody.innerHTML = chains.length ? chains.sort().map(chain => {
    const m = state.metrics[chain];
    return `<tr><td>${esc(chain)}</td><td>${m.total_attempts}</td><td>${m.included}</td>
      <td>${(m.fill_rate * 100).toFixed(0)}%</td><td>${m.avg_expected_profit.toLocaleString()}</td>
      <td>${m.avg_realized_profit !== null ? m.avg_realized_profit.toLocaleString() : "—"}</td>
      <td>${m.simulation_accuracy !== null ? (m.simulation_accuracy * 100).toFixed(0) + "%" : "—"}</td></tr>`;
  }).join("") : '<tr><td colspan="7" class="muted">нет данных в trade_log</td></tr>';

  const hbBody = document.querySelector("#heartbeats-table tbody");
  hbBody.innerHTML = state.heartbeats.map(h => `
    <tr><td>${esc(h.process)}</td><td class="${h.stale ? 'bad' : 'ok'}">${h.age_seconds !== null ? h.age_seconds.toFixed(0) + "с назад" : "нет данных"}</td></tr>
  `).join("");

  const wsBody = document.querySelector("#wallet-stats-table tbody");
  wsBody.innerHTML = state.wallet_stats.length ? state.wallet_stats.map(s => `
    <tr><td>${short(s.wallet)}</td><td>${esc(s.chain)}</td><td>${s.entries}</td><td>${s.exits}</td>
    <td>${num(s.net_pnl_estimate)}${s.net_pnl_usd !== null ? ` (~$${s.net_pnl_usd.toFixed(2)})` : ""}</td>
    <td>${(s.win_rate * 100).toFixed(0)}%</td></tr>
  `).join("") : '<tr><td colspan="6" class="muted">нет данных в trade_log</td></tr>';
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
