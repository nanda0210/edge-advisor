# Schwab Environment Setup for NandaEdge Advisor

NandaEdge Advisor can use Schwab for **read-only market data** and **options chains** through Schwab Developer OAuth tokens.

Do **not** store your Charles Schwab or thinkorswim username/password in `.env`.

## Why No Schwab Password

Brokerage login credentials are too sensitive to store inside an app config file. The safer pattern is:

```text
Schwab login happens only on Schwab's OAuth page
        ↓
Schwab returns access/refresh tokens
        ↓
NandaEdge Advisor reads tokens from local .env
        ↓
NandaEdge Advisor uses read-only market data endpoints
```

## Local Setup

Create your local `.env` from the safe template:

```bash
cd /Users/rajamac/myprojects/edge-advisor
cp .env.example .env
```

Then edit:

```text
.env
```

Use these Schwab variables:

```text
SCHWAB_APP_KEY=
SCHWAB_APP_SECRET=
SCHWAB_CALLBACK_URL=http://localhost:8765/schwab/callback
SCHWAB_ACCESS_TOKEN=
SCHWAB_REFRESH_TOKEN=
SCHWAB_BASE_URL=https://api.schwabapi.com
```

## What To Enter

| Variable | What goes here |
|---|---|
| `SCHWAB_APP_KEY` | Schwab Developer app key |
| `SCHWAB_APP_SECRET` | Schwab Developer app secret |
| `SCHWAB_CALLBACK_URL` | OAuth callback URL registered with Schwab |
| `SCHWAB_ACCESS_TOKEN` | OAuth access token returned after login |
| `SCHWAB_REFRESH_TOKEN` | OAuth refresh token returned after login |
| `SCHWAB_BASE_URL` | Usually `https://api.schwabapi.com` |

## What Not To Enter

Do not create or use these:

```text
SCHWAB_USERID
SCHWAB_USERNAME
SCHWAB_PASSWORD
THINKORSWIM_USERNAME
THINKORSWIM_PASSWORD
```

## Product Boundary

Allowed:

- Read-only quotes
- Read-only price history/candles
- Read-only options chains
- Read-only market data support

Not allowed:

- Account balances
- Positions
- Orders
- Live trading
- Order routing
- Storing Schwab login password

## Run Locally

```bash
cd /Users/rajamac/myprojects/edge-advisor
HOST=localhost PYTHONPYCACHEPREFIX=/private/tmp/codex_pycache python3 server.py
```

Open:

```text
http://localhost:8765/
```

## Current Provider Behavior

Options chains already use:

```text
1. Schwab first, if SCHWAB_ACCESS_TOKEN is configured
2. Tradier second, if TRADIER_ACCESS_TOKEN is configured
3. Yahoo fallback
```

Day Trade candles currently remain on Yahoo unless Schwab read-only candle support is added separately.
