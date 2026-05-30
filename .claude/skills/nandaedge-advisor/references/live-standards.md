# Live Standards Checklist

Use this checklist when NandaEdge Advisor is being prepared, validated, or changed as a live financial market-intelligence website.

## Security

- Secrets live only in `.env`, OS keychain, Render Environment, or other approved secret stores.
- `.env` stays gitignored and is never displayed with raw token values.
- GitHub Pages serves only static, non-secret assets.
- Schwab and Tradier tokens are never sent to the browser.
- `AUTH_TOKEN_HASH` protects live backend endpoints when the public dashboard is used.
- Logs, docs, and screenshots do not expose access tokens, refresh tokens, app secrets, passwords, or full OAuth callback URLs with live codes.

## Broker/Data Boundaries

- Schwab is used only for market-data options chains.
- Tradier is optional secondary options-chain provider.
- Yahoo/yfinance remains fallback for options and primary source for quotes, history, candles, technicals, forecast, and fear/greed.
- Do not add account access, balances, positions, order preview, order placement, or live trading.

## Reliability

- Keep provider fallbacks explicit in responses via `provider` and `providerWarnings`.
- Keep 30/60/90 DTE support intact.
- Preserve `aiConfidence`, `tradeRating`, `exitRule`, and `why`.
- Cache provider responses conservatively to reduce rate-limit pressure.
- Keep intraday/day-trade scans manual or bounded by short watch windows.
- Compact or cap candle/history payloads for Render/free-tier memory limits.

## Compliance And UX Language

- Use read-only and educational language.
- Avoid guaranteed success-rate claims.
- Explain ratings as model scores or discipline filters, not investment advice.
- Surface provider/source names when relevant.
- Keep exit rules, risks, and invalidation logic visible for speculative alerts.

## Deployment Validation

Run these checks before calling the live site ready:

```bash
curl -I https://nanda0210.github.io/edge-advisor/
curl -sS https://edge-advisor-api.onrender.com/health
curl -I https://nanda0210.github.io/edge-advisor/schwab/callback
```

Local options validation:

```bash
cd /Users/rajamac/myprojects/edge-advisor
HOST=localhost PYTHONPYCACHEPREFIX=/private/tmp/codex_pycache python3 server.py
curl 'http://localhost:8765/options?symbol=NVDA&type=puts&top=3'
curl 'http://localhost:8765/options?symbol=NVDA&type=puts&dte_min=46&dte_max=75&top=1'
curl 'http://localhost:8765/options?symbol=NVDA&type=calls&dte_min=76&dte_max=110&top=1'
```

Expected result when Schwab tokens are configured:

- `provider` is `schwab` for `/options`.
- Required option fields are present.
- No Schwab token values appear in responses or logs.
