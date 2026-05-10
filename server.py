#!/usr/bin/env python3
"""
NandaEdge Data Server — v3.5
  Dashboard   → /
  Quotes      → /quotes        ?symbols=NVDA,TSLA,...   realtime quotes
  Technicals  → /technicals    ?symbols=...             EMA/RSI/MACD/BB/ATR/VWAP
  Forecast    → /forecast      ?symbols=...             GBM projections (1w..5y)
  Fear/Greed  → /feargreed                              CNN proxy
  Cloud-ready: reads PORT/HOST from env; binds 0.0.0.0 when PORT is set.
"""
import os, sys, signal, time, json, math, socket, warnings, urllib.request, threading, hashlib, random
from datetime import date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))

def _load_dotenv(path=None):
    """Tiny local .env loader; real env vars win and secrets are never logged."""
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception as e:
        print(f"  WARN .env load skipped: {e}", flush=True)

_load_dotenv()

# ── Auth (optional: set AUTH_TOKEN_HASH in env to enable) ──
AUTH_HASH = (os.environ.get("AUTH_TOKEN_HASH", "") or "").strip().lower()
_AUTH_FAILS = {}            # ip -> [timestamp, ...] within last 15 min
_AUTH_FAIL_WINDOW = 900     # 15 min
_AUTH_FAIL_MAX    = 10

def _client_ip(handler):
    # Render forwards real IP via X-Forwarded-For
    fwd = handler.headers.get("X-Forwarded-For", "")
    if fwd: return fwd.split(",")[0].strip()
    return handler.client_address[0]

def _rate_limited(ip):
    now = time.time()
    fails = [t for t in _AUTH_FAILS.get(ip, []) if now - t < _AUTH_FAIL_WINDOW]
    _AUTH_FAILS[ip] = fails
    return len(fails) >= _AUTH_FAIL_MAX

def _record_fail(ip):
    _AUTH_FAILS.setdefault(ip, []).append(time.time())

def _auth_state(handler):
    """Returns (ok: bool, status: 'open'|'ok'|'unauthorized'|'rate_limited')."""
    if not AUTH_HASH:
        return True, "open"
    ip = _client_ip(handler)
    if _rate_limited(ip):
        return False, "rate_limited"
    h = handler.headers.get("Authorization", "")
    if h.lower().startswith("bearer "):
        token = h[7:].strip().lower()
        if token == AUTH_HASH:
            return True, "ok"
    _record_fail(ip)
    return False, "unauthorized"

warnings.filterwarnings("ignore")

PORT      = int(os.environ.get("PORT", 8765))
HOST      = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "localhost")
HTML_FILE = os.path.join(BASE_DIR, "index.html")

# ── Market-data credentials (optional; do not commit .env) ──
# Schwab is prepared for OPTIONS CHAINS ONLY. Keep account scopes, order routes,
# live trading, and account access out of this app.
SCHWAB_APP_KEY       = (os.environ.get("SCHWAB_APP_KEY", "") or "").strip()
SCHWAB_APP_SECRET    = (os.environ.get("SCHWAB_APP_SECRET", "") or "").strip()
SCHWAB_CALLBACK_URL  = (os.environ.get("SCHWAB_CALLBACK_URL", "") or "").strip()
SCHWAB_ACCESS_TOKEN  = (os.environ.get("SCHWAB_ACCESS_TOKEN", "") or "").strip()
SCHWAB_REFRESH_TOKEN = (os.environ.get("SCHWAB_REFRESH_TOKEN", "") or "").strip()
SCHWAB_BASE_URL      = (os.environ.get("SCHWAB_BASE_URL", "https://api.schwabapi.com") or "").rstrip("/")

# Optional legacy/secondary provider. If absent, /options skips Tradier safely.
TRADIER_ACCESS_TOKEN = (os.environ.get("TRADIER_ACCESS_TOKEN", "") or "").strip()
TRADIER_BASE_URL     = (os.environ.get("TRADIER_BASE_URL", "https://api.tradier.com") or "").rstrip("/")

WATCH = ["NVDA","TSLA","PLTR","AMD","MU","CRWD","INTC","IONQ","RGTI"]
MARKETS = [
    "^GSPC","^IXIC","^DJI","^RUT","^VIX","^TNX",
    "DX-Y.NYB","CL=F","GC=F","BTC-USD","ETH-USD",
    "^N225","^HSI","000001.SS","^GDAXI","^FTSE","^FCHI","SOL-USD",
]
SECTORS = [
    # Curated for IT / semis / cloud / auto focus (user preference)
    "XLK",   # Technology (broad)
    "SMH",   # Semiconductors (VanEck — most liquid)
    "SOXX",  # Semiconductors (iShares — alt weight)
    "IGV",   # Software / IT services
    "WCLD",  # Cloud computing (WisdomTree)
    "SKYY",  # Cloud computing (First Trust)
    "DRIV",  # Autonomous & EV (Global X)
    "XLC",   # Communication Services
    "XLY",   # Consumer Discretionary (TSLA-adjacent)
]
ALL = WATCH + MARKETS + SECTORS

# ── Caching — per-symbol so user watchlists share cache ───
_tech_cache  = {}                     # sym -> {"t": float, "data": dict}
_fcst_cache  = {}                     # sym -> {"t": float, "data": dict}
_opts_cache  = {}                     # (sym, type) -> {"t": float, "data": list}
_hist_cache  = {}                     # (sym, days) -> {"t": float, "data": list}
_earn_cache  = {}                     # sym -> {"t": float, "data": dict}
_rate_cache  = {"t": 0, "rate": 0.045}  # risk-free rate from ^TNX
_fg_cache    = {"t": 0, "data": None}
TECH_TTL = 300                        # 5 min
FCST_TTL = 3600                       # 1 hour — forecasts don't need to update often
OPTS_TTL = 600                        # 10 min — options chains move fast but pulling is expensive
HIST_TTL = 1800                       # 30 min — daily closes don't change intraday
EARN_TTL = 86400                      # 24 hr — earnings dates rarely change intraday
RATE_TTL = 3600                       # 1 hour for risk-free rate
FG_TTL   = 600                        # 10 min

# ── Options safety / strategy config ─────────────────────
OPTIONS_MIN_DELAY = 1.4       # slow down Yahoo/option chain calls
OPTIONS_ERROR_TTL = 180       # cache provider errors briefly to avoid hammering

DTE_BUCKETS = {
    "30": (21, 45),
    "60": (46, 75),
    "90": (76, 110),
}

_opts_last_call = {"t": 0.0}

def _options_provider_delay():
    """Protects data provider from rapid option-chain requests."""
    elapsed = time.time() - _opts_last_call["t"]
    wait = OPTIONS_MIN_DELAY - elapsed
    if wait > 0:
        time.sleep(wait + random.uniform(0.2, 0.8))
    _opts_last_call["t"] = time.time()


def _cache_options(cache_key, data):
    _opts_cache[cache_key] = {"t": time.time(), "data": data}
    return data


def calculate_ai_confidence(delta_abs, iv_pct, oi, spread_pct=None):
    score = 50

    if 0.15 <= delta_abs <= 0.35:
        score += 15
    if oi >= 500:
        score += 10
    if oi >= 1000:
        score += 8
    if 20 <= iv_pct <= 80:
        score += 8
    if spread_pct is not None and spread_pct <= 5:
        score += 7

    return min(score, 95)


def trade_rating(score):
    if score >= 85:
        return "Institutional Grade"
    if score >= 72:
        return "High Probability"
    if score >= 62:
        return "Moderate"
    return "Speculative"


def why_this_trade(opt_type, delta_abs, oi, iv_pct, spread_pct):
    reasons = []

    if 0.15 <= delta_abs <= 0.35:
        reasons.append("Favorable delta range for premium strategy")
    if oi >= 500:
        reasons.append("Healthy open interest supports liquidity")
    if iv_pct >= 20:
        reasons.append("Elevated premium environment")
    if spread_pct is not None and spread_pct <= 5:
        reasons.append("Tight bid/ask spread improves execution quality")

    if opt_type == "puts":
        reasons.append("Cash-secured put setup can acquire shares at a lower effective basis")
    else:
        reasons.append("Covered call setup can generate income against existing shares")

    return reasons[:4]

# ── Symbol parsing / validation ──────────────────────────
import re
_SYM_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,12}$")

def parse_symbols(query, default):
    """Parse ?symbols=NVDA,TSLA,...  Returns deduped list, falls back to default."""
    raw = (query.get("symbols", [""])[0] or "").strip()
    if not raw:
        return list(default)
    syms = []
    seen = set()
    for s in raw.split(","):
        s = s.strip().upper()
        if s and _SYM_RE.match(s) and s not in seen:
            seen.add(s); syms.append(s)
    return syms or list(default)

# ── Port Management ────────────────────────────────────
def kill_port(port):
    try:
        import subprocess
        pids = subprocess.run(["lsof","-ti",f":{port}"],
                              capture_output=True, text=True).stdout.split()
        for pid in pids:
            os.kill(int(pid), signal.SIGKILL)
            print(f"  Killed pid {pid} on :{port}", flush=True)
        if pids: time.sleep(1.5)
    except Exception as e:
        print(f"  kill_port: {e}", flush=True)

def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) != 0

# ── Quotes (realtime) ──────────────────────────────────
def fetch_quotes(syms=None):
    import yfinance as yf
    syms = syms or ALL
    results = []
    tickers = yf.Tickers(" ".join(syms))
    for sym in syms:
        try:
            fi     = tickers.tickers[sym].fast_info
            price  = fi.last_price or 0
            prev   = getattr(fi, "previous_close", None) or \
                     getattr(fi, "regular_market_previous_close", price)
            chg    = price - prev
            chgPct = (chg / prev * 100) if prev else 0
            results.append({
                "symbol":                     sym,
                "regularMarketPrice":         round(price, 4),
                "regularMarketChange":        round(chg,   4),
                "regularMarketChangePercent": round(chgPct, 4),
                "regularMarketOpen":          getattr(fi, "open", None),
                "regularMarketDayHigh":       getattr(fi, "day_high", None),
                "regularMarketDayLow":        getattr(fi, "day_low", None),
                "regularMarketVolume":        getattr(fi, "last_volume", None) or getattr(fi, "three_month_average_volume", None),
                "fiftyTwoWeekHigh":           getattr(fi, "fifty_two_week_high", None),
                "fiftyTwoWeekLow":            getattr(fi, "fifty_two_week_low",  None),
            })
        except Exception as e:
            print(f"  WARN quote {sym}: {e}", flush=True)
    return {"quoteResponse": {"result": results, "error": None}}

# ── Technicals (computed indicators) ───────────────────
def fetch_tech(syms=None):
    syms = syms or WATCH
    now = time.time()

    out = {}
    fresh_needed = []
    for sym in syms:
        c = _tech_cache.get(sym)
        if c and (now - c["t"] < TECH_TTL):
            out[sym] = c["data"]
        else:
            fresh_needed.append(sym)

    if not fresh_needed:
        return out

    import yfinance as yf
    import pandas as pd
    import numpy as np

    df = yf.download(" ".join(fresh_needed), period="1y", interval="1d",
                     group_by="ticker", threads=True, progress=False, auto_adjust=False)

    for sym in fresh_needed:
        try:
            d = (df[sym].dropna() if isinstance(df.columns, pd.MultiIndex) else df.dropna())
            if len(d) < 30:
                out[sym] = None
                continue
            close = d["Close"]; high = d["High"]; low = d["Low"]; vol = d["Volume"]
            last  = float(close.iloc[-1])

            def ema(n):
                return float(close.ewm(span=n, adjust=False).mean().iloc[-1])

            ema9, ema20, ema50 = ema(9), ema(20), ema(50)
            ema200 = ema(200) if len(d) >= 200 else None

            # RSI(14) — Wilder
            delta = close.diff()
            gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss  = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            rs    = gain / loss.replace(0, np.nan)
            rsi   = float((100 - 100/(1+rs)).iloc[-1])

            # MACD(12/26/9)
            macd_line   = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_v   = float(macd_line.iloc[-1])
            macd_sig = float(signal_line.iloc[-1])
            macd_h   = macd_v - macd_sig
            # Detect cross vs prior bar
            if len(macd_line) >= 2:
                prev_h = float(macd_line.iloc[-2] - signal_line.iloc[-2])
                if prev_h <= 0 and macd_h > 0:   cross = "bull_cross"
                elif prev_h >= 0 and macd_h < 0: cross = "bear_cross"
                elif macd_h > 0:                 cross = "bull"
                else:                            cross = "bear"
            else:
                cross = "flat"

            # Bollinger(20, 2)
            bb_mid = float(close.rolling(20).mean().iloc[-1])
            bb_std = float(close.rolling(20).std().iloc[-1])
            bb_up, bb_lo = bb_mid + 2*bb_std, bb_mid - 2*bb_std
            # Squeeze: current bandwidth < 0.8 × 50-day avg bandwidth
            bw_series = (close.rolling(20).mean() + 2*close.rolling(20).std()) \
                      - (close.rolling(20).mean() - 2*close.rolling(20).std())
            avg_bw = float(bw_series.rolling(50).mean().iloc[-1]) if len(bw_series.dropna()) >= 50 else None
            bb_squeeze = bool(avg_bw and (bb_up - bb_lo) < 0.8 * avg_bw)

            # ATR(14) — Wilder
            prev_c = close.shift(1)
            tr = pd.concat([high-low, (high-prev_c).abs(), (low-prev_c).abs()], axis=1).max(axis=1)
            atr = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])

            # 20-day HLC3 VWAP (daily-data proxy)
            hlc3 = (high + low + close) / 3
            v20  = vol.tail(20); hlc20 = hlc3.tail(20)
            vwap = float((hlc20 * v20).sum() / v20.sum()) if v20.sum() > 0 else None

            # Support / Resistance — tight (20d) and broad (60d)
            s1 = float(low.tail(20).min());   r1 = float(high.tail(20).max())
            s2 = float(low.tail(60).min());   r2 = float(high.tail(60).max())

            # Trend classification
            if ema200 is None:
                trend = "up" if last > ema20 > ema50 else "down" if last < ema20 < ema50 else "range"
            elif last > ema9 > ema20 > ema50 > ema200:
                trend = "strong_up"
            elif last > ema20 > ema50:
                trend = "up"
            elif last < ema9 < ema20 < ema50 < ema200:
                trend = "strong_down"
            elif last < ema20 < ema50:
                trend = "down"
            else:
                trend = "range"

            # Volume ratio — today vs 30-day average
            avg_vol = float(vol.tail(30).mean())
            today_vol = float(vol.iloc[-1])
            vol_ratio = (today_vol / avg_vol) if avg_vol else None

            out[sym] = {
                "last":        round(last, 2),
                "dayOpen":     round(float(d["Open"].iloc[-1]), 2),
                "dayHigh":     round(float(high.iloc[-1]), 2),
                "dayLow":      round(float(low.iloc[-1]), 2),
                "ema9":        round(ema9,  2),
                "ema20":       round(ema20, 2),
                "ema50":       round(ema50, 2),
                "ema200":      round(ema200, 2) if ema200 else None,
                "rsi14":       round(rsi,  1),
                "macd":        round(macd_v, 3),
                "macdSignal":  round(macd_sig, 3),
                "macdHist":    round(macd_h, 3),
                "macdCross":   cross,
                "bbUpper":     round(bb_up, 2),
                "bbMid":       round(bb_mid, 2),
                "bbLower":     round(bb_lo, 2),
                "bbSqueeze":   bb_squeeze,
                "vwap":        round(vwap, 2) if vwap else None,
                "atr14":       round(atr, 2),
                "support1":    round(s1, 2),
                "support2":    round(s2, 2),
                "resistance1": round(r1, 2),
                "resistance2": round(r2, 2),
                "trend":       trend,
                "todayVol":    today_vol,
                "avgVol30d":   avg_vol,
                "volRatio":    round(vol_ratio, 2) if vol_ratio else None,
            }
            _tech_cache[sym] = {"t": now, "data": out[sym]}
        except Exception as e:
            print(f"  WARN tech {sym}: {e}", flush=True)
            out[sym] = None

    return out

# ── Forecast (GBM-based price projection) ───────────────
# Method: estimate drift μ and volatility σ from 2yr daily log returns,
# project price assuming geometric Brownian motion:
#   median = P0 * exp(μt)
#   p25/p75 = P0 * exp(μt ± 0.6745·σ·√t)
# Returns six horizons in trading days: 1w, 1mo, 3mo, 6mo, 1y, 5y.
HORIZONS = [
    ("1w",  5),    ("1mo", 21),   ("3mo", 63),
    ("6mo", 126),  ("1y",  252),  ("5y", 1260),
]

def fetch_forecast(syms=None):
    syms = syms or WATCH
    now = time.time()

    out = {}
    fresh_needed = []
    for sym in syms:
        c = _fcst_cache.get(sym)
        if c and (now - c["t"] < FCST_TTL):
            out[sym] = c["data"]
        else:
            fresh_needed.append(sym)

    if not fresh_needed:
        return out

    import yfinance as yf
    import pandas as pd
    import numpy as np

    df = yf.download(" ".join(fresh_needed), period="2y", interval="1d",
                     group_by="ticker", threads=True, progress=False, auto_adjust=True)

    for sym in fresh_needed:
        try:
            d = (df[sym].dropna() if isinstance(df.columns, pd.MultiIndex) else df.dropna())
            close = d["Close"]
            if len(close) < 60:
                out[sym] = None
                continue
            log_ret = np.log(close / close.shift(1)).dropna()
            mu_d  = float(log_ret.mean())            # daily drift
            sig_d = float(log_ret.std(ddof=1))       # daily vol
            p0    = float(close.iloc[-1])

            horizons = []
            for label, t in HORIZONS:
                drift = mu_d * t
                vol_t = sig_d * math.sqrt(t)
                median = p0 * math.exp(drift)
                p25    = p0 * math.exp(drift - 0.6745 * vol_t)
                p75    = p0 * math.exp(drift + 0.6745 * vol_t)
                p05    = p0 * math.exp(drift - 1.6449 * vol_t)
                p95    = p0 * math.exp(drift + 1.6449 * vol_t)
                expRet = (median / p0 - 1.0) * 100.0
                horizons.append({
                    "label":  label,
                    "days":   t,
                    "median": round(median, 2),
                    "p25":    round(p25, 2),
                    "p75":    round(p75, 2),
                    "p05":    round(p05, 2),
                    "p95":    round(p95, 2),
                    "expReturnPct": round(expRet, 2),
                })

            out[sym] = {
                "last":     round(p0, 2),
                "muDaily":  round(mu_d, 6),
                "sigDaily": round(sig_d, 6),
                "muAnnualPct":  round(mu_d  * 252 * 100, 2),
                "sigAnnualPct": round(sig_d * math.sqrt(252) * 100, 2),
                "horizons": horizons,
                "method":   "GBM (μ,σ from 2yr daily log-returns)",
            }
            _fcst_cache[sym] = {"t": now, "data": out[sym]}
        except Exception as e:
            print(f"  WARN forecast {sym}: {e}", flush=True)
            out[sym] = None

    return out

# ── Black-Scholes (pure stdlib via math.erf) ───────────
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _bs_d1(S, K, T, r, sigma):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))

def bs_call_delta(S, K, T, r, sigma):
    d1 = _bs_d1(S, K, T, r, sigma)
    return None if d1 is None else _norm_cdf(d1)

def bs_put_delta(S, K, T, r, sigma):
    d1 = _bs_d1(S, K, T, r, sigma)
    return None if d1 is None else _norm_cdf(d1) - 1.0   # negative

# ── Risk-free rate (10Y yield via ^TNX) ────────────────
def get_risk_free_rate():
    now = time.time()
    if now - _rate_cache["t"] < RATE_TTL:
        return _rate_cache["rate"]
    try:
        import yfinance as yf
        info = yf.Ticker("^TNX").fast_info
        v = float(info.last_price or 0)
        if v > 0:
            r = v / 100.0   # ^TNX is the yield × 10 (e.g. 45.0 = 4.50%) — but yfinance returns it as the percent already in modern versions
            # Heuristic guard: realistic 10Y yields fall in 0.5%–10%. If r looks like 45, divide again.
            if r > 0.20:
                r = r / 10.0
            _rate_cache.update(t=now, rate=r)
            return r
    except Exception as e:
        print(f"  WARN risk-free rate: {e}", flush=True)
    return _rate_cache["rate"]

# ── Options chain (CSP / CC candidates) ────────────────
def _http_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _num(v, default=None):
    try:
        if v in ("", None):
            return default
        return float(v)
    except Exception:
        return default


def _int(v, default=0):
    try:
        if v in ("", None):
            return default
        return int(float(v))
    except Exception:
        return default


def _iv_decimal(v):
    iv = _num(v, 0.0) or 0.0
    return iv / 100.0 if iv > 3.0 else iv


def _today_and_window(dte_min, dte_max):
    today = date.today()
    return today, today + timedelta(days=dte_min), today + timedelta(days=dte_max)


def _fetch_yahoo_spot(sym):
    import yfinance as yf
    _options_provider_delay()
    tk = yf.Ticker(sym)
    spot_info = tk.fast_info
    return tk, float(spot_info.last_price or 0)


def _fetch_schwab_option_rows(sym, opt_type, dte_min, dte_max):
    """
    Schwab Market Data integration for options chains only.

    OAuth setup note for later:
    1. Create a Schwab developer app and set SCHWAB_APP_KEY,
       SCHWAB_APP_SECRET, and SCHWAB_CALLBACK_URL outside source control.
    2. Complete Schwab's authorization-code flow externally.
    3. Store only the resulting SCHWAB_ACCESS_TOKEN/SCHWAB_REFRESH_TOKEN in
       the runtime environment or local .env. This app intentionally does not
       request account scopes, place trades, or refresh tokens automatically.
    """
    if not SCHWAB_ACCESS_TOKEN:
        raise RuntimeError("Schwab not configured: missing SCHWAB_ACCESS_TOKEN")

    today, from_date, to_date = _today_and_window(dte_min, dte_max)
    params = {
        "symbol": sym,
        "contractType": "PUT" if opt_type == "puts" else "CALL",
        "strikeCount": 80,
        "includeQuotes": "TRUE",
        "strategy": "SINGLE",
        "fromDate": from_date.isoformat(),
        "toDate": to_date.isoformat(),
    }
    url = f"{SCHWAB_BASE_URL}/marketdata/v1/chains?{urlencode(params)}"
    data = _http_json(url, headers={
        "Authorization": f"Bearer {SCHWAB_ACCESS_TOKEN}",
        "Accept": "application/json",
    })

    spot = _num(data.get("underlyingPrice") or data.get("underlying", {}).get("last"), None)
    exp_map = data.get("putExpDateMap" if opt_type == "puts" else "callExpDateMap") or {}
    rows = []
    for exp_key, strikes in exp_map.items():
        exp = exp_key.split(":", 1)[0]
        try:
            dte = max(0, (date.fromisoformat(exp) - today).days)
        except Exception:
            dte = None
        if dte is None or dte < dte_min or dte > dte_max:
            continue
        for contracts in (strikes or {}).values():
            for c in contracts or []:
                rows.append({
                    "provider": "schwab",
                    "expiry": exp,
                    "dte": _int(c.get("daysToExpiration"), dte),
                    "strike": _num(c.get("strikePrice")),
                    "bid": _num(c.get("bid"), 0.0),
                    "ask": _num(c.get("ask"), 0.0),
                    "last": _num(c.get("last") or c.get("lastPrice"), 0.0),
                    "openInterest": _int(c.get("openInterest"), 0),
                    "volume": _int(c.get("totalVolume") or c.get("volume"), 0),
                    "iv": _iv_decimal(c.get("volatility") or c.get("impliedVolatility")),
                    "delta": _num(c.get("delta"), None),
                })
    return {"provider": "schwab", "spot": spot, "rows": rows}


def _tradier_expirations(sym):
    url = f"{TRADIER_BASE_URL}/v1/markets/options/expirations?{urlencode({'symbol': sym, 'includeAllRoots': 'true', 'strikes': 'false'})}"
    data = _http_json(url, headers={
        "Authorization": f"Bearer {TRADIER_ACCESS_TOKEN}",
        "Accept": "application/json",
    })
    dates = (data.get("expirations") or {}).get("date") or []
    return dates if isinstance(dates, list) else [dates]


def _fetch_tradier_option_rows(sym, opt_type, dte_min, dte_max):
    if not TRADIER_ACCESS_TOKEN:
        raise RuntimeError("Tradier not configured: missing TRADIER_ACCESS_TOKEN")

    today, _, _ = _today_and_window(dte_min, dte_max)
    expiries = []
    for exp in _tradier_expirations(sym):
        try:
            dte = max(0, (date.fromisoformat(exp) - today).days)
        except Exception:
            continue
        if dte_min <= dte <= dte_max:
            expiries.append((exp, dte))

    rows = []
    for exp, dte in expiries:
        params = {"symbol": sym, "expiration": exp, "greeks": "true"}
        url = f"{TRADIER_BASE_URL}/v1/markets/options/chains?{urlencode(params)}"
        data = _http_json(url, headers={
            "Authorization": f"Bearer {TRADIER_ACCESS_TOKEN}",
            "Accept": "application/json",
        })
        opts = (data.get("options") or {}).get("option") or []
        if isinstance(opts, dict):
            opts = [opts]
        want = "put" if opt_type == "puts" else "call"
        for c in opts:
            if str(c.get("option_type", "")).lower() != want:
                continue
            greeks = c.get("greeks") or {}
            rows.append({
                "provider": "tradier",
                "expiry": c.get("expiration_date") or exp,
                "dte": dte,
                "strike": _num(c.get("strike")),
                "bid": _num(c.get("bid"), 0.0),
                "ask": _num(c.get("ask"), 0.0),
                "last": _num(c.get("last"), 0.0),
                "openInterest": _int(c.get("open_interest"), 0),
                "volume": _int(c.get("volume"), 0),
                "iv": _iv_decimal(greeks.get("mid_iv") or greeks.get("iv")),
                "delta": _num(greeks.get("delta"), None),
            })

    _, spot = _fetch_yahoo_spot(sym)
    return {"provider": "tradier", "spot": spot, "rows": rows}


def _fetch_yahoo_option_rows(sym, opt_type, dte_min, dte_max):
    tk, spot = _fetch_yahoo_spot(sym)
    expiries = list(tk.options or ())
    today, _, _ = _today_and_window(dte_min, dte_max)
    rows = []

    for exp in expiries:
        try:
            dte = max(0, (date.fromisoformat(exp) - today).days)
        except Exception:
            continue
        if dte < dte_min or dte > dte_max:
            continue

        _options_provider_delay()
        chain = tk.option_chain(exp)
        df = chain.puts if opt_type == "puts" else chain.calls
        for _, row in df.iterrows():
            rows.append({
                "provider": "yahoo",
                "expiry": exp,
                "dte": dte,
                "strike": _num(row.get("strike")),
                "bid": _num(row.get("bid"), 0.0),
                "ask": _num(row.get("ask"), 0.0),
                "last": _num(row.get("lastPrice"), 0.0),
                "openInterest": _int(row.get("openInterest"), 0),
                "volume": _int(row.get("volume"), 0),
                "iv": _iv_decimal(row.get("impliedVolatility")),
                "delta": None,
            })
    return {"provider": "yahoo", "spot": spot, "rows": rows}


def _fetch_option_rows(sym, opt_type, dte_min, dte_max):
    providers = [
        ("schwab", _fetch_schwab_option_rows, bool(SCHWAB_ACCESS_TOKEN)),
        ("tradier", _fetch_tradier_option_rows, bool(TRADIER_ACCESS_TOKEN)),
        ("yahoo", _fetch_yahoo_option_rows, True),
    ]
    warnings_out = []
    for name, fn, enabled in providers:
        if not enabled:
            warnings_out.append(f"{name} skipped: not configured")
            continue
        try:
            data = fn(sym, opt_type, dte_min, dte_max)
            if data.get("spot") and data.get("rows"):
                data["warnings"] = warnings_out
                return data
            warnings_out.append(f"{name} returned no matching contracts")
        except Exception as e:
            warnings_out.append(f"{name} unavailable: {e}")
    return {"provider": None, "spot": None, "rows": [], "warnings": warnings_out}


def fetch_options(sym, opt_type, dte_min, dte_max, delta_min, delta_max, top_n=5):
    """
    Returns top-N candidate contracts.
    opt_type: 'puts' = cash-secured puts, 'calls' = covered calls.

    Provider priority:
    - Schwab Market Data first when SCHWAB_ACCESS_TOKEN is configured
    - Tradier second when TRADIER_ACCESS_TOKEN is configured
    - Yahoo/yfinance fallback only as last resort

    Improvements:
    - Caches success and provider-fallback results
    - Slows Yahoo option-chain calls to reduce provider rate limiting
    - Supports 30/60/90 DTE ranges through query params
    - Adds AI confidence, trade rating, and why-this-trade explanations
    """
    cache_key = (sym, opt_type, dte_min, dte_max, round(delta_min, 2), round(delta_max, 2), top_n)
    now = time.time()

    c = _opts_cache.get(cache_key)
    if c and (now - c["t"] < OPTS_TTL):
        return c["data"]

    chain_data = _fetch_option_rows(sym, opt_type, dte_min, dte_max)
    S = _num(chain_data.get("spot"), 0.0) or 0.0
    provider = chain_data.get("provider")
    provider_warnings = chain_data.get("warnings") or []

    if S <= 0:
        return _cache_options(cache_key, {
            "symbol": sym,
            "type": opt_type,
            "spot": None,
            "provider": provider,
            "providerWarnings": provider_warnings,
            "candidates": [],
            "error": "no spot price"
        })

    r = get_risk_free_rate()
    candidates = []

    for row in chain_data.get("rows") or []:
        try:
            exp = row["expiry"]
            dte = int(row["dte"])
            strike = float(row["strike"])
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            last = float(row.get("last", 0) or 0)
            oi = int(row.get("openInterest", 0) or 0)
            vol = int(row.get("volume", 0) or 0)
            iv = float(row.get("iv", 0) or 0)
        except Exception:
            continue

        if iv <= 0 or strike <= 0:
            continue

        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last
        if mid <= 0:
            continue

        spread_pct = ((ask - bid) / mid * 100.0) if (mid > 0 and ask > bid > 0) else None

        T = dte / 365.0
        provider_delta = _num(row.get("delta"), None)
        if provider_delta is not None and opt_type == "puts" and provider_delta > 0:
            provider_delta = -provider_delta
        delta = provider_delta
        if delta is None or abs(delta) > 1:
            delta = bs_put_delta(S, strike, T, r, iv) if opt_type == "puts" else bs_call_delta(S, strike, T, r, iv)

        if delta is None:
            continue

        delta_abs = abs(delta)

        if delta_abs < delta_min or delta_abs > delta_max:
            continue

        liq_oi = oi >= 500
        liq_spread = (spread_pct is not None) and (spread_pct <= 5.0)

        if opt_type == "puts":
            breakeven = strike - mid
            cap_required = strike * 100
            ann_return = (mid / strike) * (365.0 / max(dte, 1)) * 100.0 if dte > 0 else None
        else:
            breakeven = strike + mid
            cap_required = None
            ann_return = (mid / S) * (365.0 / max(dte, 1)) * 100.0 if dte > 0 and S > 0 else None

        iv_pct = iv * 100.0
        ai_score = calculate_ai_confidence(delta_abs, iv_pct, oi, spread_pct)

        candidates.append({
            "symbol": sym,
            "type": opt_type,
            "provider": provider,
            "strategy": "Cash-Secured Put" if opt_type == "puts" else "Covered Call",
            "expiry": exp,
            "dte": dte,
            "strike": round(strike, 2),
            "bid": round(bid, 2),
            "ask": round(ask, 2),
            "mid": round(mid, 2),
            "lastPrice": round(last, 2),
            "openInterest": oi,
            "volume": vol,
            "iv": round(iv_pct, 1),
            "delta": round(delta, 3),
            "deltaAbs": round(delta_abs, 3),
            "breakeven": round(breakeven, 2),
            "capitalRequired": cap_required,
            "annualizedReturnPct": round(ann_return, 2) if ann_return is not None else None,
            "spreadPct": round(spread_pct, 2) if spread_pct is not None else None,
            "liqOK_OI": liq_oi,
            "liqOK_Spread": liq_spread,
            "liqOK": bool(liq_oi and liq_spread),
            "aiConfidence": ai_score,
            "tradeRating": trade_rating(ai_score),
            "exitRule": "50% profit or 2× loss",
            "why": why_this_trade(opt_type, delta_abs, oi, iv_pct, spread_pct),
        })

    candidates.sort(key=lambda c: (
        0 if c["liqOK"] else 1,
        -c["aiConfidence"],
        -(c["annualizedReturnPct"] or 0)
    ))

    error = None
    if not candidates:
        error = "no matching contracts found; try wider DTE/delta filters or wait for provider rate limit reset"

    out = {
        "symbol": sym,
        "type": opt_type,
        "spot": round(S, 2),
        "provider": provider,
        "providerWarnings": provider_warnings,
        "riskFreeRate": round(r, 4),
        "filter": {
            "dte_min": dte_min,
            "dte_max": dte_max,
            "delta_min": delta_min,
            "delta_max": delta_max,
            "supportedDTE": DTE_BUCKETS,
        },
        "candidates": candidates[:top_n],
        "totalFound": len(candidates),
        "error": error,
    }

    return _cache_options(cache_key, out)

# ── History (daily closes for sparklines) ──────────────
def fetch_history(syms, days=90):
    """Returns {sym: [close_0, close_1, ...]} for the last N trading days."""
    syms = syms or WATCH
    days = max(20, min(days, 365))
    now = time.time()

    out = {}
    fresh = []
    for sym in syms:
        c = _hist_cache.get((sym, days))
        if c and (now - c["t"] < HIST_TTL):
            out[sym] = c["data"]
        else:
            fresh.append(sym)

    if fresh:
        import yfinance as yf
        import pandas as pd
        # Fetch enough trading days; use period in days × 1.6 to allow for weekends/holidays
        period_days = max(int(days * 1.6), 60)
        df = yf.download(" ".join(fresh), period=f"{period_days}d", interval="1d",
                         group_by="ticker", threads=True, progress=False, auto_adjust=True)
        for sym in fresh:
            try:
                d = (df[sym].dropna() if isinstance(df.columns, pd.MultiIndex) else df.dropna())
                closes = [round(float(x), 2) for x in d["Close"].tail(days).tolist()]
                out[sym] = closes
                _hist_cache[(sym, days)] = {"t": now, "data": closes}
            except Exception as e:
                print(f"  WARN history {sym}: {e}", flush=True)
                out[sym] = []
    return out

# ── Earnings calendar ──────────────────────────────────
def fetch_earnings(syms):
    """For each ticker, returns next upcoming earnings date (ISO) and DTE, or None."""
    syms = syms or WATCH
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    today_t = time.mktime(time.strptime(today, "%Y-%m-%d"))

    out = {}
    fresh = []
    for sym in syms:
        c = _earn_cache.get(sym)
        if c and (now - c["t"] < EARN_TTL):
            out[sym] = c["data"]
        else:
            fresh.append(sym)

    if fresh:
        import yfinance as yf
        for sym in fresh:
            data = {"nextDate": None, "dte": None, "source": None}
            try:
                tk = yf.Ticker(sym)
                # Try earnings_dates (preferred — DataFrame with future + past)
                try:
                    df = tk.earnings_dates
                    if df is not None and not df.empty:
                        future = df.index[df.index > __import__("pandas").Timestamp.now(tz=df.index.tz)]
                        if len(future) > 0:
                            next_dt = future.min()
                            iso = next_dt.strftime("%Y-%m-%d")
                            try:
                                dte = int(round((time.mktime(time.strptime(iso, "%Y-%m-%d")) - today_t) / 86400.0))
                            except Exception:
                                dte = None
                            data = {"nextDate": iso, "dte": dte, "source": "earnings_dates"}
                except Exception:
                    pass
                # Fallback to calendar
                if not data["nextDate"]:
                    try:
                        cal = tk.calendar
                        if cal:
                            ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
                            if ed:
                                # Could be a list, datetime, or pd.Timestamp
                                if isinstance(ed, (list, tuple)) and ed:
                                    ed = ed[0]
                                iso = ed.strftime("%Y-%m-%d") if hasattr(ed, "strftime") else str(ed)[:10]
                                try:
                                    dte = int(round((time.mktime(time.strptime(iso, "%Y-%m-%d")) - today_t) / 86400.0))
                                except Exception:
                                    dte = None
                                data = {"nextDate": iso, "dte": dte, "source": "calendar"}
                    except Exception:
                        pass
            except Exception as e:
                print(f"  WARN earnings {sym}: {e}", flush=True)
            _earn_cache[sym] = {"t": now, "data": data}
            out[sym] = data
    return out

# ── CNN Fear & Greed ───────────────────────────────────
def fetch_fg():
    now = time.time()
    if _fg_cache["data"] and (now - _fg_cache["t"] < FG_TTL):
        return _fg_cache["data"]
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.cnn.com/",
        })
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.load(r)
        fg     = data.get("fear_and_greed", {})
        score  = round(float(fg.get("score", 0)))
        rating = str(fg.get("rating", "neutral")).title()
        prev   = round(float(fg.get("previous_close", 0)))
        result = {"score": score, "label": rating, "prev": prev,
                  "direction": "improving" if score > prev else "deteriorating" if score < prev else "flat"}
        _fg_cache.update(t=now, data=result)
        return result
    except Exception as e:
        print(f"  WARN F&G: {e}", flush=True)
        return None

# ── HTTP Handler ───────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs   = parse_qs(parsed.query)

        # Public routes (no auth required): "/", "/index.html", "/health"
        if path in ("/", "/index.html"):
            try:
                body = open(HTML_FILE, "rb").read()
                self._send(200, "text/html; charset=utf-8", body)
            except FileNotFoundError:
                self._send(404, "text/plain", b"index.html not found")
            return
        if path == "/health":
            self._send(200, "application/json", json.dumps({"ok": True, "authRequired": bool(AUTH_HASH)}).encode())
            return

        # All endpoints below require auth (when AUTH_TOKEN_HASH is set)
        ok, status = _auth_state(self)
        if path == "/auth":
            payload = {"ok": ok, "status": status, "authRequired": bool(AUTH_HASH)}
            code = 200 if ok else (429 if status == "rate_limited" else 401)
            self._send(code, "application/json", json.dumps(payload).encode())
            return
        if not ok:
            code = 429 if status == "rate_limited" else 401
            self._send(code, "application/json", json.dumps({"error": status}).encode())
            return

        if path == "/quotes":
            syms = parse_symbols(qs, ALL)
            print(f"  [{time.strftime('%H:%M:%S')}] /quotes ({len(syms)}) ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_quotes(syms)
            n = len(data["quoteResponse"]["result"])
            print(f"{n} syms in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

        elif path == "/technicals":
            syms = parse_symbols(qs, WATCH)
            print(f"  [{time.strftime('%H:%M:%S')}] /technicals ({len(syms)}) ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_tech(syms)
            print(f"{sum(1 for v in data.values() if v)} ok in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

        elif path == "/forecast":
            syms = parse_symbols(qs, WATCH)
            print(f"  [{time.strftime('%H:%M:%S')}] /forecast ({len(syms)}) ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_forecast(syms)
            print(f"{sum(1 for v in data.values() if v)} ok in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

        elif path == "/earnings":
            syms = parse_symbols(qs, WATCH)
            print(f"  [{time.strftime('%H:%M:%S')}] /earnings ({len(syms)}) ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_earnings(syms)
            n = sum(1 for v in data.values() if v.get("nextDate"))
            print(f"{n} dated in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

        elif path == "/history":
            syms = parse_symbols(qs, WATCH)
            try:
                days = int(qs.get("days", ["90"])[0])
            except ValueError:
                days = 90
            print(f"  [{time.strftime('%H:%M:%S')}] /history ({len(syms)},{days}d) ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_history(syms, days)
            print(f"ok in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

        elif path == "/options":
            sym = (qs.get("symbol", [""])[0] or "").strip().upper()
            opt = (qs.get("type",   ["puts"])[0] or "puts").lower()
            if opt not in ("puts", "calls"):
                self._send(400, "application/json", b'{"error":"type must be puts or calls"}'); return
            if not _SYM_RE.match(sym):
                self._send(400, "application/json", b'{"error":"invalid symbol"}'); return
            try:
                dte_min  = int(qs.get("dte_min",  ["30" if opt == "puts" else "21"])[0])
                dte_max  = int(qs.get("dte_max",  ["45" if opt == "puts" else "35"])[0])
                d_min    = float(qs.get("delta_min", ["0.15" if opt == "puts" else "0.20"])[0])
                d_max    = float(qs.get("delta_max", ["0.30" if opt == "puts" else "0.35"])[0])
                top_n    = int(qs.get("top",      ["5"])[0])
            except ValueError:
                self._send(400, "application/json", b'{"error":"bad numeric param"}'); return
            print(f"  [{time.strftime('%H:%M:%S')}] /options {sym} {opt} d{d_min}-{d_max} dte{dte_min}-{dte_max} ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_options(sym, opt, dte_min, dte_max, d_min, d_max, top_n)
            print(f"{len(data.get('candidates', []))} cand in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

        elif path == "/feargreed":
            data = fetch_fg()
            self._send(200, "application/json", json.dumps(data or {}).encode())

        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

# Threaded server so /technicals doesn't block /quotes
class ThreadedServer(HTTPServer):
    def process_request(self, request, client_address):
        threading.Thread(target=self._handle, args=(request, client_address), daemon=True).start()
    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        finally:
            self.shutdown_request(request)

# ── Main ───────────────────────────────────────────────
if __name__ == "__main__":
    is_cloud = bool(os.environ.get("PORT"))
    print(f"\n  NandaEdge Server v3.5 — {HOST}:{PORT}", flush=True)

    if not is_cloud:
        if not port_free(PORT):
            print(f"  Port {PORT} in use — killing...", flush=True)
            kill_port(PORT)
        if not port_free(PORT):
            print(f"  ERROR: port {PORT} still busy. Run: kill $(lsof -ti:{PORT})", flush=True)
            sys.exit(1)

    base = f"http://{HOST}:{PORT}" if not is_cloud else f"http://0.0.0.0:{PORT}"
    print(f"  Dashboard   → {base}/", flush=True)
    print(f"  Quotes      → {base}/quotes", flush=True)
    print(f"  Technicals  → {base}/technicals", flush=True)
    print(f"  Fear/Greed  → {base}/feargreed", flush=True)
    print(f"  Press Ctrl+C to stop.\n", flush=True)

    server = ThreadedServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
