# NandaEdge Advisor — Technology Stack

> **Maintenance rule:** this document must be updated in the same commit as any
> change that adds, removes, or swaps a technology, dependency, provider,
> endpoint, or deployment target. If a feature changes the stack, it changes
> this file. See [Keeping this current](#keeping-this-current).

Read-only market-intelligence dashboard. No live trading, no broker account
access, no order routing.

---

## Architecture

Split deployment — the static frontend never holds a secret.

| Layer | Technology | Host |
|---|---|---|
| Frontend | Single-file HTML + vanilla JS | GitHub Pages |
| Backend API | Python stdlib HTTP server | Render (free tier) |
| Persistence | JSON file on local disk | Render instance |
| OAuth callback | Static HTML page, token-free | GitHub Pages |

```
Browser ──> GitHub Pages (index.html)
               │  fetch + Bearer token
               ▼
          Render (server.py) ──> Yahoo / Schwab / Tradier / CNN
               │
               ▼
          daytrade_history.json
```

## Backend

- **Python 3.11.9** (pinned via `PYTHON_VERSION` in `render.yaml`)
- **`http.server`** — `HTTPServer` + `BaseHTTPRequestHandler`. No Flask, no
  FastAPI, no ASGI server; the whole API is one module, `server.py`.
- **Custom `ThreadedServer`** — one thread per request, bounded by a
  `threading.BoundedSemaphore` with load shedding and a socket timeout, so a
  burst or a half-open connection cannot exhaust the instance.
- **`threading`** — locks for Schwab token refresh, provider-error state, and
  prediction-history writes.
- **`logging`** — a handler plus `threading.local` buffer captures noisy
  provider output per request. Deliberately *not* `contextlib.redirect_stdout`:
  that swaps a process-global stream, so one request would swallow another's
  logs in a threaded server.
- **`zoneinfo`** — Pacific market-time handling (`America/Los_Angeles`).
- **`hmac.compare_digest`** — constant-time auth token comparison.
- **`urllib.request`** — all outbound HTTP. No `requests` dependency.
- **`gc`** — explicit collection after scan passes to stay inside the free
  tier's memory budget.

### Third-party Python packages

Only three, all in `requirements.txt`:

| Package | Used for |
|---|---|
| `yfinance` | Quotes, OHLCV history, intraday candles, options fallback |
| `pandas` | Candle DataFrames, time-indexed filtering, resampling |
| `numpy` | Technical indicators and the GBM forecast model |

## Frontend

- **Vanilla JavaScript**, no framework and no build step
- **Zero external requests** — no CDN scripts, stylesheets, fonts, or images.
  Everything ships inside `index.html` (~3.3k lines)
- **`fetch`** against the Render API with a `Authorization: Bearer` header
- **`crypto.subtle`** — SHA-256 of the password computed in the browser; the
  password itself is never stored or transmitted
- **`localStorage` / `sessionStorage`** — session persistence, depending on
  whether "Remember on this device" is chosen
- **Inline SVG** for iconography; **`Intl`** for number/date formatting
- **CSS custom properties** for theming

## Data providers

| Provider | Scope | Priority |
|---|---|---|
| Yahoo Finance (via `yfinance`) | Quotes, history, technicals, forecast, candles | Primary for everything except options chains |
| Schwab Market Data | Options chains **only** | First choice when configured |
| Tradier | Options chains | Second choice when configured |
| Yahoo options | Options chains | Last-resort fallback |
| CNN | Fear & Greed index | Single purpose |

Every options response reports which provider served it, plus
`providerWarnings` when one was skipped or failed.

## Authentication and secrets

- **Shared-password gate.** `AUTH_TOKEN_HASH` holds `SHA-256(password)`; the
  browser sends the hash as a Bearer token. The plaintext password exists only
  in the user's head.
- **Per-IP rate limiting** — 10 failures per 15 minutes returns `429`.
- **`/health` is public** (so the frontend can discover whether auth is on);
  every other endpoint is gated.
- **Schwab OAuth** — authorization-code flow, run externally via
  `scripts/schwab_oauth_tokens.py`. Tokens live in Render env vars or a local
  gitignored `.env`; access tokens may be refreshed in memory but are never
  written to source, logs, or browser storage. No account scopes are requested.

## Persistence

- **A single JSON file**, `daytrade_history.json` — no database, no ORM.
- **Atomic writes** — temp file + `fsync` + `os.replace`, serialized under a
  lock, so a crash or two concurrent scans cannot truncate it or drop a day.
- **Two retention tiers**: raw predictions for 7 days (matching Yahoo's
  1-minute history limit), and a durable per-day rollup for 400 days that is
  absorbed *before* each prune so long-range statistics survive it.
- Gitignored. Note that Render's free tier uses ephemeral disk, so the file
  does not survive a restart unless `DAYTRADE_HISTORY_FILE` points at a volume.

## Scoring and analytics

Deterministic arithmetic — **no AI/ML services and no paid APIs**.

- **Point-in-time scoring** — a prediction is graded only against bars strictly
  after its signal timestamp, within a forward window.
- **Path-dependent resolution** — whichever of exit/stop is touched first
  decides; a single bar spanning both resolves as a stop, never a win.
- **R-multiples and expectancy** as the primary metric, net of a configurable
  round-trip cost in basis points. Hit rate is reported as secondary context
  because it rises when targets shrink toward the stop.
- **Walk-forward split** — in-sample vs out-of-sample, to expose noise fitting.
- **Breakdowns** by confidence, rating, time-of-day, VIX regime, bias, symbol.
- **Technical indicators** computed locally: EMA, RSI, MACD, Bollinger Bands,
  ATR, VWAP, support/resistance.
- **Black–Scholes** for options delta and implied-volatility handling.
- **Geometric Brownian Motion** for price-range forecasts.

## CI/CD and automation

- **GitHub Actions**
  - `deploy.yml` — publishes `index.html` to GitHub Pages on push to `main`
  - `market-scan.yml` — cron-scheduled mid-session scans plus an after-close
    validation pass, with cold-start retry and a run summary
- **Render** — auto-deploys from `main`; configuration as code in `render.yaml`

No custom domain and no CDN in front: the frontend is served from
`nanda0210.github.io/edge-advisor/` and the API from
`edge-advisor-api.onrender.com` directly.

## Development and testing

- **`py_compile`** and **`pyflakes`** for the backend
- **`node --check`** on the extracted inline JavaScript — it catches scope
  collisions that a visual diff review does not
- **Node with a DOM shim** to execute the render functions against real API
  payloads, rather than only inspecting the diff
- **Standalone Python test scripts** covering concurrency, durability,
  scoring arithmetic, and aggregation
- **Claude Code skills** in `.claude/skills/` encoding product rules and
  provider boundaries

## Deliberate non-choices

Recorded because they are decisions, not omissions:

| Not used | Why |
|---|---|
| Web framework (Flask/FastAPI/Django) | One stdlib module keeps the free-tier footprint small and deployment dependency-free |
| Frontend framework / bundler | No build step; GitHub Pages serves the file as-is |
| Database | A single JSON file with atomic writes covers 7 days of predictions |
| Paid AI or LLM APIs | All scoring is deterministic arithmetic — no per-use cost |
| CDN-hosted assets | Self-contained page; nothing to break or leak externally |
| Background polling | Explicit refresh and bounded watch windows protect the memory budget |

## Keeping this current

Update this file in the **same commit** as the change when any of the
following happens:

1. A dependency is added, removed, or upgraded across a major version
2. A data provider is added, removed, or has its priority changed
3. An API endpoint is added or removed
4. The deployment target, host, or build/CI configuration changes
5. The persistence model or retention policy changes
6. An authentication or secret-handling mechanism changes
7. A scoring or analytics method is introduced or replaced
8. Something on the [non-choices](#deliberate-non-choices) list is adopted

Operational details — endpoint contracts, env-var defaults, troubleshooting —
live in `TECH-DOC.md`. Product rules and constraints live in
`NANDAEDGE_ARCHITECTURE.md`. This file covers *what technology is used and
why*; keep the three from drifting apart.
