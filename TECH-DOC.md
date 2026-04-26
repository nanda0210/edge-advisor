# NandaEdge Advisor — Deployment & Tech Doc

## Live URLs

| Component | URL | Host |
|---|---|---|
| **Frontend (Dashboard)** | https://nanda0210.github.io/edge-advisor/ | GitHub Pages |
| **Backend API** | https://edge-advisor-api.onrender.com | Render (free) |
| **Source repo** | https://github.com/nanda0210/edge-advisor | GitHub |

API endpoints (all accept `?symbols=NVDA,TSLA,...`):
- `GET /quotes` — realtime prices (default: watchlist + market indices)
- `GET /technicals` — EMA/RSI/MACD/BB/ATR/VWAP/S&R (cached 5 min, per symbol)
- `GET /forecast` — GBM price projections for 1w / 1mo / 3mo / 6mo / 1y / 5y (cached 1 hr, per symbol)
- `GET /options?symbol=NVDA&type=puts|calls` — live options chain candidates with Black-Scholes delta, IV, OI, mid, BE, annualized return (cached 10 min)
- `GET /history?symbols=...&days=90` — daily closes for sparklines (cached 30 min)
- `GET /feargreed` — CNN Fear & Greed (cached 10 min)

---

## Architecture

```
Browser
   │
   ▼
GitHub Pages  ─── static index.html ───►  detects hostname
   │                                          │
   │                                          ├─ localhost → http://localhost:8765
   │                                          └─ github.io → https://edge-advisor-api.onrender.com
   ▼
Render (Python web service)
   │
   └─► server.py
         ├─ yfinance → Yahoo Finance
         └─ urllib  → CNN Fear & Greed
```

The frontend is **fully static**. It auto-detects whether it's served locally or from `github.io` and switches `API_BASE` accordingly (see `index.html` near `const API_BASE`).

---

## Files

| File | Purpose |
|---|---|
| `index.html` | Self-contained dashboard (HTML + CSS + JS, no build step) |
| `server.py` | Python HTTP server — quotes, technicals, fear/greed |
| `requirements.txt` | yfinance, pandas, numpy |
| `render.yaml` | Render Blueprint config (free plan, Python 3.11) |
| `.gitignore` | __pycache__, *.pyc, .DS_Store |

---

## How deployment works

### Frontend → GitHub Pages
- **Source**: `main` branch, root folder
- **Trigger**: every `git push` rebuilds in ~30s
- **No build step**: `index.html` is served as-is
- Configured at: Repo → Settings → Pages

### Backend → Render
- **Trigger**: every `git push` to `main` auto-redeploys (~3–5 min)
- **Build**: `pip install -r requirements.txt`
- **Start**: `python server.py`
- **Env vars set by Render**:
  - `PORT` — Render assigns; `server.py` reads it
  - `PYTHON_VERSION=3.11.9` — pinned in `render.yaml`
- `server.py` binds `0.0.0.0` when `PORT` is set, else `localhost` (local dev)

### CORS
`server.py` sends `Access-Control-Allow-Origin: *` so any origin (including `github.io`) can call the API.

---

## Free tier behavior (Render)

- **Cold start**: service sleeps after 15 min of inactivity. First request after sleep takes **30–60s** (build container + run yfinance imports).
- **Quota**: 750 hours/month free — plenty for personal use.
- **Workaround for cold start** (optional): set up a cron-job.org or UptimeRobot ping every 10 min to keep it warm.

---

## Local development

```bash
cd /Users/rajamac/claude-projects/finance-dashboard
pip3 install -r requirements.txt
python3 server.py
# Open http://localhost:8765
```

`index.html` auto-detects `localhost` and uses `http://localhost:8765` instead of the Render URL.

---

## Common updates

### Add a new ticker
Edit `WATCH` list in `server.py:20`. Push. Render redeploys backend; Pages serves frontend (no change needed there unless you want UI tweaks).

### Change cache TTL
Edit `TECH_TTL` (line 31) or `FG_TTL` (line 32) in `server.py`.

### Repoint frontend to a different backend
Edit `API_BASE` in `index.html` (search for `// ── FETCH`). Push.

### Deploy frontend changes
```bash
git add index.html
git commit -m "your message"
git push
# Pages rebuilds in ~30s
```

### Deploy backend changes
```bash
git add server.py requirements.txt
git commit -m "your message"
git push
# Render auto-redeploys in 3–5 min
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| ⚠️ Backend unreachable | Render service sleeping | Wait 30–60s and refresh |
| Frontend loads but no data | Backend down or CORS broke | Check https://edge-advisor-api.onrender.com/quotes directly |
| `HTTP 500` from `/technicals` | yfinance rate-limited | Cache holds 5 min; try again later |
| Page 404 on github.io | Pages not enabled | Settings → Pages → Source: `main` / `/ (root)` |
| Backend 500 on Render after push | Build failed | Check Render dashboard → Logs |

---

## Security notes

- **No secrets in repo**. yfinance and CNN F&G are public/unauthenticated APIs.
- **CORS is `*`** (intentional — public read-only endpoints). If you add auth or user-specific data, restrict to `https://nanda0210.github.io`.
- **No write endpoints**. Backend is read-only.

---

## Watchlist

| Ticker | Company | Theme |
|---|---|---|
| NVDA | NVIDIA | AI infrastructure |
| TSLA | Tesla | EV / Robotics / AI |
| PLTR | Palantir | AI software / Defense |
| AMD | Advanced Micro Devices | AI accelerators |
| MU | Micron | HBM / AI memory |
| CRWD | CrowdStrike | Cybersecurity |
| INTC | Intel | Semiconductor turnaround |
| IONQ | IonQ | Quantum |
| RGTI | Rigetti | Quantum (speculative) |

Edit in `server.py:20` (`WATCH`) and `MARKETS` (line 21) for indices/commodities/crypto.

---

## Forecast model (GBM)

For each ticker the backend computes:
- **μ (drift)** — mean of daily log-returns over the last 2 years
- **σ (vol)** — std-dev of daily log-returns over the last 2 years
- **Projection** — `P_t = P_0 · exp(μt ± k·σ·√t)`
  - **median** = `P_0 · exp(μt)`
  - **P25 / P75** = ± 0.6745 σ√t (50% interval)
  - **P05 / P95** = ± 1.6449 σ√t (90% interval)
- Horizons: 1w (5d), 1mo (21d), 3mo (63d), 6mo (126d), 1y (252d), 5y (1260d)

Caveats — the model assumes returns stay log-normal and stationary. It cannot capture regime changes, earnings, macro shocks, or M&A. **The 5-year band is informational only; do not trade on it.**

---

## Authentication (password gate)

Backend gates all data endpoints behind a single password. The password itself is **never** stored or sent — only its SHA-256 hash.

**How it works:**
1. Set env var `AUTH_TOKEN_HASH` on Render = `SHA-256(password)` (lowercase hex).
2. Frontend shows a login overlay. User enters password.
3. Browser computes `SHA-256(password)` via `crypto.subtle` and sends as `Authorization: Bearer <hash>`.
4. Backend compares to `AUTH_TOKEN_HASH`. Match → 200. Else → 401.
5. On success, the hash is stored in `localStorage` (if "Remember on this device") or `sessionStorage`. Used as Bearer on every subsequent API call.

**Brute-force protection:** 10 failed attempts per IP per 15 min → 429 (rate-limited).

**Setting / changing the password:**
```bash
# Compute hash for a chosen password (replace "MyP@ss"):
printf 'MyP@ss' | shasum -a 256 | awk '{print $1}'
```
Paste the resulting hex into the Render service: **Dashboard → edge-advisor-api → Environment → Add Environment Variable** → `AUTH_TOKEN_HASH=<hex>` → Save (auto-redeploys).

**Endpoints:**
- `GET /health` — always public; returns `{ok, authRequired}` so frontend knows whether to prompt.
- `GET /auth` — returns 200 if Bearer token matches, 401 otherwise.
- All data endpoints — 401 without valid Bearer token.

**Reset / forget device:** clear browser storage, or use a private/incognito window.

**Caveats:** The dashboard HTML is still served publicly from GitHub Pages (it's a static file). Auth gates the *data*, not the *UI shell*. If you need the HTML private too, move hosting off GitHub Pages.

---

## Watchlist customization

The watchlist is user-editable from the dashboard:
- **Add**: type a symbol → click `+ Add`. Backend verifies it exists on Yahoo before saving.
- **Remove**: click `✕` on any row.
- **Reset**: restores the default 9 tickers.
- Persisted in browser `localStorage` (key: `nandaedge.watchlist.v1`) — survives reload, per-browser.

For tickers not in the static `META` table, the dashboard shows a default neutral profile while still computing live price, technicals, and forecast.

---

## Deployment history

| Date | Change |
|---|---|
| 2026-04-21 | Initial commit — NandaEdge Advisor v2.0 (local-only) |
| 2026-04-21 | README added |
| 2026-04-25 | Made backend cloud-deployable; deployed to Render + GitHub Pages |
| 2026-04-25 | Added `/forecast` (GBM) + user-editable watchlist (add/remove tickers) |
| 2026-04-25 | Added password gate (SHA-256 Bearer-token auth, rate-limited) + 60s API timeout |
| 2026-04-25 | Added mobile card layout for watchlist & forecast (<750px) |
| 2026-04-25 | Added `/options` endpoint (Black-Scholes delta, OI/spread liquidity gates) + Options Candidates UI section |
| 2026-04-25 | Added `/history` endpoint, SVG sparklines on forecast cards, and Today's Top Setups auto-ranked section |
