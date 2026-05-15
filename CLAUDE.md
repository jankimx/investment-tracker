# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

A two-tier app for tracking a personal stock portfolio and running on-demand fundamental analysis on individual stocks.

- **`backend/`** — Flask + MongoDB API. Single-process, no auth beyond a shared `APP_PASSWORD`. Deploys to Railway via `Procfile`/`railway.toml` (gunicorn).
- **`frontend/`** — Static `index.html` + `app.js` + `style.css`. No build step, no framework. Talks to the backend via `fetch`. The API base URL is hard-coded at `frontend/app.js:2` (Railway URL); change it for local dev.

There is no test suite, lint config, or package manager for the frontend.

### Backend module layout

- **`app.py`** — All Flask routes, MongoDB collections (`entries`, `holdings`, `transactions`, `meta`, `analyses`), and the APScheduler job that auto-refreshes prices Mon–Fri at 16:00 ET. Entry-point endpoints: `/auth`, `/holdings`, `/transactions`, `/entries`, `/summary`, `/refresh-prices`, `/refresh-status`, `/analyze/<symbol>`, `/analyze/<symbol>/question`, `/recent-analyses`, `/track-record`, `/health`.
- **`analyzer.py`** — Pure-math scoring engine. Fetches ~10 endpoints from Financial Modeling Prep in parallel (`fetch_all_data`), then computes Quality (5 components, 100 pts), Value (3 components, 100 pts), value-trap signals, red flags, and insider activity. **No AI here.** Public entry: `analyze_stock(symbol)`.
- **`claude_synthesis.py`** — Calls the Anthropic API (`claude-sonnet-4-6`, hardcoded at `claude_synthesis.py:12`) to turn scores into plain-English prose. The main `synthesize_full_report` builds a single prompt that asks Claude to emit `SECTION_X` / `END_X` separators which are then parsed with regex. **Claude is told to use only the numbers passed in the prompt** — never invent figures.

### Two key data flows

1. **Portfolio refresh** — `do_refresh()` in `app.py` collects every distinct symbol from transactions + balances and fetches them in **one batched FMP `/quote` call** (`fetch_prices_batch`), then upserts a `prices` row keyed by `(symbol, date)` — the same daily row is overwritten on every intraday refresh. Cron runs **every minute Mon-Fri 9:30am-4:00pm ET**; SMS via Resend fires **only on the 16:00 ET close tick** (intraday + manual refreshes are silent). Also triggered by `POST /refresh-prices`.

2. **Stock analysis** — `GET /analyze/<symbol>` returns a cached report from `analyses` if one is < 24 h old; otherwise runs `analyze_stock` (FMP fetch + scoring) → `synthesize_full_report` (Claude prose) and stores the combined result. Forces a refresh with `?refresh=true`.

### Conventions worth knowing

- MongoDB `_id` is renamed to `id` on the way out via `serialize()` — don't expose raw `ObjectId`.
- Entries are uniquely keyed by `(date, platform, stock)` and upserted; the same stock on different platforms is intentionally tracked as separate positions.
- Cost basis on a `buy` transaction is recomputed as a share-weighted average; `sell` keeps the existing cost basis.
- Dates are stored as `YYYY-MM-DD` strings (UTC); refresh timestamps are ISO 8601.

### Frontend conventions

- Single-page app with tabs (`switchTab` in `app.js`); state lives in a global `state` object. Auth is a `sessionStorage` flag — clear it to log out.
- All currency formatting goes through `fmt`/`fmtDec`; gain/loss coloring through `fmtGain` (returns `{ dollar, pct, cls }`).
- Charts use Chart.js loaded from CDN — no bundler.
- Color/badge assignment for platforms and stocks is deterministic from a string hash (`badgeClass`, `colorFor`) so the same stock always renders the same color.

## Common commands

### Backend (run from `backend/`)

```bash
pip install -r requirements.txt
python app.py                 # dev server on :5000
./start.sh                    # interactive bootstrap (creates .env, installs, starts)
gunicorn app:app --bind 0.0.0.0:$PORT   # what Railway runs
```

Required env vars (see `.env.example`): `MONGO_URI`, `DB_NAME`, `APP_PASSWORD` (the server **refuses to start** if `APP_PASSWORD` is unset — there is no default). Optional but feature-gating: `FMP_API_KEY` (price refresh + stock analyzer), `ANTHROPIC_API_KEY` (Claude synthesis), `RESEND_API_KEY` + `NOTIFY_VERIZON`/`NOTIFY_ATT` (SMS), `APP_URL` (link in SMS). `GET /health` reports which keys are configured.

### Auth model

`POST /auth` issues a random session token (stored in the `sessions` collection with a TTL index, default 30 days). All data routes are decorated with `@require_auth` and reject requests without `Authorization: Bearer <token>`. The frontend keeps the token in `sessionStorage.token` and sends it on every request; on a 401 it drops back to login. `POST /auth/logout` revokes the token server-side.

### Refresh model

`POST /refresh-prices` returns 202 immediately and runs the refresh in a background thread (a per-process `threading.Lock` blocks double-invocations). Each gunicorn worker imports the module and starts its own `BackgroundScheduler`; to avoid duplicate cron firings, `_scheduled_refresh` does a unique `meta.insert_one({_id: "refresh_tick_<ts>"})` first — only the worker that wins the insert runs the refresh.

### Frontend

No build. Open `frontend/index.html` directly in a browser, or serve the directory with any static server. To test against a local backend, edit the `API` constant at `frontend/app.js:2`.
