# NandaEdge Advisor Live Deployment

NandaEdge Advisor runs as a static dashboard plus a private API:

- Frontend: `https://nanda0210.github.io/edge-advisor/`
- Backend API: `https://edge-advisor-api.onrender.com`
- Schwab OAuth callback: `https://nanda0210.github.io/edge-advisor/schwab/callback`

Keep this product read-only. Do not add live trading, order placement, account access, balances, positions, or broker account scopes.

## 1. GitHub Pages

Push `main` to GitHub. GitHub Pages serves `index.html` and the Schwab callback page.

Validate:

```bash
curl -I https://nanda0210.github.io/edge-advisor/
curl -I https://nanda0210.github.io/edge-advisor/schwab/callback
```

## 2. Render Environment Variables

In Render, open `edge-advisor-api` → Environment and set these values.

Do not commit or paste secrets into source files.

```text
AUTH_TOKEN_HASH=<sha256 hash of dashboard unlock password>
SCHWAB_APP_KEY=<Schwab app key / client id>
SCHWAB_APP_SECRET=<Schwab app secret>
SCHWAB_CALLBACK_URL=https://nanda0210.github.io/edge-advisor/schwab/callback
SCHWAB_ACCESS_TOKEN=<local token from scripts/schwab_oauth_tokens.py>
SCHWAB_REFRESH_TOKEN=<local refresh token from scripts/schwab_oauth_tokens.py>
SCHWAB_BASE_URL=https://api.schwabapi.com
```

The live server keeps tokens server-side. When Schwab returns an expired access
token response, the backend refreshes the access token in memory using
`SCHWAB_REFRESH_TOKEN` and retries the options-chain request once. It does not
write tokens to GitHub Pages, browser storage, logs, or source files.

Optional:

```text
TRADIER_ACCESS_TOKEN=<secondary options provider token>
TRADIER_BASE_URL=https://api.tradier.com
```

## 3. Redeploy Render

After saving env vars, redeploy the Render service.

Validate health:

```bash
curl -I https://edge-advisor-api.onrender.com/health
```

If `AUTH_TOKEN_HASH` is set, protected data endpoints require the dashboard unlock token.

## 4. Validate Schwab Options

Use the live dashboard after unlocking:

```text
https://nanda0210.github.io/edge-advisor/
```

For local validation:

```bash
cd /Users/rajamac/myprojects/edge-advisor
HOST=localhost PYTHONPYCACHEPREFIX=/private/tmp/codex_pycache python3 server.py
curl 'http://localhost:8765/options?symbol=NVDA&type=puts&top=3'
```

Expected behavior:

- `/options` uses Schwab first when tokens are configured.
- Tradier is second if configured.
- Yahoo/yfinance is the final fallback.
- Yahoo/yfinance remains the source for quotes, history, technicals, forecast, candles, and fear/greed.

## 5. Token Refresh

The Render server can refresh access tokens in memory while it is running. If
the service restarts and the saved refresh token has expired or been rotated,
refresh tokens locally:

```bash
cd /Users/rajamac/myprojects/edge-advisor
python3 scripts/schwab_oauth_tokens.py --refresh --write-env
```

Then copy the updated Schwab token values from local `.env` into Render
Environment and redeploy.
