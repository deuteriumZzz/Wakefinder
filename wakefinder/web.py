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

import json
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from wakefinder.common import killswitch
from wakefinder.common.config import get_settings
from wakefinder.live_config import load_live_config, save_live_config
from wakefinder.live_state import gather_state
from wakefinder.telegram_auth import verify_init_data

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


class LiveConfigPayload(BaseModel):
    """Валидация формы на границе API (см. wakefinder/live_config.py про то,
    почему этот файл вообще существует) — риск-параметры типизированы как
    float, чтобы битый ввод из формы/JSON-редактора отклонялся здесь, а не
    ронял apply_risk_overrides_live() в торговом процессе позже."""

    watched_wallets: list[str] = []
    token_allowlist: list[str] = []
    token_denylist: list[str] = []
    risk: dict[str, float] = {}


@app.get("/api/config", dependencies=[Depends(_check_auth)])
async def api_config_get() -> dict:
    settings = get_settings()
    return load_live_config(settings.live_config_file)


@app.post("/api/config", dependencies=[Depends(_check_auth)])
async def api_config_post(body: LiveConfigPayload) -> dict:
    settings = get_settings()
    save_live_config(settings.live_config_file, body.model_dump())
    return {"saved": True}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(_check_auth)])
async def index() -> str:
    return _PAGE


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _check_telegram_auth(x_telegram_init_data: str = Header(default="")) -> None:
    """Двойная проверка: (1) initData реально подписан Telegram для КАКОГО-ТО
    пользователя (verify_init_data — крипто-подпись, подделать без токена
    бота невозможно), (2) этот user_id — именно TELEGRAM_ALLOWED_USER_ID
    (allowlist владельца, не "любой Telegram-пользователь с валидной сессией").
    Обе проверки обязательны — initData доказывает подлинность запроса, не
    право управлять ботом."""
    settings = get_settings()
    if not settings.telegram_allowed_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Telegram MiniApp не настроен (TELEGRAM_ALLOWED_USER_ID пуст)")

    parsed = verify_init_data(x_telegram_init_data, settings.telegram_bot_token.get_secret_value())
    if parsed is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    if str(user.get("id", "")) != settings.telegram_allowed_user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


class KillSwitchAction(BaseModel):
    action: str  # "engage" | "disengage"


@app.get("/api/telegram/state", dependencies=[Depends(_check_telegram_auth)])
async def api_telegram_state() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(await gather_state(settings))


@app.post("/api/telegram/killswitch", dependencies=[Depends(_check_telegram_auth)])
async def api_telegram_killswitch(body: KillSwitchAction) -> dict:
    settings = get_settings()
    if body.action == "engage":
        killswitch.engage(settings.kill_switch_file, "engaged via Telegram MiniApp")
    elif body.action == "disengage":
        killswitch.disengage(settings.kill_switch_file)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="action должен быть 'engage' или 'disengage'")
    return {"kill_switch_engaged": killswitch.is_engaged(settings.kill_switch_file)}


@app.get("/api/telegram/config", dependencies=[Depends(_check_telegram_auth)])
async def api_telegram_config_get() -> dict:
    settings = get_settings()
    return load_live_config(settings.live_config_file)


@app.post("/api/telegram/config", dependencies=[Depends(_check_telegram_auth)])
async def api_telegram_config_post(body: LiveConfigPayload) -> dict:
    settings = get_settings()
    save_live_config(settings.live_config_file, body.model_dump())
    return {"saved": True}


@app.get("/telegram", response_class=HTMLResponse)
async def telegram_page() -> str:
    return _TELEGRAM_PAGE


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

  <h3>Живой конфиг (watched_wallets / allowlist / denylist / risk)</h3>
  <p class="muted" style="max-width:70ch;">
    Правки подхватываются торговыми процессами на следующем опросе
    (обычно в течение LIVE_CONFIG_CHECK_INTERVAL_SECONDS, по умолчанию 10с) —
    не мгновенно. Пулы арбитража здесь не редактируются (см. README).
  </p>
  <textarea id="config-editor" style="width:100%; min-height:220px; background:var(--panel); color:var(--text); border:1px solid var(--border);
    border-radius:6px; padding:0.7rem; font-family:inherit; font-size:0.85rem;"></textarea>
  <div style="margin-top:0.5rem; display:flex; gap:0.6rem; align-items:center;">
    <button id="config-save" style="padding:0.5rem 1rem; border-radius:6px; border:none; background:var(--accent); color:#06251a; font-weight:600; cursor:pointer;">Сохранить</button>
    <span id="config-status" class="muted"></span>
  </div>

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

async function loadConfig() {
  const resp = await fetch("/api/config");
  if (!resp.ok) return;
  const config = await resp.json();
  document.getElementById("config-editor").value = JSON.stringify(config, null, 2);
}

document.getElementById("config-save").addEventListener("click", async () => {
  const statusEl = document.getElementById("config-status");
  let parsed;
  try {
    parsed = JSON.parse(document.getElementById("config-editor").value);
  } catch (e) {
    statusEl.textContent = "Некорректный JSON";
    statusEl.className = "bad";
    return;
  }
  try {
    const resp = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(parsed) });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    statusEl.textContent = "Сохранено — подхватится ботом на следующем опросе";
    statusEl.className = "ok";
  } catch (e) {
    statusEl.textContent = "Не удалось сохранить";
    statusEl.className = "bad";
  }
});

loadConfig();
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""

_TELEGRAM_PAGE = """
<html>
<head>
  <title>Wakefinder</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: #0b0f0e; --panel: #111715; --border: #1f2b27; --text: #d7e5df; --muted: #6f8a80;
      --accent: #35d68a; --bad: #ff5d5d;
    }
    * { box-sizing: border-box; }
    body { font-family: "SF Mono", Menlo, Consolas, monospace; background: var(--bg); color: var(--text); margin: 0; padding: 1rem; }
    h1 { font-size: 1rem; letter-spacing: 0.05em; text-transform: uppercase; color: var(--accent); margin: 0 0 0.75rem; }
    h3 { font-size: 0.75rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); margin: 1.25rem 0 0.4rem; }
    .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 0.8rem 1rem; margin-bottom: 0.6rem; }
    .row { display: flex; justify-content: space-between; align-items: center; }
    .label { color: var(--muted); font-size: 0.7rem; text-transform: uppercase; }
    .value { font-size: 1.1rem; font-variant-numeric: tabular-nums; }
    .muted { color: var(--muted); font-size: 0.85rem; }
    .ok { color: var(--accent); }
    .bad { color: var(--bad); font-weight: 600; }
    button { width: 100%; padding: 0.7rem; border-radius: 8px; border: none; font-family: inherit; font-size: 0.85rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }
    #kill-toggle.engage { background: var(--bad); color: #fff; }
    #kill-toggle.disengage { background: var(--accent); color: #06251a; }
    .pos-row { display: flex; justify-content: space-between; font-size: 0.85rem; padding: 0.3rem 0; border-bottom: 1px solid var(--border); }
    #status { text-align: center; color: var(--muted); font-size: 0.75rem; margin-top: 1rem; }
  </style>
</head>
<body>
  <h1>Wakefinder</h1>
  <div id="kill-banner" class="card"><span class="muted">загрузка…</span></div>
  <button id="kill-toggle" disabled>…</button>

  <h3>Балансы</h3>
  <div class="card row"><span class="label">ETH</span><span class="value" id="eth-balance">…</span></div>
  <div class="card row"><span class="label">SOL</span><span class="value" id="sol-balance">…</span></div>

  <h3>Открытые позиции</h3>
  <div class="card" id="positions"><div class="muted">загрузка…</div></div>

  <h3>Живой конфиг</h3>
  <div class="muted" style="font-size:0.75rem; margin-bottom:0.4rem;">watched_wallets / allowlist / denylist / risk — подхватится ботом на следующем опросе.</div>
  <textarea id="config-editor" style="width:100%; min-height:160px; background:var(--panel); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:0.6rem; font-family:inherit; font-size:0.8rem;"></textarea>
  <button id="config-save" style="background:var(--accent); color:#06251a; margin-top:0.5rem;">Сохранить конфиг</button>

  <div id="status"></div>

<script>
const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
const initData = tg?.initData || "";

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function short(a) { return a ? esc(a).slice(0, 10) + "…" : ""; }
function pnl(pct) {
  if (pct === null || pct === undefined) return '<span class="muted">—</span>';
  const cls = pct >= 0 ? "ok" : "bad";
  return `<span class="${cls}">${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%</span>`;
}

let killEngaged = false;

async function callApi(path, opts) {
  const resp = await fetch(path, { ...opts, headers: { ...(opts?.headers || {}), "X-Telegram-Init-Data": initData } });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

function renderPositions(state) {
  const rows = [
    ...state.eth.copytrade_positions.map(p => ({ ...p, tag: "ETH copytrade" })),
    ...state.eth.snipe_positions.map(p => ({ ...p, tag: "ETH snipe" })),
    ...state.solana.copytrade_positions.map(p => ({ ...p, tag: "SOL copytrade" })),
  ];
  const el = document.getElementById("positions");
  el.innerHTML = rows.length ? rows.map(p => `
    <div class="pos-row"><span>${esc(p.tag)} · ${short(p.token)}</span><span>${pnl(p.pnl_pct)}</span></div>
  `).join("") : '<div class="muted">открытых позиций нет</div>';
}

function updateKillButton() {
  const btn = document.getElementById("kill-toggle");
  const banner = document.getElementById("kill-banner");
  btn.disabled = false;
  if (killEngaged) {
    banner.innerHTML = '<span class="bad">KILL SWITCH ВКЛЮЧЁН</span>';
    btn.textContent = "Снять kill switch";
    btn.className = "disengage";
  } else {
    banner.innerHTML = '<span class="ok">kill switch не активен</span>';
    btn.textContent = "Включить kill switch";
    btn.className = "engage";
  }
}

async function refresh() {
  try {
    const state = await callApi("/api/telegram/state");
    killEngaged = state.kill_switch_engaged;
    updateKillButton();
    document.getElementById("eth-balance").textContent = state.eth.balance !== null ? state.eth.balance.toFixed(4) + " ETH" : "—";
    document.getElementById("sol-balance").textContent = state.solana.balance !== null ? state.solana.balance.toFixed(4) + " SOL" : "—";
    renderPositions(state);
    document.getElementById("status").textContent = "";
  } catch (e) {
    document.getElementById("kill-toggle").disabled = true;
    document.getElementById("status").textContent = "Нет доступа или сбой соединения — проверьте, что открыто через кнопку бота.";
  }
}

document.getElementById("kill-toggle").addEventListener("click", async () => {
  const action = killEngaged ? "disengage" : "engage";
  try {
    const result = await callApi("/api/telegram/killswitch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });
    killEngaged = result.kill_switch_engaged;
    updateKillButton();
  } catch (e) {
    document.getElementById("status").textContent = "Не удалось изменить kill switch.";
  }
});

async function loadConfig() {
  try {
    const config = await callApi("/api/telegram/config");
    document.getElementById("config-editor").value = JSON.stringify(config, null, 2);
  } catch (e) { /* основной refresh() уже покажет статус недоступности */ }
}

document.getElementById("config-save").addEventListener("click", async () => {
  const statusEl = document.getElementById("status");
  let parsed;
  try {
    parsed = JSON.parse(document.getElementById("config-editor").value);
  } catch (e) {
    statusEl.textContent = "Некорректный JSON";
    return;
  }
  try {
    await callApi("/api/telegram/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(parsed) });
    statusEl.textContent = "Сохранено";
  } catch (e) {
    statusEl.textContent = "Не удалось сохранить конфиг";
  }
});

loadConfig();
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
