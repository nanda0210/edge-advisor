# NandaEdge Advisor — Global Elite Stock Intelligence Dashboard

> A world-class AI-powered stock research and trading dashboard covering 9 high-conviction tickers with real-time prices, 5-layer analysis, options intelligence, and institutional-grade trade plans.

---

## Live Dashboard

```
http://localhost:8765
```

Start the local server (see setup below), then open the URL in any browser.

---

## Watchlist

| Ticker | Company | Sector | Theme |
|--------|---------|--------|-------|
| **NVDA** | NVIDIA Corporation | AI / Semiconductors | AI infrastructure backbone |
| **TSLA** | Tesla, Inc. | EV / Robotics / AI | Multi-vertical disruption |
| **PLTR** | Palantir Technologies | AI / Defense / Data | AI software + government contracts |
| **AMD** | Advanced Micro Devices | Semiconductors / AI | MI300X AI accelerator momentum |
| **MU** | Micron Technology | Memory / HBM | AI memory supercycle |
| **CRWD** | CrowdStrike Holdings | Cybersecurity | Platform leader, ARR growth |
| **INTC** | Intel Corporation | Semiconductors / Foundry | Turnaround play |
| **IONQ** | IonQ Inc. | Quantum Computing | Early-stage quantum leader |
| **RGTI** | Rigetti Computing | Quantum Computing | High-risk speculative |

---

## Features

### Real-Time Market Data
- Live prices for all 9 tickers via **yfinance** (Yahoo Finance)
- US market bar: S&P 500, NASDAQ, DOW, Russell 2000, VIX, 10Y Yield, DXY, Oil, Gold, BTC
- Asia & Europe indices: Nikkei, Hang Seng, Shanghai, DAX, FTSE, CAC 40
- Auto-refresh every 60 seconds

### Five-Layer Analysis Per Ticker
1. **Fundamental** — TipRanks consensus, price targets, Seeking Alpha Factor Grades (Value / Growth / Profitability / Momentum / Revision)
2. **Technical** — RSI, MACD, EMA stack, Bollinger Bands, VWAP, support/resistance, TradingView AI signal
3. **Quantitative / AI** — WallStreetZen Zen Score, factor model, mean reversion filter
4. **Options & Derivatives** — IV Rank, Greeks (Delta/Theta/Vega), max pain, unusual sweep detection
5. **Sentiment & Behavioral** — TipRanks Smart Score, news polarity, insider activity, short interest, Fear & Greed Index

### Daily Trade Plans
Each ticker includes a fully structured plan:
- **Swing Trade** (2–10 days): entry zone, Target 1, Target 2, stop loss
- **Options Play**: strategy, strike, expiry, Greeks, exit rule (50% profit / 2× loss)
- **Conviction Rating**: 1–5 stars + risk level (Low / Medium / High / Speculative)

### Dashboard Sections
| Section | Content |
|---------|---------|
| Macro Regime | VIX regime, Fed stance, yield curve, cycle stage, drawdown protocol |
| Watchlist Table | Live price, change %, volume, 52W range, TipRanks, SA Quant, Smart Score, signal |
| Trade Cards | Full 5-layer analysis card per ticker |
| Options Flow | Unusual sweeps & blocks >$500K premium |
| Analyst Feed | Upgrades / downgrades / new price targets (last 48h) |
| Risk Dashboard | Portfolio heat, position sizing rules, drawdown levels |
| News Feed | Top headlines with ticker tags and polarity scores (−5 to +5) |
| Top Analysts | 7 elite analysts tracked daily |

### Top Analysts Tracked
Tom Lee (Fundstrat) · Lloyd Walmsley (UBS) · Jurrien Timmer (Fidelity) · Ryan Detrick (Carson Group) · Dan Ives (Wedbush) · Mark Lipacis (Evercore ISI) · Steve Reitmeister (WallStreetZen)

---

## Setup & Installation

### Requirements
- Python 3.x (pre-installed on macOS)
- `yfinance` library

### Install dependency
```bash
pip3 install yfinance
```

### Start the server
```bash
python3 server.py
```

The server:
- Automatically kills any existing process on port 8765
- Serves the dashboard at `http://localhost:8765`
- Serves live quotes at `http://localhost:8765/quotes`

### Open the dashboard
```
http://localhost:8765
```

Bookmark this URL. Every time you want to use NandaEdge, just run `python3 server.py` and open the URL.

---

## Research Sources

| Source | Purpose |
|--------|---------|
| **TipRanks** | Analyst consensus, Smart Score, insider transactions, hedge fund activity |
| **Seeking Alpha** | Quant Factor Grades, crowdsourced analysis |
| **TradingView** | AI TA summary, charting, community trade ideas |
| **WallStreetZen** | Zen Score, AI stock summaries, screener |
| **Benzinga Pro** | Breaking news, unusual options activity |
| **SEC EDGAR** | 8-K filings, insider Form 4 transactions, 13F holdings |
| **CBOE** | VIX term structure, put/call ratio, SKEW index |
| **CNN Fear & Greed** | Market-wide sentiment gauge |
| **FRED** | Macro data: CPI, yield curve, M2, credit spreads |

---

## Data Architecture

```
Browser (http://localhost:8765)
        │
        ▼
  server.py (Python)
        │
        ├── GET /          → serves index.html
        └── GET /quotes    → fetches via yfinance → returns JSON
                                     │
                                     ▼
                            Yahoo Finance (real-time)
```

No API keys required. No external proxy. No CORS issues.

---

## File Structure

```
finance-dashboard/
├── index.html     # NandaEdge dashboard UI (HTML/CSS/JS)
├── server.py      # Local data server — port 8765
├── .gitignore
└── README.md
```

---

## Options Strategy Reference

| Market Condition | Strategy |
|-----------------|----------|
| Strong uptrend, low IV | Long Call / Bull Call Spread |
| Strong downtrend, low IV | Long Put / Bear Put Spread |
| High IV, range-bound | Iron Condor / Short Strangle |
| Pre-earnings, low IV | Long Straddle / Strangle |
| Post-earnings, high IV | Iron Condor (sell IV crush) |
| Bullish with income | Covered Call / Bull Put Spread |
| Momentum breakout | ATM Debit Call Spread |

---

## Risk Management Rules

- Max single position: **5%** of portfolio (speculative <$5B cap: **2%**)
- Max options premium at risk: **3%** per trade
- Stop loss: never move further from entry — only tighten
- Close winning options at **50% of max profit**
- Close losing options at **2× premium paid**

| Drawdown | Action |
|----------|--------|
| −5% | Review all positions, tighten stops |
| −10% | Reduce exposure by 50% |
| −15% | Move to cash, defensive hedges only |
| −20% | Full capital preservation mode |

---

## Disclaimer

> All analysis, trade ideas, price targets, and recommendations are for **educational and informational purposes only**. They do not constitute personalized investment advice. Options trading involves substantial risk of loss. Past performance does not guarantee future results. Always consult a licensed financial advisor before investing. Never trade with money you cannot afford to lose.

---

*NandaEdge Advisor v2.0 — Built with Python & vanilla JS · Data via yfinance*
