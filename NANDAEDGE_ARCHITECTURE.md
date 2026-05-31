# NandaEdge Advisor Architecture

NandaEdge Advisor is a read-only financial market-intelligence website. It must be simple to use, accurate about data sources, and safe with broker credentials.

## Target Product Shape

- Static dashboard on GitHub Pages.
- Python market-data API on Render.
- Schwab OAuth credentials and tokens only on the server.
- Yahoo/yfinance for quotes, history, technicals, forecast, fear/greed, and candles.
- Schwab first for options chains.
- Tradier second for options chains when configured.
- Yahoo options fallback only as last resort.

## Primary User Flows

1. Open live site.
2. Unlock dashboard if API auth is enabled.
3. Use Live Command Center for the highest-value actions:
   - Refresh Live Data
   - Scan Options
   - Day Trade Scan
   - Validate Learning
4. Review provider/source status before trusting a result.
5. Use options candidates as research, not as order instructions.
6. Use day-trade scans as read-only alerts with explicit entry, exit, stop, timestamp, and validation history.

## Data Accuracy Rules

- Every options response should surface the provider.
- Every strategy candidate keeps `aiConfidence`, `tradeRating`, `exitRule`, and `why`.
- Day Trade history should use Pacific trading dates, not UTC calendar dates.
- After market close, weekends, and before open, Day Trade should validate prior predictions automatically when refreshed.
- Pending predictions must not be counted as reviewed outcomes.
- Success-rate language must be historical/comparative, never guaranteed.

## Server Online Model

Render owns all live API calls. GitHub Pages never holds secrets.

The server may refresh Schwab access tokens in memory with `SCHWAB_REFRESH_TOKEN` when Schwab returns an expired-token response. It must not write refreshed tokens into source files, browser storage, logs, or GitHub Pages.

## Simplification Roadmap

### Done

- Online frontend/backend split.
- Schwab OAuth callback page.
- Schwab-first options-chain provider path.
- Server-side Schwab access-token refresh for live uptime.
- Day Trade manual refresh and bounded watch mode.
- Day Trade after-hours auto-validation.
- Live Command Center for quick actions and data status.

### Next

- Add a compact `/status` API with provider readiness: Yahoo, Schwab, Tradier, auth, cache age.
- Add data freshness labels per card: live, cached, stale, provider fallback.
- Add a single “Best Opportunity” queue that merges stock setup, option candidate, and risk regime.
- Add a daily after-market validation automation for Day Trade history.
- Add a token rotation checklist inside the live admin docs.

## Non-Negotiables

- No live trading.
- No account access.
- No balances, positions, orders, order previews, or account scopes.
- No hardcoded secrets.
- No public token exposure.
- No guaranteed success-rate claims.
