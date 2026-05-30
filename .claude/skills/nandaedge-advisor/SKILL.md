---
name: nandaedge-advisor
description: Reference workflow and product rules for maintaining NandaEdge Advisor. Use when Codex is asked to review, modify, debug, deploy, document, or test the NandaEdge Advisor/edge-advisor project, especially tasks involving server.py, index.html, market-data providers, options chains, Schwab or Tradier integration, Yahoo/yfinance behavior, environment variables, or product-readiness/security constraints.
---

# NandaEdge Advisor

## Core Rules

- Preserve the product name exactly as `NandaEdge Advisor`.
- Work in `/Users/rajamac/myprojects/edge-advisor` unless the user gives a different path.
- Keep the app read-only for market intelligence. Do not add live trading, order placement, account access, portfolio/account endpoints, or broker account scopes.
- Do not hardcode secrets. Load credentials from environment variables or local `.env`; keep `.env` gitignored.
- Keep changes minimal, safe, and backward compatible with the existing static frontend and Python backend.

## Data Provider Boundaries

- Keep Yahoo/yfinance for quotes, history, technicals, forecast, and fear/greed support.
- Use Schwab Market Data only for options chains when configured.
- Use Tradier only as a secondary options-chain provider when configured.
- Use Yahoo/yfinance options chains only as the final fallback.
- Preserve the existing `/options` response contract for the frontend unless the user explicitly requests a breaking API change.

For provider details and endpoint expectations, read `references/provider-contract.md`.

For live deployment/security readiness, read `references/live-standards.md` when the task involves production, Render, GitHub Pages, Schwab OAuth, public access, or product-readiness review.

## Required Options Fields

When modifying `/options`, preserve:

- 30/60/90 DTE support through query params and `supportedDTE`.
- `aiConfidence`
- `tradeRating`
- `exitRule`
- `why`
- Strategy names for cash-secured puts and covered calls.
- Existing candidate fields such as symbol, type, expiry, dte, strike, bid, ask, mid, lastPrice, openInterest, volume, iv, delta, breakeven, liquidity flags, and annualized return.

## Intraday Candle Skill

When the user asks for live candles, day-trade entries/exits, speculative alerts, or prediction history:

- Keep this workflow read-only and educational. Never add live trading, broker account access, order tickets, or automated execution.
- Use Yahoo/yfinance intraday history for candle data unless the user explicitly approves another read-only market-data provider.
- Preserve `/candles` for OHLCV candles and `/daytrade` for candle-derived entries/exits on the core watchlist.
- Preserve `/speculative-daytrade` for the top speculative 1-minute candle alerts. It should rank volatile names with strict filters, surface active alerts, remove invalid alerts from the alert window, and include entry, stop, exit, confidence, rating, exit rule, and why-this-trade fields.
- Do not promise a 90% success rate. Phrase the approach as a strict-filter, high-discipline model that must be compared with actual market results.
- Keep prediction history local for 7 days so today's prediction can become tomorrow's comparison record. Store local runtime history in `daytrade_history.json` and keep that file gitignored.
- Keep normal dashboard candle endpoints memory-light: cap candle arrays, compact persisted history, and reserve heavier 5-day validation for explicit after-market validation runs such as `/speculative-daytrade?limit=5&validate=1`.
- Add learning notes that compare prior predictions with realized candles and explain what went wrong, but do not auto-generate broker actions.

## Environment Variables

Support these Schwab variables without logging or committing their values:

```text
SCHWAB_APP_KEY
SCHWAB_APP_SECRET
SCHWAB_CALLBACK_URL
SCHWAB_ACCESS_TOKEN
SCHWAB_REFRESH_TOKEN
```

Optional provider variables may include:

```text
TRADIER_ACCESS_TOKEN
SCHWAB_BASE_URL
TRADIER_BASE_URL
AUTH_TOKEN_HASH
PORT
HOST
```

## Live Website Standards

Treat the live product as a split deployment:

- Static frontend: `https://nanda0210.github.io/edge-advisor/`
- Backend API: `https://edge-advisor-api.onrender.com`
- Schwab callback: `https://nanda0210.github.io/edge-advisor/schwab/callback`

Follow financial-product engineering standards:

- Keep all broker credentials and tokens server-side only. Never place Schwab or Tradier secrets in `index.html`, GitHub Pages, screenshots, docs, logs, or commits.
- Use environment variables on Render for live secrets; keep local tokens in `.env`; keep `.env` gitignored.
- Keep the GitHub callback page static and token-free. It may display a local command containing an OAuth `code`, but must not store, transmit, or exchange tokens itself.
- Treat access tokens as short-lived and refresh tokens as sensitive credentials. Rotate/refresh through `scripts/schwab_oauth_tokens.py`, then update Render env vars manually.
- Preserve CORS only as needed for the static frontend. Do not expose unauthenticated account, order, position, balance, or trading endpoints.
- Keep Render memory use conservative: cap candle arrays, avoid background polling by default, prefer explicit refresh/watch windows for intraday data.
- Label outputs as market intelligence/education. Do not present entries, exits, probabilities, or ratings as guarantees.

When converting or validating the live site:

```bash
curl -I https://nanda0210.github.io/edge-advisor/
curl -sS https://edge-advisor-api.onrender.com/health
curl -I https://nanda0210.github.io/edge-advisor/schwab/callback
```

If `AUTH_TOKEN_HASH` is enabled on Render, protected endpoints must be tested through the dashboard unlock flow or with the correct `Authorization` header.

## OAuth Comments

Add comments for future Schwab OAuth token setup, but keep the app read-only:

- Create a Schwab developer app externally.
- Complete authorization-code flow externally.
- Store access and refresh tokens in environment variables or local `.env`.
- Do not request account scopes, place trades, or refresh tokens through account-access flows unless the user explicitly changes the product scope.

## Workflow

1. Inspect `git status --short` and avoid overwriting user changes.
2. Review `server.py` and `index.html` before making product or API changes.
3. Keep frontend changes unnecessary unless the backend contract changes or the user asks for UI work.
4. Add or update tests by running lightweight local checks first:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/codex_pycache python3 -m py_compile server.py
python3 server.py
curl 'http://localhost:8765/options?symbol=NVDA&type=puts&top=3'
curl 'http://localhost:8765/options?symbol=NVDA&type=puts&dte_min=46&dte_max=75&top=1'
curl 'http://localhost:8765/options?symbol=NVDA&type=calls&dte_min=76&dte_max=110&top=1'
```

If sandboxing blocks local server binding, request approval to run the server locally.

## Delivery Notes

When finishing a NandaEdge task, report:

- Exactly what files changed.
- How to run locally.
- How to test `/options` for `NVDA`.
- Any provider fallback behavior observed, such as Schwab/Tradier skipped because credentials are not configured.
