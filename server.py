#!/usr/bin/env python3
"""
NandaEdge Data Server — v3.6
  Dashboard   → /
  Quotes      → /quotes        ?symbols=NVDA,TSLA,...   realtime quotes
  Candles     → /candles       ?symbols=NVDA&interval=5m&period=1d intraday OHLCV
  Day Trade   → /daytrade      ?symbols=NVDA,AMD,IONQ candle-based read-only setups
  Technicals  → /technicals    ?symbols=...             EMA/RSI/MACD/BB/ATR/VWAP
  Forecast    → /forecast      ?symbols=...             GBM projections (1w..5y)
  Fear/Greed  → /feargreed                              CNN proxy
  Cloud-ready: reads PORT/HOST from env; binds 0.0.0.0 when PORT is set.
"""
import os, sys, signal, time, json, math, socket, warnings, urllib.request, urllib.error, threading, hashlib, random, subprocess, gc, base64
from datetime import date, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from zoneinfo import ZoneInfo

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DAYTRADE_HISTORY_FILE = os.path.join(BASE_DIR, "daytrade_history.json")
WHATSAPP_STOCK_FEED_FILE = os.environ.get(
    "WHATSAPP_STOCK_FEED_FILE",
    os.path.expanduser("~/myprojects/edge-advisor-local/whatsapp-feeds/BuyAlertsContrbutingAndPaidMembers/whatsapp_stock_feed.json"),
)
WHATSAPP_AUTO_SCRIPT = os.path.join(BASE_DIR, "scripts", "whatsapp_stock_feed_auto.py")
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

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
SCHWAB_TOKEN_URL     = "https://api.schwabapi.com/v1/oauth/token"
_SCHWAB_TOKEN_LOCK   = threading.Lock()
_SCHWAB_REFRESH_LAST = 0.0

# Optional legacy/secondary provider. If absent, /options skips Tradier safely.
TRADIER_ACCESS_TOKEN = (os.environ.get("TRADIER_ACCESS_TOKEN", "") or "").strip()
TRADIER_BASE_URL     = (os.environ.get("TRADIER_BASE_URL", "https://api.tradier.com") or "").rstrip("/")

WATCH = ["NVDA","TSLA","PLTR","AMD","MU","CRWD","INTC","IONQ","RGTI"]
SPECULATIVE_UNIVERSE = [
    "NVDA","AMD","IONQ","RGTI","PLTR","TSLA","MU","SMCI",
    "COIN","MARA","RIOT","SOUN","AI","QBTS","RKLB","SOFI"
]
CAP_CATEGORY_UNIVERSES = {
    # Curated active-trader lanes. These lists define the scan pool; ranking is
    # still driven by live 1-minute candle range, ATR, volume pulse, and trend.
    "small": ["RGTI", "SOUN", "QBTS", "BBAI", "KULR", "LAES", "LUNR", "JOBY"],
    "mid": ["IONQ", "RKLB", "SOFI", "HOOD", "AFRM", "RBLX", "MARA", "RIOT"],
    "large": ["NVDA", "AMD", "TSLA", "PLTR", "MU", "SMCI", "COIN", "AVGO"],
}
# Keep cloud memory stable. The full speculative universe can be expanded
# later, but normal dashboard refreshes should not batch-load every hot ticker.
SPECULATIVE_SCAN_LIMIT = int(os.environ.get("SPECULATIVE_SCAN_LIMIT", "5") or "5")
CAP_CATEGORY_SCAN_LIMIT = int(os.environ.get("CAP_CATEGORY_SCAN_LIMIT", "5") or "5")
DAYTRADE_HISTORY_DAYS = int(os.environ.get("DAYTRADE_HISTORY_DAYS", "7") or "7")
CANDLE_RESPONSE_LIMIT = int(os.environ.get("CANDLE_RESPONSE_LIMIT", "120") or "120")
DAYTRADE_CHART_LIMIT = int(os.environ.get("DAYTRADE_CHART_LIMIT", "32") or "32")
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
_candle_cache= {}                     # (symbols, interval, period) -> {"t": float, "data": dict}
_daytrade_cache = {}                  # (symbols, interval, period) -> {"t": float, "data": dict}
_spec_daytrade_cache = {}             # (limit, validate) -> {"t": float, "data": dict}
_earn_cache  = {}                     # sym -> {"t": float, "data": dict}
_rate_cache  = {"t": 0, "rate": 0.045}  # risk-free rate from ^TNX
_fg_cache    = {"t": 0, "data": None}
TECH_TTL = 300                        # 5 min
FCST_TTL = 3600                       # 1 hour — forecasts don't need to update often
OPTS_TTL = 600                        # 10 min — options chains move fast but pulling is expensive
HIST_TTL = 1800                       # 30 min — daily closes don't change intraday
CANDLE_TTL = 45                       # short TTL for intraday candles
DAYTRADE_TTL = 60                     # day-trade plans refresh with live candle reads
SPEC_DAYTRADE_TTL = int(os.environ.get("SPEC_DAYTRADE_TTL", "90") or "90")
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

# ── Intraday candles + read-only day-trade setups ───────
def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        if hasattr(v, "item"):
            v = v.item()
        if isinstance(v, float) and math.isnan(v):
            return default
        return float(v)
    except Exception:
        return default


def _pacific_time_label(ts):
    try:
        if hasattr(ts, "to_pydatetime"):
            dt = ts.to_pydatetime()
        else:
            dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        pt = dt.astimezone(PACIFIC_TZ)
        return pt.strftime("%b %-d, %-I:%M %p PT")
    except Exception:
        return None


def _touch_times(d, bias, entry, exit_value, stop):
    entry_ts = exit_ts = invalid_ts = None
    entry_seen = False
    for ts, row in d.iterrows():
        hi = _safe_float(row.get("High"))
        lo = _safe_float(row.get("Low"))
        if hi is None or lo is None:
            continue
        if not entry_seen:
            if bias == "SHORT":
                entry_seen = lo <= entry
            else:
                entry_seen = hi >= entry
            if entry_seen:
                entry_ts = ts
        if entry_seen:
            if exit_ts is None:
                if bias == "SHORT" and lo <= exit_value:
                    exit_ts = ts
                elif bias != "SHORT" and hi >= exit_value:
                    exit_ts = ts
            if invalid_ts is None:
                if bias == "SHORT" and hi >= stop:
                    invalid_ts = ts
                elif bias != "SHORT" and lo <= stop:
                    invalid_ts = ts
    last_ts = d.index[-1] if len(d.index) else None
    return {
        "signalTimePST": _pacific_time_label(last_ts),
        "entryTimePST": _pacific_time_label(entry_ts),
        "exitTimePST": _pacific_time_label(exit_ts),
        "invalidTimePST": _pacific_time_label(invalid_ts),
    }


def _trim_cache(cache, limit):
    """Keep short-lived in-memory caches from growing on cloud instances."""
    if len(cache) <= limit:
        return
    stale = sorted(cache.items(), key=lambda item: item[1].get("t", 0))
    for key, _ in stale[:max(0, len(cache) - limit)]:
        cache.pop(key, None)


def _fetch_intraday_frames(syms, interval="5m", period="1d"):
    import yfinance as yf
    import pandas as pd

    symbols = [s for s in syms if _SYM_RE.match(s)]
    if not symbols:
        return {}
    try:
        df = yf.download(" ".join(symbols), period=period, interval=interval,
                         group_by="ticker", threads=False, progress=False,
                         auto_adjust=False, prepost=False)
    except Exception as e:
        print(f"  WARN candles batch fallback: {e}", flush=True)
        df = None
    frames = {}
    if df is not None:
        for sym in symbols:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    d = df[sym].dropna()
                else:
                    d = df.dropna()
                if d.empty:
                    continue
                frames[sym] = d
            except Exception as e:
                print(f"  WARN candles frame {sym}: {e}", flush=True)
    missing = [sym for sym in symbols if sym not in frames]
    for sym in missing:
        try:
            d = yf.download(sym, period=period, interval=interval,
                            group_by="ticker", threads=False, progress=False,
                            auto_adjust=False, prepost=False).dropna()
            if not d.empty:
                frames[sym] = d
        except Exception as e:
            print(f"  WARN candles single {sym}: {e}", flush=True)
    return frames


def fetch_candles(syms=None, interval="5m", period="1d"):
    syms = syms or ["NVDA", "AMD", "IONQ"]
    interval = interval if interval in {"1m","2m","5m","15m","30m","60m","1h","1d"} else "5m"
    period = period if period in {"1d","5d","1mo","3mo","6mo","1y"} else "1d"
    key = (",".join(syms), interval, period)
    now = time.time()
    c = _candle_cache.get(key)
    if c and now - c["t"] < CANDLE_TTL:
        return c["data"]

    frames = _fetch_intraday_frames(syms, interval, period)
    out = {}
    for sym, d in frames.items():
        candles = []
        for ts, row in d.tail(CANDLE_RESPONSE_LIMIT).iterrows():
            candles.append({
                "time": str(ts),
                "open": round(_safe_float(row.get("Open"), 0), 4),
                "high": round(_safe_float(row.get("High"), 0), 4),
                "low": round(_safe_float(row.get("Low"), 0), 4),
                "close": round(_safe_float(row.get("Close"), 0), 4),
                "volume": int(_safe_float(row.get("Volume"), 0) or 0),
            })
        out[sym] = {"symbol": sym, "interval": interval, "period": period, "candles": candles}
    _candle_cache[key] = {"t": now, "data": out}
    _trim_cache(_candle_cache, 12)
    return out


def _daytrade_for_frame(sym, d, interval):
    import pandas as pd

    d = d.dropna().copy()
    if len(d) < 20:
        return None
    close = d["Close"].astype(float)
    high = d["High"].astype(float)
    low = d["Low"].astype(float)
    open_ = d["Open"].astype(float)
    vol = d["Volume"].astype(float)
    last = float(close.iloc[-1])
    day_open = float(open_.iloc[0])
    day_high = float(high.max())
    day_low = float(low.min())
    ema9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    typical = (high + low + close) / 3
    vwap = float((typical * vol).cumsum().iloc[-1] / max(vol.cumsum().iloc[-1], 1))
    prev_c = close.shift(1)
    tr = pd.concat([high-low, (high-prev_c).abs(), (low-prev_c).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean()) if len(tr.dropna()) >= 14 else max(day_high - day_low, last * 0.01)
    atr = max(atr, last * 0.003)
    vol_avg = float(vol.tail(20).mean()) if len(vol) >= 20 else float(vol.mean())
    vol_now = float(vol.tail(3).mean())
    vol_ratio = vol_now / vol_avg if vol_avg else 1.0

    bullish = last > vwap and ema9 >= ema20 and last >= day_open
    bearish = last < vwap and ema9 < ema20 and last < day_open
    if bullish:
        bias = "LONG"
        setup = "VWAP pullback / breakout continuation"
        entry = max(last, vwap + 0.10 * atr)
        stop = min(vwap - 0.45 * atr, entry - 1.15 * atr)
        risk = max(entry - stop, 0.01)
        exit_value = entry + 1.6 * risk
        target2 = entry + 2.4 * risk
    elif bearish:
        bias = "SHORT"
        setup = "VWAP rejection / downside continuation"
        entry = min(last, vwap - 0.10 * atr)
        stop = max(vwap + 0.45 * atr, entry + 1.15 * atr)
        risk = max(stop - entry, 0.01)
        exit_value = entry - 1.6 * risk
        target2 = entry - 2.4 * risk
    else:
        bias = "WAIT"
        setup = "No clean candle edge"
        entry = max(day_high + 0.10 * atr, last + 0.35 * atr)
        stop = max(day_low, last - 1.10 * atr)
        risk = max(entry - stop, 0.01)
        exit_value = entry + 1.35 * risk
        target2 = entry + 2.0 * risk

    score = 50
    if last > vwap: score += 10
    if ema9 > ema20: score += 10
    if vol_ratio >= 1.2: score += 10
    if abs(last - vwap) / max(last, 1) < 0.018: score += 6
    if day_high > day_low: score += 4
    if bias == "WAIT": score = min(score, 58)
    confidence = max(35, min(92, round(score)))
    trade_rating_value = trade_rating(confidence)
    why = []
    why.append("Price is above intraday VWAP" if last > vwap else "Price is below intraday VWAP")
    why.append("EMA9 is above EMA20" if ema9 > ema20 else "EMA9 is below EMA20")
    why.append(f"Volume pulse {vol_ratio:.1f}x recent candle average")
    why.append("Entry/exit are derived from VWAP, ATR, and intraday trend state")

    candles = []
    for ts, row in d.tail(DAYTRADE_CHART_LIMIT).iterrows():
        candles.append({
            "time": str(ts),
            "timePST": _pacific_time_label(ts),
            "open": round(_safe_float(row.get("Open"), 0), 4),
            "high": round(_safe_float(row.get("High"), 0), 4),
            "low": round(_safe_float(row.get("Low"), 0), 4),
            "close": round(_safe_float(row.get("Close"), 0), 4),
            "volume": int(_safe_float(row.get("Volume"), 0) or 0),
        })
    times = _touch_times(d, bias, entry, exit_value, stop)

    return {
        "symbol": sym,
        "interval": interval,
        "last": round(last, 2),
        "bias": bias,
        "setup": setup,
        "entryValue": round(entry, 2),
        "exitValue": round(exit_value, 2),
        "target2": round(target2, 2),
        "stopLoss": round(stop, 2),
        "riskPerShare": round(risk, 2),
        "vwap": round(vwap, 2),
        "ema9": round(ema9, 2),
        "ema20": round(ema20, 2),
        "atr": round(atr, 2),
        "dayHigh": round(day_high, 2),
        "dayLow": round(day_low, 2),
        "volumeRatio": round(vol_ratio, 2),
        "aiConfidence": confidence,
        "tradeRating": trade_rating_value,
        "exitRule": "Take partial profits at exit value; trail remainder near EMA9/VWAP; exit immediately if stop-loss breaks.",
        "why": why,
        "candles": candles,
        **times,
    }


def fetch_daytrade(syms=None, interval="5m", period="1d"):
    syms = syms or ["NVDA", "AMD", "IONQ"]
    interval = interval if interval in {"1m","2m","5m","15m","30m","60m","1h"} else "5m"
    period = period if period in {"1d","5d"} else "1d"
    key = (",".join(syms), interval, period)
    now = time.time()
    c = _daytrade_cache.get(key)
    if c and now - c["t"] < DAYTRADE_TTL:
        return c["data"]

    frames = _fetch_intraday_frames(syms, interval, period)
    results = []
    for sym in syms:
        try:
            item = _daytrade_for_frame(sym, frames.get(sym), interval) if sym in frames else None
            if item:
                results.append(item)
        except Exception as e:
            print(f"  WARN daytrade {sym}: {e}", flush=True)
            results.append({"symbol": sym, "error": str(e)})
    data = {
        "symbols": syms,
        "interval": interval,
        "period": period,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "disclaimer": "Read-only educational day-trade analysis from intraday candles. Not financial advice and not an order instruction.",
        "results": results,
    }
    _daytrade_cache[key] = {"t": now, "data": data}
    _trim_cache(_daytrade_cache, 8)
    return data


def _history_load():
    try:
        with open(DAYTRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"days": {}}
    except Exception:
        return {"days": {}}


def _history_save(data):
    try:
        days = data.setdefault("days", {})
        cutoff = date.today() - timedelta(days=DAYTRADE_HISTORY_DAYS)
        for k in list(days.keys()):
            try:
                if date.fromisoformat(k) < cutoff:
                    del days[k]
            except Exception:
                del days[k]
        for day_payload in days.values():
            day_payload["predictions"] = [
                _compact_setup(p) for p in day_payload.get("predictions", [])
            ]
        with open(DAYTRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  WARN daytrade history save: {e}", flush=True)


def _compact_setup(setup, keep_candles=False):
    """Persist only the decision fields; candle arrays stay response-only."""
    fields = [
        "symbol", "interval", "last", "bias", "setup", "entryValue", "exitValue",
        "target2", "stopLoss", "riskPerShare", "vwap", "ema9", "ema20", "atr",
        "dayHigh", "dayLow", "volumeRatio", "aiConfidence", "tradeRating",
        "exitRule", "why", "speculativeScore", "alertStatus", "alertReason",
        "modelRule", "signalTimePST", "entryTimePST", "exitTimePST",
        "invalidTimePST", "outcome", "outcomeReason", "realized",
        "whatWentWrong",
    ]
    compact = {k: setup[k] for k in fields if k in setup}
    if keep_candles and "candles" in setup:
        compact["candles"] = setup["candles"]
    return compact


def _compact_live_setup(setup):
    """Return lightweight live scanner rows; tables do not need candle arrays."""
    fields = [
        "symbol", "interval", "last", "bias", "setup", "entryValue", "exitValue",
        "stopLoss", "vwap", "atr", "dayHigh", "dayLow", "volumeRatio",
        "aiConfidence", "tradeRating", "exitRule", "why", "speculativeScore",
        "variancePct", "alertStatus", "alertReason", "modelRule", "capCategory",
        "signalTimePST", "entryTimePST", "exitTimePST", "invalidTimePST",
    ]
    return {k: setup[k] for k in fields if k in setup}


def _compact_history(history):
    compact = {"days": {}}
    for day_key, payload in history.get("days", {}).items():
        compact["days"][day_key] = {
            "generatedAt": payload.get("generatedAt"),
            "method": payload.get("method"),
            "predictions": [_compact_setup(p) for p in payload.get("predictions", [])],
        }
    return compact


def _setup_status(setup, last=None, high=None, low=None):
    bias = setup.get("bias")
    entry = _safe_float(setup.get("entryValue"))
    exit_value = _safe_float(setup.get("exitValue"))
    stop = _safe_float(setup.get("stopLoss"))
    if bias == "LONG":
        if high is not None and exit_value is not None and high >= exit_value:
            return "exit_hit", "Exit target reached"
        if low is not None and stop is not None and low <= stop:
            return "invalid", "Stop-loss invalidated setup"
        if last is not None and stop is not None and last <= stop:
            return "invalid", "Current price broke stop-loss"
        return "active", "Waiting for entry/exit confirmation"
    if bias == "SHORT":
        if low is not None and exit_value is not None and low <= exit_value:
            return "exit_hit", "Exit target reached"
        if high is not None and stop is not None and high >= stop:
            return "invalid", "Stop-loss invalidated setup"
        if last is not None and stop is not None and last >= stop:
            return "invalid", "Current price broke stop-loss"
        return "active", "Waiting for entry/exit confirmation"
    if entry is not None and high is not None and high >= entry:
        return "triggered", "Breakout entry triggered"
    if stop is not None and low is not None and low <= stop:
        return "invalid", "Setup invalidated before entry"
    return "watch", "No clean entry yet"


def _speculative_score(setup):
    last = _safe_float(setup.get("last"), 0) or 0
    day_high = _safe_float(setup.get("dayHigh"), last) or last
    day_low = _safe_float(setup.get("dayLow"), last) or last
    atr = _safe_float(setup.get("atr"), 0) or 0
    vol_ratio = _safe_float(setup.get("volumeRatio"), 1) or 1
    confidence = _safe_float(setup.get("aiConfidence"), 0) or 0
    range_pct = ((day_high - day_low) / max(last, 1)) * 100
    atr_pct = (atr / max(last, 1)) * 100
    bias_bonus = 8 if setup.get("bias") in ("LONG", "SHORT") else 0
    return round(range_pct * 2.2 + atr_pct * 4.0 + min(vol_ratio, 5) * 8 + confidence * 0.35 + bias_bonus, 2)


def _candle_variance_pct(setup):
    last = _safe_float(setup.get("last"), 0) or 0
    day_high = _safe_float(setup.get("dayHigh"), last) or last
    day_low = _safe_float(setup.get("dayLow"), last) or last
    if not last:
        return 0
    return round(((day_high - day_low) / max(last, 1)) * 100, 2)


def _realized_for_symbol(frames, sym, day_key):
    d = frames.get(sym)
    if d is None or d.empty:
        return None
    try:
        rows = d[d.index.strftime("%Y-%m-%d") == day_key]
        if rows.empty:
            return None
        return {
            "high": round(float(rows["High"].max()), 4),
            "low": round(float(rows["Low"].min()), 4),
            "close": round(float(rows["Close"].iloc[-1]), 4),
            "candles": int(len(rows)),
        }
    except Exception:
        return None


def _update_prediction_history(history, today_key, frames_5d):
    days = history.setdefault("days", {})
    for day_key, day_payload in list(days.items()):
        if day_key >= today_key:
            continue
        for setup in day_payload.get("predictions", []):
            if setup.get("outcome") in ("exit_hit", "invalid", "triggered"):
                continue
            realized = _realized_for_symbol(frames_5d, setup.get("symbol", ""), day_key)
            if not realized:
                continue
            status, reason = _setup_status(setup, high=realized["high"], low=realized["low"])
            setup["outcome"] = status
            setup["outcomeReason"] = reason
            setup["realized"] = realized
            setup["whatWentWrong"] = _what_went_wrong(setup, status, realized)
    return history


def _what_went_wrong(setup, status, realized):
    if status == "exit_hit":
        return "Worked: price reached planned exit before invalidation."
    if status == "invalid":
        bias = setup.get("bias")
        if bias == "LONG":
            return "Failed: candle low breached stop; require stronger VWAP hold and volume confirmation next run."
        if bias == "SHORT":
            return "Failed: candle high breached stop; require cleaner VWAP rejection and weaker EMA alignment next run."
        return "Failed: watch setup invalidated before clean entry trigger."
    return "Unresolved: neither exit nor stop was reached in the comparison window."


def _learning_notes(history):
    notes = []
    recent = []
    for day_payload in history.get("days", {}).values():
        recent.extend(day_payload.get("predictions", []))
    invalids = [x for x in recent if x.get("outcome") == "invalid"]
    exits = [x for x in recent if x.get("outcome") == "exit_hit"]
    if invalids:
        notes.append("Tighten entries after invalidations: require VWAP agreement plus EMA9/20 alignment before active alert.")
    if exits and len(exits) >= len(invalids):
        notes.append("Current ATR exit model is working better than stops over the retained week.")
    if not notes:
        notes.append("Not enough completed history yet; keep collecting one-minute predictions for calibration.")
    notes.append("Success is not guaranteed; this is a strict-filter educational model, not a 90% promise.")
    return notes[:4]


def fetch_speculative_daytrade(limit=5, validate_history=False):
    interval, period = "1m", "1d"
    today_key = date.today().isoformat()
    limit = max(1, min(limit, 8))
    cache_key = (limit, bool(validate_history))
    now = time.time()
    cached = _spec_daytrade_cache.get(cache_key)
    if cached and now - cached["t"] < SPEC_DAYTRADE_TTL:
        return cached["data"]

    def build_setups(universe, category=None):
        scan_limit = max(limit, min(CAP_CATEGORY_SCAN_LIMIT if category else SPECULATIVE_SCAN_LIMIT, len(universe)))
        scan_universe = universe[:scan_limit]
        frames = _fetch_intraday_frames(scan_universe, interval, period)
        rows = []
        for sym in scan_universe:
            try:
                if sym not in frames:
                    continue
                setup = _daytrade_for_frame(sym, frames[sym], interval)
                if not setup:
                    continue
                setup["capCategory"] = category or "speculative"
                setup["variancePct"] = _candle_variance_pct(setup)
                setup["speculativeScore"] = _speculative_score(setup)
                status, reason = _setup_status(setup, last=setup.get("last"))
                setup["alertStatus"] = status
                setup["alertReason"] = reason
                setup["modelRule"] = "1m candles: rank range% + ATR% + volume pulse + VWAP/EMA alignment; remove active alert when stop invalidates."
                rows.append(setup)
            except Exception as e:
                print(f"  WARN speculative setup {sym}: {e}", flush=True)
        rows.sort(key=lambda x: (x.get("variancePct", 0), x.get("alertStatus") == "active", x.get("speculativeScore", 0)), reverse=True)
        del frames
        gc.collect()
        return rows[:limit], scan_universe

    categories = {}
    scanned_universe = []
    for key, universe in CAP_CATEGORY_UNIVERSES.items():
        rows, scan_universe = build_setups(universe, key)
        categories[key] = {
            "label": {"small": "Small Cap", "mid": "Mid Cap", "large": "Large Cap"}.get(key, key.title()),
            "universe": scan_universe,
            "results": [_compact_live_setup(x) for x in rows],
        }
        scanned_universe.extend(scan_universe)

    top = []
    for key in ("small", "mid", "large"):
        top.extend(categories.get(key, {}).get("results", []))
    top.sort(key=lambda x: (x.get("variancePct", 0), x.get("alertStatus") == "active", x.get("speculativeScore", 0)), reverse=True)

    history = _history_load()
    if validate_history:
        history_symbols = sorted({
            p.get("symbol", "")
            for payload in history.get("days", {}).values()
            for p in payload.get("predictions", [])
            if p.get("symbol")
        }) or [p.get("symbol") for p in top if p.get("symbol")]
        frames_5d = _fetch_intraday_frames(history_symbols[:8], "1m", "5d")
        history = _update_prediction_history(history, today_key, frames_5d)
    history.setdefault("days", {})[today_key] = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "NandaEdge speculative 1m candle skill",
        "predictions": [_compact_setup(x) for x in top],
    }
    _history_save(history)

    lightweight_top = [_compact_live_setup(x) for x in top]
    active_alerts = [x for x in lightweight_top if x.get("alertStatus") not in ("invalid", "exit_hit")]
    compact_history = _compact_history(history)
    data = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "interval": interval,
        "period": period,
        "universe": scanned_universe,
        "results": lightweight_top,
        "categories": categories,
        "activeAlerts": active_alerts,
        "history": compact_history,
        "learningNotes": _learning_notes(history),
        "disclaimer": "Read-only educational scanner. No live trading, no order routing, and no guaranteed success rate.",
    }
    _spec_daytrade_cache[cache_key] = {"t": time.time(), "data": data}
    _trim_cache(_spec_daytrade_cache, 4)
    return data

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


def _schwab_basic_auth():
    raw = f"{SCHWAB_APP_KEY}:{SCHWAB_APP_SECRET}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _schwab_token_request(payload, use_basic=True):
    data = dict(payload)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if use_basic:
        headers["Authorization"] = _schwab_basic_auth()
    else:
        data["client_id"] = SCHWAB_APP_KEY
        data["client_secret"] = SCHWAB_APP_SECRET
    req = urllib.request.Request(
        SCHWAB_TOKEN_URL,
        data=urlencode(data).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _refresh_schwab_access_token():
    """
    Refresh Schwab market-data access in memory for live server uptime.

    Tokens remain server-side. This does not request account/trading scopes and
    does not persist new token values to source files. Render should still keep
    SCHWAB_REFRESH_TOKEN in Environment for restarts.
    """
    global SCHWAB_ACCESS_TOKEN, SCHWAB_REFRESH_TOKEN, _SCHWAB_REFRESH_LAST
    if not (SCHWAB_APP_KEY and SCHWAB_APP_SECRET and SCHWAB_REFRESH_TOKEN):
        return False
    with _SCHWAB_TOKEN_LOCK:
        now = time.time()
        if now - _SCHWAB_REFRESH_LAST < 20:
            return bool(SCHWAB_ACCESS_TOKEN)
        _SCHWAB_REFRESH_LAST = now
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": SCHWAB_REFRESH_TOKEN,
        }
        try:
            token_payload = _schwab_token_request(payload, use_basic=True)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if "invalid_client" not in detail:
                raise RuntimeError(f"Schwab token refresh failed: HTTP {e.code}")
            token_payload = _schwab_token_request(payload, use_basic=False)
        access = (token_payload.get("access_token") or "").strip()
        refresh = (token_payload.get("refresh_token") or "").strip()
        if not access:
            raise RuntimeError("Schwab token refresh returned no access token")
        SCHWAB_ACCESS_TOKEN = access
        if refresh:
            SCHWAB_REFRESH_TOKEN = refresh
        return True


def _schwab_http_json(url):
    if not SCHWAB_ACCESS_TOKEN:
        raise RuntimeError("Schwab not configured: missing SCHWAB_ACCESS_TOKEN")
    headers = {
        "Authorization": f"Bearer {SCHWAB_ACCESS_TOKEN}",
        "Accept": "application/json",
    }
    try:
        return _http_json(url, headers=headers)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
        if not _refresh_schwab_access_token():
            raise RuntimeError("Schwab token expired and refresh token is not configured")
        return _http_json(url, headers={
            "Authorization": f"Bearer {SCHWAB_ACCESS_TOKEN}",
            "Accept": "application/json",
        })


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
    spot = 0.0
    try:
        spot_info = tk.fast_info
        spot = float(spot_info.last_price or 0)
    except Exception:
        spot = 0.0
    if spot <= 0:
        try:
            hist = tk.history(period="5d", interval="1d", auto_adjust=False)
            if hist is not None and not hist.empty:
                spot = float(hist["Close"].dropna().iloc[-1])
        except Exception:
            spot = 0.0
    return tk, spot


def _fetch_schwab_option_rows(sym, opt_type, dte_min, dte_max):
    """
    Schwab Market Data integration for options chains only.

    OAuth setup note:
    1. Create a Schwab developer app and set SCHWAB_APP_KEY,
       SCHWAB_APP_SECRET, and SCHWAB_CALLBACK_URL outside source control.
    2. Complete Schwab's authorization-code flow externally.
    3. Store only the resulting SCHWAB_ACCESS_TOKEN/SCHWAB_REFRESH_TOKEN in
       Render Environment or local .env.
    4. The live server may refresh an expired market-data access token in
       memory using SCHWAB_REFRESH_TOKEN, but it never requests account scopes,
       places trades, or exposes broker tokens to the browser.
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
    data = _schwab_http_json(url)

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

def load_whatsapp_stock_feed():
    try:
        with open(WHATSAPP_STOCK_FEED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "group": "BuyAlertsContrbutingAndPaidMembers",
            "signals": [],
            "message": "No local WhatsApp stock feed found yet. Run scripts/whatsapp_stock_feed_auto.py after exporting the group chat.",
            "expectedPath": WHATSAPP_STOCK_FEED_FILE,
        }
    except Exception as e:
        return {"signals": [], "error": f"local feed unreadable: {e}"}

def sync_whatsapp_stock_feed():
    """Runs the fixed local WhatsApp export scan once; no user command input."""
    if not os.path.exists(WHATSAPP_AUTO_SCRIPT):
        return {"ok": False, "error": "auto ingester script missing", "feed": load_whatsapp_stock_feed()}
    try:
        proc = subprocess.run(
            [sys.executable, WHATSAPP_AUTO_SCRIPT, "--once"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=90,
        )
        return {
            "ok": proc.returncode == 0,
            "returnCode": proc.returncode,
            "stdout": (proc.stdout or "").strip()[-2000:],
            "stderr": (proc.stderr or "").strip()[-2000:],
            "feed": load_whatsapp_stock_feed(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "WhatsApp sync timed out", "feed": load_whatsapp_stock_feed()}
    except Exception as e:
        return {"ok": False, "error": str(e), "feed": load_whatsapp_stock_feed()}

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
        if path == "/docs/whatsapp-stock-feed":
            try:
                body = open(os.path.join(BASE_DIR, "WHATSAPP_STOCK_FEED.md"), "rb").read()
                self._send(200, "text/markdown; charset=utf-8", body)
            except FileNotFoundError:
                self._send(404, "text/plain", b"WHATSAPP_STOCK_FEED.md not found")
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

        elif path == "/candles":
            syms = parse_symbols(qs, ["NVDA", "AMD", "IONQ"])
            interval = (qs.get("interval", ["5m"])[0] or "5m").lower()
            period = (qs.get("period", ["1d"])[0] or "1d").lower()
            print(f"  [{time.strftime('%H:%M:%S')}] /candles ({','.join(syms)}) {interval}/{period} ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_candles(syms, interval, period)
            print(f"{len(data)} syms in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

        elif path == "/daytrade":
            syms = parse_symbols(qs, ["NVDA", "AMD", "IONQ"])
            interval = (qs.get("interval", ["5m"])[0] or "5m").lower()
            period = (qs.get("period", ["1d"])[0] or "1d").lower()
            print(f"  [{time.strftime('%H:%M:%S')}] /daytrade ({','.join(syms)}) {interval}/{period} ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_daytrade(syms, interval, period)
            print(f"{len(data.get('results', []))} setups in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

        elif path == "/speculative-daytrade":
            try:
                limit = int(qs.get("limit", ["5"])[0])
            except ValueError:
                limit = 5
            validate_history = (qs.get("validate", ["0"])[0] or "0").lower() in {"1", "true", "yes"}
            print(f"  [{time.strftime('%H:%M:%S')}] /speculative-daytrade top {limit} 1m validate={validate_history} ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_speculative_daytrade(limit, validate_history)
            print(f"{len(data.get('results', []))} setups in {time.time()-t0:.1f}s", flush=True)
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

        elif path == "/whatsapp-stock-feed":
            self._send(200, "application/json", json.dumps(load_whatsapp_stock_feed()).encode())

        elif path == "/whatsapp-sync":
            print(f"  [{time.strftime('%H:%M:%S')}] /whatsapp-sync local scan ...", end=" ", flush=True)
            t0 = time.time()
            data = sync_whatsapp_stock_feed()
            print(f"done in {time.time()-t0:.1f}s", flush=True)
            self._send(200, "application/json", json.dumps(data).encode())

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
    print(f"\n  NandaEdge Server v3.6 — {HOST}:{PORT}", flush=True)

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
