# NandaEdge Advisor Provider Contract

## Current Architecture

- `index.html` is a static dashboard.
- `server.py` serves the dashboard and read-only JSON endpoints.
- The app should remain deployable as a small Python HTTP server with no build step.

## Endpoint Ownership

Use Yahoo/yfinance for:

- `/quotes`
- `/technicals`
- `/forecast`
- `/history`
- `/earnings` when implemented from public data
- Risk-free-rate helper via `^TNX`

Use CNN/public proxy behavior for:

- `/feargreed`

Use the provider chain for:

- `/options`

## `/options` Provider Priority

The `/options` endpoint should try providers in this order:

1. Schwab Market Data, only when Schwab access token is configured.
2. Tradier, only when Tradier access token is configured.
3. Yahoo/yfinance, as fallback only.

Return provider metadata such as `provider` and non-secret provider warnings when useful. Never include tokens, app secrets, callback secrets, or Authorization headers in responses or logs.

## `/options` Query Shape

Support:

```text
symbol=NVDA
type=puts|calls
dte_min=21
dte_max=45
delta_min=0.15
delta_max=0.30
top=5
```

Preserve default behavior:

- Puts default around 30 DTE.
- Calls default around 21-35 DTE unless intentionally changed.
- 30/60/90 DTE buckets:
  - `30`: 21-45
  - `60`: 46-75
  - `90`: 76-110

## Candidate Response Shape

Each candidate should include:

```json
{
  "symbol": "NVDA",
  "type": "puts",
  "provider": "schwab|tradier|yahoo",
  "strategy": "Cash-Secured Put",
  "expiry": "YYYY-MM-DD",
  "dte": 34,
  "strike": 200.0,
  "bid": 5.1,
  "ask": 5.2,
  "mid": 5.15,
  "lastPrice": 5.21,
  "openInterest": 2420,
  "volume": 490,
  "iv": 44.3,
  "delta": -0.261,
  "deltaAbs": 0.261,
  "breakeven": 194.85,
  "capitalRequired": 20000.0,
  "annualizedReturnPct": 27.64,
  "spreadPct": 1.94,
  "liqOK_OI": true,
  "liqOK_Spread": true,
  "liqOK": true,
  "aiConfidence": 95,
  "tradeRating": "Institutional Grade",
  "exitRule": "50% profit or 2x loss",
  "why": ["Favorable delta range for premium strategy"]
}
```

It is acceptable for JSON encoding to escape special characters in `exitRule`; keep the semantic value unchanged.

## Safety Constraints

- Do not add endpoints that expose accounts, balances, positions, transactions, or orders.
- Do not add POST/PUT/DELETE trading actions.
- Do not log secrets.
- Do not commit `.env`.
- Keep CORS/auth behavior aligned with the existing app unless the user asks for a security redesign.
