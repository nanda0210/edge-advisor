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
import os, sys, signal, time, json, math, socket, warnings, urllib.request, urllib.error, threading, random, subprocess, gc, base64
import logging, hmac
from datetime import date, datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from zoneinfo import ZoneInfo

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
# Overridable so the scan can be exercised without mutating real history, and
# so cloud deploys can point it at a persistent volume instead of ephemeral disk.
DAYTRADE_HISTORY_FILE = os.environ.get(
    "DAYTRADE_HISTORY_FILE",
    os.path.join(BASE_DIR, "daytrade_history.json"),
)
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
# Two credentials, two roles:
#   AUTH_TOKEN_HASH  — the owner's password
#   DEMO_TOKEN_HASH  — a shared, publicly-known demo password
# Both are SHA-256 of the password, lowercase hex. The demo credential is
# deliberately not a secret; it gates a read-only dashboard.
AUTH_HASH = (os.environ.get("AUTH_TOKEN_HASH", "") or "").strip().lower()
DEMO_HASH = (os.environ.get("DEMO_TOKEN_HASH", "") or "").strip().lower()

# User accounts are optional: the dashboard still runs on the demo password
# alone if the module or its database is unavailable.
try:
    import accounts
    accounts.create_tables()
    ACCOUNTS_ENABLED = True
    print(f"  Accounts: enabled ({accounts.storage_label()})", flush=True)
except Exception as _acct_err:
    accounts = None
    ACCOUNTS_ENABLED = False
    print(f"  Accounts: disabled ({type(_acct_err).__name__}: {_acct_err})", flush=True)
# Session length the client is told to enforce, per role.
SESSION_MINUTES_BY_ROLE = {
    "demo": int(os.environ.get("DEMO_SESSION_MINUTES", "15") or "15"),
    "user": int(os.environ.get("USER_SESSION_MINUTES", "15") or "15"),
}
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
    """Returns (ok, status, role).

    status: 'open' | 'ok' | 'unauthorized' | 'rate_limited'
    role:   'open' | 'user' | 'demo' | None
    """
    if not AUTH_HASH and not DEMO_HASH and not ACCOUNTS_ENABLED:
        return True, "open", "open"
    ip = _client_ip(handler)
    if _rate_limited(ip):
        return False, "rate_limited", None
    h = handler.headers.get("Authorization", "")
    if h.lower().startswith("bearer "):
        raw = h[7:].strip()
        # An account session token is checked first: it is opaque and random,
        # so it cannot collide with a 64-char hex password hash.
        if ACCOUNTS_ENABLED and len(raw) > 40:
            try:
                user = accounts.user_for_session(raw)
                if user:
                    accounts.touch_session(raw)      # idle timeout, not a hard cap
                    return True, "ok", "user"
            except Exception as e:
                print(f"  WARN session lookup: {type(e).__name__}: {e}", flush=True)
        token = raw.lower()
        # Constant-time: a plain == leaks match length/prefix through timing.
        # Both are checked so the comparison count does not reveal which
        # credential was tried.
        is_user = bool(AUTH_HASH) and hmac.compare_digest(token, AUTH_HASH)
        is_demo = bool(DEMO_HASH) and hmac.compare_digest(token, DEMO_HASH)
        if is_user:
            return True, "ok", "user"
        if is_demo:
            return True, "ok", "demo"
    _record_fail(ip)
    return False, "unauthorized", None

warnings.filterwarnings("ignore")

PORT      = int(os.environ.get("PORT", 8765))
HOST      = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "localhost")
HTML_FILE = os.path.join(BASE_DIR, "index.html")

# yfinance is chatty on provider/network failure. Keep only a short, latest
# error string for response payloads; avoid spamming stdout/stderr during normal
# dashboard refreshes.
_LAST_YFINANCE_ERROR = None
_LAST_YFINANCE_ERROR_BY_SYMBOL = {}
_YFINANCE_ERROR_LOCK = threading.Lock()
# Cap the per-symbol map: scans walk large universes and this would otherwise
# grow for the life of the process.
_YFINANCE_ERROR_MAX_SYMBOLS = 512

def _set_last_yfinance_error(msg, sym=None):
    global _LAST_YFINANCE_ERROR
    if not msg:
        return
    s = str(msg).strip()
    if not s:
        return
    with _YFINANCE_ERROR_LOCK:
        _LAST_YFINANCE_ERROR = s[:240]
        if sym:
            if len(_LAST_YFINANCE_ERROR_BY_SYMBOL) >= _YFINANCE_ERROR_MAX_SYMBOLS:
                _LAST_YFINANCE_ERROR_BY_SYMBOL.clear()
            _LAST_YFINANCE_ERROR_BY_SYMBOL[str(sym).upper()] = _LAST_YFINANCE_ERROR

def _provider_hint(msg):
    """Return a short, actionable hint for common provider failures."""
    if not msg:
        return None
    s = str(msg).strip()
    if not s:
        return None
    lower = s.lower()
    if "could not resolve host: guce.yahoo.com" in lower or "enotfound guce.yahoo.com" in lower:
        return "DNS cannot resolve guce.yahoo.com (Yahoo consent host). If you run ad-block DNS (Pi-hole/NextDNS), whitelist guce.yahoo.com or use a non-filtering DNS for validation runs."
    if "too many requests" in lower or "rate limit" in lower or "429" in lower:
        return "Yahoo rate-limited the request. Reduce refresh frequency, lower symbol count, or retry later."
    if "impersonateerror" in lower or "not supported" in lower and "chrome" in lower:
        return "curl_cffi impersonation mismatch. Upgrade curl_cffi/yfinance or set a supported impersonation profile."
    return None

# Provider noise is captured off yfinance's own loggers rather than by
# redirecting sys.stdout/sys.stderr. This server runs a thread per request:
# redirect_stdout swaps a PROCESS-GLOBAL stream, so one request would swallow
# another's log lines and could surface them as its own `providerError`.
# A logging handler plus thread-local buffer keeps capture per-request.
_YF_CAPTURE = threading.local()


class _ProviderLogCapture(logging.Handler):
    def emit(self, record):
        buf = getattr(_YF_CAPTURE, "buf", None)
        if buf is None:
            return
        try:
            buf.append(self.format(record))
        except Exception:
            pass


_provider_log_handler = _ProviderLogCapture()
_provider_log_handler.setFormatter(logging.Formatter("%(message)s"))
for _noisy in ("yfinance", "urllib3", "peewee", "curl_cffi"):
    _lg = logging.getLogger(_noisy)
    _lg.addHandler(_provider_log_handler)
    _lg.setLevel(logging.WARNING)
    _lg.propagate = False          # keep it off the console


def _quiet_call(fn):
    """Run `fn`, returning (result, provider_log) for this thread only."""
    buf = []
    prev = getattr(_YF_CAPTURE, "buf", None)
    _YF_CAPTURE.buf = buf
    try:
        result = fn()
    finally:
        _YF_CAPTURE.buf = prev
    return result, "\n".join(buf).strip()

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
# Raw prediction history stays at 7 days per product rules. Week-over-week
# accuracy does not need it: tallies are absorbed into a durable rollup before
# each prune (see _accuracy_absorb). 7 days also matches Yahoo's 1-minute
# history limit, which is the window point-in-time scoring can reach.
DAYTRADE_HISTORY_DAYS = int(os.environ.get("DAYTRADE_HISTORY_DAYS", "7") or "7")

# ── Scoring configuration ──────────────────────────────
# Forward window a prediction gets to resolve. 0 = until the session close.
FORWARD_WINDOW_MINUTES = int(os.environ.get("FORWARD_WINDOW_MINUTES", "0") or "0")
# Round-trip cost (spread + slippage) charged against every scored trade, in
# basis points of entry. Ignoring it flatters tight-stop setups badly.
COST_BPS = float(os.environ.get("COST_BPS", "10") or "10")
# A stop closer than this multiple of ATR is noise-range: it will be hit by
# normal oscillation regardless of direction.
MIN_STOP_ATR_MULT = float(os.environ.get("MIN_STOP_ATR_MULT", "0.5") or "0.5")
# A signal with fewer than this many minutes to the close cannot realistically
# resolve a multi-percent target.
MIN_MINUTES_TO_CLOSE = int(os.environ.get("MIN_MINUTES_TO_CLOSE", "30") or "30")
# Most recent N days are held out as out-of-sample for walk-forward reporting.
OUT_OF_SAMPLE_DAYS = int(os.environ.get("OUT_OF_SAMPLE_DAYS", "3") or "3")

RESOLUTION_EXIT = "exit_first"
RESOLUTION_STOP = "stop_first"
RESOLUTION_NONE = "no_resolution"
RESOLUTION_UNSCOREABLE = "unscoreable"
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
    # Key includes caller-supplied DTE/delta/top_n, so this is the one cache an
    # unauthenticated client can inflate at will. Keep it bounded.
    _trim_cache(_opts_cache, 128)
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


def _pacific_date_key(ts):
    try:
        if hasattr(ts, "to_pydatetime"):
            dt = ts.to_pydatetime()
        else:
            dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PACIFIC_TZ).strftime("%Y-%m-%d")
    except Exception:
        return None


def _to_utc_datetime(ts):
    """Normalize a pandas/py timestamp to an aware UTC datetime."""
    try:
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _utc_iso(ts):
    """Machine-parseable signal time. `signalTimePST` is a display label only;
    point-in-time validation needs a real timestamp to filter bars against."""
    dt = _to_utc_datetime(ts)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _session_bucket(ts):
    """Coarse time-of-day bucket in Pacific market hours (06:30–13:00 PT)."""
    dt = _to_utc_datetime(ts)
    if not dt:
        return None
    pt = dt.astimezone(PACIFIC_TZ)
    minutes = pt.hour * 60 + pt.minute
    if minutes < 6 * 60 + 30:
        return "pre_market"
    if minutes < 7 * 60 + 30:
        return "open_hour"
    if minutes < 10 * 60:
        return "morning"
    if minutes < 12 * 60:
        return "midday"
    if minutes < 13 * 60:
        return "power_hour"
    return "after_hours"


def _minutes_to_close(ts):
    """Minutes from the signal bar to the 13:00 PT close; negative after it."""
    dt = _to_utc_datetime(ts)
    if not dt:
        return None
    pt = dt.astimezone(PACIFIC_TZ)
    return (13 * 60) - (pt.hour * 60 + pt.minute)


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
        "signalDayPT": _pacific_date_key(last_ts),
        "signalTsUTC": _utc_iso(last_ts),
        "sessionBucket": _session_bucket(last_ts),
        "minutesToClose": _minutes_to_close(last_ts),
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
        df, provider_log = _quiet_call(lambda: yf.download(
            " ".join(symbols),
            period=period,
            interval=interval,
            group_by="ticker",
            threads=False,
            progress=False,
            auto_adjust=False,
            prepost=False,
        ))
        if provider_log:
            _set_last_yfinance_error(provider_log.splitlines()[0])
    except Exception as e:
        print(f"  WARN candles batch fallback: {e}", flush=True)
        _set_last_yfinance_error(e)
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
            d, provider_log = _quiet_call(lambda: yf.download(
                sym,
                period=period,
                interval=interval,
                group_by="ticker",
                threads=False,
                progress=False,
                auto_adjust=False,
                prepost=False,
            ))
            if provider_log:
                _set_last_yfinance_error(provider_log.splitlines()[0], sym=sym)
            d = d.dropna() if d is not None else None
            if d is not None and not d.empty:
                frames[sym] = d
        except Exception as e:
            print(f"  WARN candles single {sym}: {e}", flush=True)
            _set_last_yfinance_error(e, sym=sym)
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
    for sym in syms:
        d = frames.get(sym)
        if d is None or getattr(d, "empty", True):
            per_sym_error = _LAST_YFINANCE_ERROR_BY_SYMBOL.get(str(sym).upper()) or _LAST_YFINANCE_ERROR
            per_sym_hint = _provider_hint(per_sym_error)
            out[sym] = {
                "symbol": sym,
                "interval": interval,
                "period": period,
                "candles": [],
                "error": "no intraday candle data",
                "providerError": per_sym_error,
                **({"providerHint": per_sym_hint} if per_sym_hint else {}),
            }
            continue
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
            if sym not in frames or frames.get(sym) is None or getattr(frames.get(sym), "empty", True):
                per_sym_error = _LAST_YFINANCE_ERROR_BY_SYMBOL.get(str(sym).upper()) or _LAST_YFINANCE_ERROR
                per_sym_hint = _provider_hint(per_sym_error)
                row = {"symbol": sym, "error": "no intraday candle data", "providerError": per_sym_error}
                if per_sym_hint:
                    row["providerHint"] = per_sym_hint
                results.append(row)
                continue
            item = _daytrade_for_frame(sym, frames.get(sym), interval)
            results.append(item or {"symbol": sym, "error": "insufficient candle history"})
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


# Prediction history is a single JSON file mutated read-modify-write by request
# threads. Serialize those writes and make them atomic: a crash or two
# concurrent scans previously truncated the file or dropped a whole day.
_HISTORY_LOCK = threading.RLock()


def _history_load():
    try:
        with open(DAYTRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"days": {}}
    except Exception:
        return {"days": {}}


def _history_prune(data):
    """Drop days past the retention window and re-compact stored predictions."""
    # Absorb accuracy FIRST: once a day is pruned its outcomes are unrecoverable,
    # and the week-over-week comparison depends on them.
    _accuracy_absorb(data)
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
    return data


def _history_write_atomic(data):
    """Write to a temp file in the same directory, then os.replace() it in."""
    tmp = f"{DAYTRADE_HISTORY_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DAYTRADE_HISTORY_FILE)


def _history_save(data):
    try:
        with _HISTORY_LOCK:
            _history_write_atomic(_history_prune(data))
    except Exception as e:
        print(f"  WARN daytrade history save: {e}", flush=True)


def _history_merge_day(day_key, payload):
    """Re-read from disk under the lock, set one day, write atomically.

    A scan holds its in-memory `history` across network calls, so writing that
    whole snapshot back would discard any day another request stored meanwhile.
    """
    try:
        with _HISTORY_LOCK:
            fresh = _history_load()
            fresh.setdefault("days", {})[day_key] = payload
            _history_write_atomic(_history_prune(fresh))
            return fresh
    except Exception as e:
        print(f"  WARN daytrade history merge: {e}", flush=True)
        return None


def _compact_setup(setup, keep_candles=False):
    """Persist only the decision fields; candle arrays stay response-only."""
    fields = [
        "symbol", "interval", "last", "bias", "setup", "entryValue", "exitValue",
        "target2", "stopLoss", "riskPerShare", "vwap", "ema9", "ema20", "atr",
        "dayHigh", "dayLow", "volumeRatio", "aiConfidence", "tradeRating",
        "exitRule", "why", "speculativeScore", "alertStatus", "alertReason",
        "modelRule", "signalTimePST", "entryTimePST", "exitTimePST",
        "invalidTimePST", "signalDayPT", "outcome", "outcomeReason", "realized",
        "whatWentWrong",
        # scoring fields (point-in-time validation, R-multiples, regime tags)
        "signalTsUTC", "sessionBucket", "minutesToClose", "vixRegime",
        "plannedR", "targetDistPct", "stopDistPct", "stopAtrMult", "stopTooTight",
        "resolution", "resolvedAtUTC", "realizedR", "mfeR", "maeR", "costBps",
        "lateSignal",
    ]
    compact = {k: setup[k] for k in fields if k in setup}
    if compact.get("outcome") in (None, ""):
        compact["outcome"] = "pending"
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
        "signalTimePST", "signalDayPT", "entryTimePST", "exitTimePST", "invalidTimePST",
        "signalTsUTC", "sessionBucket", "minutesToClose", "vixRegime",
        "plannedR", "targetDistPct", "stopDistPct", "stopAtrMult", "stopTooTight",
        "lateSignal",
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
    # Per-day tallies outlive the raw predictions, so surface them too.
    acc_days = (history.get("accuracy") or {}).get("days") or {}
    if acc_days:
        compact["accuracyDays"] = dict(sorted(acc_days.items()))
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


_RESOLUTION_TO_OUTCOME = {
    RESOLUTION_EXIT: "exit_hit",
    RESOLUTION_STOP: "invalid",
    RESOLUTION_NONE: "no_resolution",
}


def _update_prediction_history(history, today_key, frames_5d):
    """Score stored predictions point-in-time.

    Scoring is attempted against bars strictly after each signal. When the
    provider gives only daily bars (no intraday timestamps to compare against),
    the prediction is left unscored rather than graded on a day range that
    includes price action from before the signal fired.
    """
    days = history.setdefault("days", {})
    for day_key, day_payload in list(days.items()):
        if day_key >= today_key:
            continue
        if not _is_trading_day(day_key):
            continue          # no session, so nothing to validate against
        for setup in day_payload.get("predictions", []):
            if setup.get("resolution") in (RESOLUTION_EXIT, RESOLUTION_STOP, RESOLUTION_NONE):
                continue
            setup.update(_planned_geometry(setup))
            sym = setup.get("symbol", "")
            frame = frames_5d.get(sym) if frames_5d else None
            realized = _realized_for_symbol(frames_5d, sym, day_key)
            if realized:
                setup["realized"] = realized

            bars = _bars_after(frame, setup.get("signalTsUTC"))
            if bars is None:
                # No post-signal bars available (daily-only data, or a legacy
                # row written before signalTsUTC existed).
                setup["outcome"] = setup.get("outcome") or "pending"
                setup["resolution"] = RESOLUTION_UNSCOREABLE
                setup["outcomeReason"] = (
                    "Not scoreable point-in-time: no intraday bars after the signal "
                    "timestamp (needs 1m data within Yahoo's ~7-day window)."
                )
                continue

            res = _resolve_path(setup, bars)
            setup.update(res)
            setup["outcome"] = _RESOLUTION_TO_OUTCOME.get(res.get("resolution"), "pending")
            setup["outcomeReason"] = _resolution_reason(res)
            setup["whatWentWrong"] = _what_went_wrong(setup, setup["outcome"], realized)
    return history


def _resolution_reason(res):
    r = res.get("resolution")
    rr = res.get("realizedR")
    if r == RESOLUTION_EXIT:
        return f"Exit touched first after the signal (net {rr}R after costs)."
    if r == RESOLUTION_STOP:
        return f"Stop touched first after the signal (net {rr}R after costs)."
    if r == RESOLUTION_NONE:
        return f"Forward window closed with neither level touched (marked to close, {rr}R)."
    return "Not scoreable."


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


# ── Prediction accuracy ────────────────────────────────
# Raw predictions are pruned after DAYTRADE_HISTORY_DAYS (7), so a week-over-week
# comparison cannot be computed from them alone — last week's rows are already
# gone. Accuracy is therefore absorbed into a durable per-day rollup BEFORE each
# prune. Days still in raw history are recomputed every pass (outcomes get filled
# in later by validation); pruned days keep their last computed values.
ACCURACY_RETENTION_DAYS = int(os.environ.get("ACCURACY_RETENTION_DAYS", "400") or "400")
# Below this many resolved predictions a percentage is noise, so none is shown.
MIN_ACCURACY_SAMPLE = int(os.environ.get("MIN_ACCURACY_SAMPLE", "5") or "5")
# Cap symbols per validation pass to bound the provider call.
ACCURACY_SYMBOL_LIMIT = int(os.environ.get("ACCURACY_SYMBOL_LIMIT", "40") or "40")

WIN_OUTCOMES = ("exit_hit",)
LOSS_OUTCOMES = ("invalid",)


_vix_regime_cache = {"t": 0, "value": None}
VIX_REGIME_TTL = 600


def _vix_regime():
    """Coarse volatility regime at signal time, for slicing outcomes later.

    Cached and failure-tolerant: regime tagging must never break a scan.
    """
    now = time.time()
    if _vix_regime_cache["value"] and now - _vix_regime_cache["t"] < VIX_REGIME_TTL:
        return _vix_regime_cache["value"]
    try:
        frames = _fetch_intraday_frames(["^VIX"], "1d", "5d")
        d = frames.get("^VIX") if frames else None
        if d is None or getattr(d, "empty", True):
            return None
        vix = _safe_float(d["Close"].dropna().iloc[-1])
        if not vix or vix <= 0:
            return None
        if vix < 15:
            label = "calm"
        elif vix < 20:
            label = "normal"
        elif vix < 30:
            label = "elevated"
        else:
            label = "stressed"
        _vix_regime_cache.update({"t": now, "value": label})
        return label
    except Exception:
        return None


def _planned_geometry(setup):
    """Target/stop distances and planned R, measured from the entry reference."""
    bias = setup.get("bias")
    base = _safe_float(setup.get("entryValue")) or _safe_float(setup.get("last"))
    exit_value = _safe_float(setup.get("exitValue"))
    stop = _safe_float(setup.get("stopLoss"))
    atr = _safe_float(setup.get("atr"))
    out = {}
    if not base or exit_value is None or stop is None:
        return out
    reward = abs(exit_value - base)
    risk = abs(base - stop)
    out["targetDistPct"] = round(reward / base * 100, 3)
    out["stopDistPct"] = round(risk / base * 100, 3)
    out["plannedR"] = round(reward / risk, 2) if risk else None
    if atr and atr > 0:
        out["stopAtrMult"] = round(risk / atr, 2)
        out["stopTooTight"] = bool(risk / atr < MIN_STOP_ATR_MULT)
    mins = setup.get("minutesToClose")
    if isinstance(mins, (int, float)):
        out["lateSignal"] = bool(mins < MIN_MINUTES_TO_CLOSE)
    _ = bias
    return out


def _bars_after(d, signal_ts_utc, window_minutes=None):
    """Bars strictly AFTER the signal, bounded by the forward window.

    This is the correction that matters most: scoring a signal against the whole
    session's high/low counts price action that happened before the signal
    existed, which manufactures stop-outs that never occurred.
    """
    if d is None or getattr(d, "empty", True) or not signal_ts_utc:
        return None
    try:
        start = datetime.strptime(signal_ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    try:
        idx = d.index
        if getattr(idx, "tz", None) is None:
            idx_utc = idx.tz_localize("UTC")
        else:
            idx_utc = idx.tz_convert("UTC")
        mask = idx_utc > start
        window = window_minutes if window_minutes is not None else FORWARD_WINDOW_MINUTES
        if window and window > 0:
            mask = mask & (idx_utc <= start + timedelta(minutes=window))
        rows = d[mask]
        return rows if not rows.empty else None
    except Exception:
        return None


def _resolve_path(setup, bars):
    """Walk bars in time order; report which level is touched FIRST.

    `_setup_status` tests the exit before the stop, so when a single bar's range
    spans both it always scores a win. Here the first bar to touch either level
    decides, and a bar touching both is resolved conservatively as a stop.
    """
    bias = setup.get("bias")
    base = _safe_float(setup.get("entryValue")) or _safe_float(setup.get("last"))
    exit_value = _safe_float(setup.get("exitValue"))
    stop = _safe_float(setup.get("stopLoss"))
    if not base or exit_value is None or stop is None or bars is None:
        return {"resolution": RESOLUTION_UNSCOREABLE}

    risk = abs(base - stop)
    if not risk:
        return {"resolution": RESOLUTION_UNSCOREABLE}
    short = bias == "SHORT"
    mfe = mae = 0.0

    for ts, row in bars.iterrows():
        hi = _safe_float(row.get("High"))
        lo = _safe_float(row.get("Low"))
        if hi is None or lo is None:
            continue
        fav = (base - lo) if short else (hi - base)
        adv = (hi - base) if short else (base - lo)
        mfe = max(mfe, fav)
        mae = max(mae, adv)

        hit_exit = (lo <= exit_value) if short else (hi >= exit_value)
        hit_stop = (hi >= stop) if short else (lo <= stop)
        if hit_exit and hit_stop:
            # Both inside one bar: intra-bar order is unknown, so assume the
            # worse outcome rather than crediting a win we cannot prove.
            return _resolution_payload(RESOLUTION_STOP, ts, base, risk, mfe, mae, stop_r=-1.0)
        if hit_exit:
            reward = abs(exit_value - base)
            return _resolution_payload(RESOLUTION_EXIT, ts, base, risk, mfe, mae,
                                       stop_r=reward / risk)
        if hit_stop:
            return _resolution_payload(RESOLUTION_STOP, ts, base, risk, mfe, mae, stop_r=-1.0)

    # Window expired with neither level touched: mark to close, not as a loss.
    last_ts = bars.index[-1]
    last_close = _safe_float(bars["Close"].iloc[-1])
    gross_r = 0.0
    if last_close is not None:
        move = (base - last_close) if short else (last_close - base)
        gross_r = move / risk
    return _resolution_payload(RESOLUTION_NONE, last_ts, base, risk, mfe, mae, stop_r=gross_r)


def _resolution_payload(resolution, ts, base, risk, mfe, mae, stop_r):
    """Attach the cost charge and convert favourable/adverse excursions to R."""
    cost_r = (base * COST_BPS / 10000.0) / risk if risk else 0.0
    return {
        "resolution": resolution,
        "resolvedAtUTC": _utc_iso(ts),
        "realizedR": round(stop_r - cost_r, 3),
        "mfeR": round(mfe / risk, 3) if risk else None,
        "maeR": round(mae / risk, 3) if risk else None,
        "costBps": COST_BPS,
    }


def _iso_week_key(day_key):
    """'2026-10-09' -> '2026-W41' (ISO week, so weeks never split a year oddly)."""
    try:
        y, w, _ = date.fromisoformat(day_key).isocalendar()
        return f"{y}-W{w:02d}"
    except Exception:
        return None


def _is_trading_day(day_key):
    """Mon–Fri only. Saturday/Sunday predictions can never be scored: the market
    is closed, so no bar exists for that date and the outcome stays unresolved
    forever. They are excluded from accuracy rather than counted as misses."""
    try:
        return date.fromisoformat(day_key).weekday() < 5
    except Exception:
        return False


def _previous_trading_day(d):
    """Walk back to the nearest Mon–Fri (today included if it is a weekday)."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _bucket_keys(p):
    """Dimensions a prediction is sliced by for calibration/regime analysis."""
    conf = p.get("aiConfidence")
    if isinstance(conf, (int, float)):
        # 0-1 or 0-100 both appear in the wild; normalize to deciles of percent.
        pct = conf * 100 if conf <= 1 else conf
        conf_key = f"{int(max(0, min(99, pct)) // 10) * 10}-{int(max(0, min(99, pct)) // 10) * 10 + 9}%"
    else:
        conf_key = str(conf) if conf else "unknown"
    return {
        "confidence": conf_key,
        "rating": str(p.get("tradeRating") or "unknown"),
        "session": str(p.get("sessionBucket") or "unknown"),
        "vix": str(p.get("vixRegime") or "unknown"),
        "symbol": str(p.get("symbol") or "unknown"),
        "bias": str(p.get("bias") or "unknown"),
    }


def _blank_stats():
    return {"wins": 0, "losses": 0, "unresolved": 0, "sumR": 0.0,
            "sumWinR": 0.0, "sumLossR": 0.0}


def _add_outcome(stats, outcome, realized_r):
    if outcome in WIN_OUTCOMES:
        stats["wins"] += 1
        if realized_r is not None:
            stats["sumWinR"] += realized_r
    elif outcome in LOSS_OUTCOMES:
        stats["losses"] += 1
        if realized_r is not None:
            stats["sumLossR"] += realized_r
    else:
        stats["unresolved"] += 1
        return                    # unresolved contributes no R
    if realized_r is not None:
        stats["sumR"] += realized_r


def _tally_predictions(predictions):
    """Count wins/losses/unresolved plus R sums and per-dimension buckets.

    R sums are stored (not just counts) so expectancy survives raw-history
    pruning exactly as the win/loss tallies do.
    """
    agg = _blank_stats()
    agg["unscoreable"] = 0
    buckets = {}
    for p in predictions or []:
        outcome = p.get("outcome")
        realized_r = _safe_float(p.get("realizedR"))
        if p.get("resolution") == RESOLUTION_UNSCOREABLE:
            agg["unscoreable"] += 1
        _add_outcome(agg, outcome, realized_r)
        if outcome in WIN_OUTCOMES or outcome in LOSS_OUTCOMES:
            for dim, key in _bucket_keys(p).items():
                slot = buckets.setdefault(dim, {}).setdefault(key, _blank_stats())
                _add_outcome(slot, outcome, realized_r)
    agg["resolved"] = agg["wins"] + agg["losses"]
    agg["total"] = agg["resolved"] + agg["unresolved"]
    agg["buckets"] = buckets
    return agg


def _accuracy_pct(wins, resolved):
    """Percentage over RESOLVED predictions only; None when the sample is thin."""
    if not resolved or resolved < MIN_ACCURACY_SAMPLE:
        return None
    return round(100.0 * wins / resolved, 1)


def _expectancy_block(stats):
    """Expectancy in R — the metric that decides whether an edge exists.

    Win rate alone is gameable: shrink the target toward the stop and it rises
    while profitability falls. Expectancy per trade (average R, net of costs)
    cannot be gamed that way, so it is reported alongside the hit rate.
    """
    wins, losses = stats.get("wins", 0), stats.get("losses", 0)
    resolved = wins + losses
    out = {
        "wins": wins,
        "losses": losses,
        "unresolved": stats.get("unresolved", 0),
        "resolved": resolved,
        "total": resolved + stats.get("unresolved", 0),
        "accuracyPct": _accuracy_pct(wins, resolved),
        "avgWinR": round(stats["sumWinR"] / wins, 3) if wins else None,
        "avgLossR": round(stats["sumLossR"] / losses, 3) if losses else None,
    }
    if resolved >= MIN_ACCURACY_SAMPLE:
        out["expectancyR"] = round(stats.get("sumR", 0.0) / resolved, 3)
        out["totalR"] = round(stats.get("sumR", 0.0), 2)
        gross_win = stats["sumWinR"]
        gross_loss = abs(stats["sumLossR"])
        out["profitFactor"] = round(gross_win / gross_loss, 2) if gross_loss else None
        # Win rate this geometry must beat just to break even.
        if out["avgWinR"] and out["avgLossR"]:
            rr = abs(out["avgWinR"] / out["avgLossR"]) if out["avgLossR"] else None
            out["rewardRisk"] = round(rr, 2) if rr else None
            out["breakevenWinPct"] = round(100.0 / (1 + rr), 1) if rr else None
        out["verdict"] = (
            "positive expectancy" if out["expectancyR"] > 0 else
            "negative expectancy" if out["expectancyR"] < 0 else "flat"
        )
    else:
        out["expectancyR"] = None
        out["verdict"] = f"insufficient sample (<{MIN_ACCURACY_SAMPLE} resolved)"
    return out


def _merge_stats(target, src):
    for k in ("wins", "losses", "unresolved", "sumR", "sumWinR", "sumLossR", "unscoreable"):
        if k in src:
            target[k] = target.get(k, 0) + (src.get(k) or 0)
    return target


def _merge_buckets(target, src):
    for dim, keys in (src or {}).items():
        for key, stats in keys.items():
            slot = target.setdefault(dim, {}).setdefault(key, _blank_stats())
            _merge_stats(slot, stats)
    return target


def _accuracy_absorb(data):
    """Refresh the durable per-day rollup from whatever raw days are present.

    Idempotent: re-running recomputes live days and leaves pruned days frozen.
    Must be called before pruning or the pruned day's result is lost forever.
    """
    acc = data.setdefault("accuracy", {})
    acc_days = acc.setdefault("days", {})
    # Persisted as {day: count} so the disclosure survives raw-history pruning
    # and re-absorbing overwrites a day rather than double-counting it.
    excluded = acc.setdefault("excludedWeekend", {})
    for day_key, payload in (data.get("days") or {}).items():
        tally = _tally_predictions(payload.get("predictions"))
        if not tally["total"]:
            continue
        if not _is_trading_day(day_key):
            # Weekend-dated rows are unscoreable by definition — keep them out
            # of the tallies instead of letting them sit as permanent misses.
            excluded[day_key] = tally["total"]
            acc_days.pop(day_key, None)
            continue
        tally["week"] = _iso_week_key(day_key)
        tally["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        acc_days[day_key] = tally
    cutoff = date.today() - timedelta(days=ACCURACY_RETENTION_DAYS)
    for k in list(acc_days.keys()):
        try:
            # Drop expired entries, plus any weekend rows written by older builds.
            if date.fromisoformat(k) < cutoff or not _is_trading_day(k):
                del acc_days[k]
        except Exception:
            del acc_days[k]
    for k in list(excluded.keys()):
        try:
            if date.fromisoformat(k) < cutoff:
                del excluded[k]
        except Exception:
            del excluded[k]
    return data


def _aggregate_days(acc_days, day_keys, with_buckets=False):
    stats = _blank_stats()
    stats["unscoreable"] = 0
    buckets = {}
    for k in day_keys:
        day = acc_days.get(k) or {}
        _merge_stats(stats, day)
        if with_buckets:
            _merge_buckets(buckets, day.get("buckets"))
    out = _expectancy_block(stats)
    out["unscoreable"] = stats.get("unscoreable", 0)
    out["tradingDays"] = len(day_keys)
    if with_buckets:
        out["_buckets"] = buckets
    return out


def _week_bucket(acc_days, week_key):
    keys = [k for k, v in acc_days.items() if v.get("week") == week_key]
    bucket = _aggregate_days(acc_days, keys)
    bucket["week"] = week_key
    bucket["days"] = sorted(keys)
    return bucket


def _rank_buckets(buckets, dim, min_resolved=None):
    """Expectancy per bucket for one dimension, worst-to-best, sample-gated."""
    floor = MIN_ACCURACY_SAMPLE if min_resolved is None else min_resolved
    rows = []
    for key, stats in (buckets.get(dim) or {}).items():
        block = _expectancy_block(stats)
        block["key"] = key
        rows.append(block)
    rows.sort(key=lambda r: (r.get("expectancyR") is None, r.get("expectancyR") or 0))
    return [r for r in rows if r["resolved"] >= floor] or rows


def _confidence_level(key):
    """Numeric lower bound of a '70-79%' style bucket key, for ordering."""
    try:
        return int(str(key).split("-", 1)[0].rstrip("%"))
    except Exception:
        return None


def _calibration_notes(buckets):
    """Does a HIGHER stated confidence actually produce a better outcome?

    Must be judged by ordering buckets on confidence level, not on expectancy —
    sorting by expectancy makes any score look predictive by construction.
    """
    notes = []
    scored = [r for r in _rank_buckets(buckets, "confidence")
              if r.get("expectancyR") is not None and _confidence_level(r["key"]) is not None]
    if len(scored) < 2:
        return notes
    scored.sort(key=lambda r: _confidence_level(r["key"]))
    low, high = scored[0], scored[-1]          # lowest vs highest CONFIDENCE
    if high["expectancyR"] > low["expectancyR"]:
        notes.append(
            f"aiConfidence is directionally calibrated: {low['key']} = {low['expectancyR']}R "
            f"vs {high['key']} = {high['expectancyR']}R. The top buckets are where the edge is."
        )
    elif high["expectancyR"] < low["expectancyR"]:
        notes.append(
            f"aiConfidence is INVERTED: the {high['key']} bucket returns "
            f"{high['expectancyR']}R while {low['key']} returns {low['expectancyR']}R. "
            "High-confidence signals are performing worse than low-confidence ones."
        )
    else:
        notes.append(
            f"aiConfidence is not calibrated: {low['key']} and {high['key']} both return "
            f"{high['expectancyR']}R. The score carries no information — treat it as decoration."
        )
    return notes


def _accuracy_summary(history, today=None):
    """Week-over-week accuracy plus all-time, computed off the durable rollup."""
    acc_days = (history.get("accuracy") or {}).get("days") or {}
    today = today or date.today()
    iso_y, iso_w, _ = today.isocalendar()
    this_week = f"{iso_y}-W{iso_w:02d}"
    prev_y, prev_w, _ = (today - timedelta(days=7)).isocalendar()
    prev_week = f"{prev_y}-W{prev_w:02d}"

    current = _week_bucket(acc_days, this_week)
    previous = _week_bucket(acc_days, prev_week)
    all_time = _aggregate_days(acc_days, list(acc_days.keys()), with_buckets=True)
    all_buckets = all_time.pop("_buckets", {})

    # Walk-forward: hold out the most recent days. If in-sample looks good and
    # out-of-sample does not, the model is fitted to noise.
    oos_cutoff = today - timedelta(days=OUT_OF_SAMPLE_DAYS)
    oos_keys, is_keys = [], []
    for k in acc_days:
        try:
            (oos_keys if date.fromisoformat(k) > oos_cutoff else is_keys).append(k)
        except Exception:
            continue
    walk_forward = {
        "outOfSampleDays": OUT_OF_SAMPLE_DAYS,
        "inSample": _aggregate_days(acc_days, is_keys),
        "outOfSample": _aggregate_days(acc_days, oos_keys),
    }

    delta = None
    if current["accuracyPct"] is not None and previous["accuracyPct"] is not None:
        delta = round(current["accuracyPct"] - previous["accuracyPct"], 1)

    trend = sorted(
        (_week_bucket(acc_days, w) for w in {v.get("week") for v in acc_days.values() if v.get("week")}),
        key=lambda b: b["week"],
    )[-8:]

    notes = []
    if previous["resolved"] < MIN_ACCURACY_SAMPLE:
        notes.append(
            f"Previous week has only {previous['resolved']} resolved prediction(s); "
            f"{MIN_ACCURACY_SAMPLE} needed before a percentage is shown."
        )
    if delta is not None:
        direction = "improved" if delta > 0 else ("declined" if delta < 0 else "held flat")
        notes.append(
            f"Historical hit rate {direction} {abs(delta)} pts vs the prior week "
            f"({previous['accuracyPct']}% → {current['accuracyPct']}%)."
        )
    if all_time["unresolved"]:
        notes.append(
            f"{all_time['unresolved']} prediction(s) never reached exit or stop and are "
            "excluded from the percentage rather than counted as wins."
        )
    if all_time.get("unscoreable"):
        notes.append(
            f"{all_time['unscoreable']} prediction(s) could not be scored point-in-time "
            "(no intraday bars after the signal timestamp) and are excluded entirely."
        )
    if all_time.get("expectancyR") is not None:
        notes.append(
            f"Expectancy {all_time['expectancyR']}R per resolved trade "
            f"(net of {COST_BPS} bps costs) — {all_time['verdict']}. "
            f"At {all_time.get('rewardRisk')}:1 reward/risk this geometry only needs "
            f"{all_time.get('breakevenWinPct')}% wins to break even, so hit rate alone "
            "is the wrong target."
        )
    notes.extend(_calibration_notes(all_buckets))
    wf_in = walk_forward["inSample"].get("expectancyR")
    wf_out = walk_forward["outOfSample"].get("expectancyR")
    if wf_in is not None and wf_out is not None and wf_in > 0 and wf_out <= 0:
        notes.append(
            f"Warning: in-sample expectancy is {wf_in}R but out-of-sample is {wf_out}R. "
            "That gap is the signature of fitting to noise."
        )
    notes.append(
        "Historical measurement over resolved predictions only, scored point-in-time "
        "against bars after each signal. Past results are not a forecast and no success "
        "rate is guaranteed."
    )

    acc_meta = history.get("accuracy") or {}
    excluded_map = acc_meta.get("excludedWeekend") or {}
    weekend_days = sorted(excluded_map.keys())
    weekend_preds = sum(excluded_map.values())
    if weekend_preds:
        notes.insert(0, (
            f"Excluded {weekend_preds} prediction(s) stored under {len(weekend_days)} "
            "weekend date(s): markets are closed, so no bar exists to score them."
        ))

    return {
        "basis": "resolved predictions only, scored point-in-time against bars AFTER each "
                 "signal timestamp (exit_first = win, stop_first = loss; a bar spanning both "
                 "resolves as a stop). Unresolved, unscoreable, and Saturday/Sunday-dated "
                 "predictions are excluded, never counted as wins.",
        "primaryMetric": "expectancyR",
        "minSample": MIN_ACCURACY_SAMPLE,
        "costBps": COST_BPS,
        "forwardWindowMinutes": FORWARD_WINDOW_MINUTES or "to session close",
        "excludedWeekendDays": sorted(weekend_days),
        "excludedWeekendPredictions": weekend_preds,
        "currentWeek": current,
        "previousWeek": previous,
        "weekOverWeekDeltaPts": delta,
        "allTime": all_time,
        "weeklyTrend": trend,
        "walkForward": walk_forward,
        "breakdown": {
            "confidence": _rank_buckets(all_buckets, "confidence"),
            "rating": _rank_buckets(all_buckets, "rating"),
            "session": _rank_buckets(all_buckets, "session"),
            "vixRegime": _rank_buckets(all_buckets, "vix"),
            "bias": _rank_buckets(all_buckets, "bias"),
            "symbol": _rank_buckets(all_buckets, "symbol"),
        },
        "notes": notes,
    }


def fetch_accuracy(validate=True):
    """Score retained predictions point-in-time and report hit rate + expectancy.

    Validation needs INTRADAY bars: a prediction is scored only against bars
    after its signal timestamp, and daily bars carry no intraday timestamps to
    compare against. Yahoo serves ~7 days of 1-minute history, which is why raw
    prediction retention is 7 days — the two windows are deliberately matched.
    Anything older is reported `unscoreable` rather than graded on a day range
    that includes pre-signal price action.
    """
    history = _history_load()
    today_key = date.today().isoformat()
    warnings_out = []

    if validate:
        symbols = sorted({
            p.get("symbol", "")
            for payload in (history.get("days") or {}).values()
            for p in payload.get("predictions", [])
            if p.get("symbol")
        })
        if symbols:
            frames = _fetch_intraday_frames(symbols[:ACCURACY_SYMBOL_LIMIT], "1m", "7d")
            if frames:
                history = _update_prediction_history(history, today_key, frames)
                _history_save(history)        # prunes, and absorbs the rollup first
                history = _history_load()
            else:
                warnings_out.append("No intraday bars returned; reporting stored outcomes unchanged.")
                if _LAST_YFINANCE_ERROR:
                    warnings_out.append(f"Provider error: {_LAST_YFINANCE_ERROR}")
                    hint = _provider_hint(_LAST_YFINANCE_ERROR)
                    if hint:
                        warnings_out.append(f"Provider hint: {hint}")
        else:
            warnings_out.append("No stored predictions to validate yet.")

    _accuracy_absorb(history)
    summary = _accuracy_summary(history)
    summary["generatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary["validated"] = bool(validate)
    summary["rawRetentionDays"] = DAYTRADE_HISTORY_DAYS
    summary["rollupRetentionDays"] = ACCURACY_RETENTION_DAYS
    summary["warnings"] = warnings_out
    summary["disclaimer"] = (
        "Read-only historical measurement of past scanner predictions. "
        "Not financial advice, not a forecast, and no guaranteed success rate."
    )
    return summary


def fetch_speculative_daytrade(limit=5, validate_history=False):
    interval, period = "1m", "1d"
    today_key = date.today().isoformat()
    limit = max(1, min(limit, 8))
    cache_key = (limit, bool(validate_history))
    now = time.time()
    cached = _spec_daytrade_cache.get(cache_key)
    if cached and now - cached["t"] < SPEC_DAYTRADE_TTL:
        return cached["data"]

    history = _history_load()
    existing_today = history.get("days", {}).get(today_key, {})
    existing_today_count = len(existing_today.get("predictions", []) or [])

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
                # Tag geometry and regime at signal time so later analysis can
                # slice outcomes without re-deriving conditions after the fact.
                setup.update(_planned_geometry(setup))
                setup["vixRegime"] = _vix_regime()
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

    prediction_day = today_key
    if top:
        counts = {}
        for row in top:
            k = row.get("signalDayPT")
            if k:
                counts[k] = counts.get(k, 0) + 1
        if counts:
            prediction_day = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))[-1][0]
    if not _is_trading_day(prediction_day):
        # Scanning on a weekend returns the last session's candles; filing them
        # under Sat/Sun would create a row that can never be scored.
        prediction_day = _previous_trading_day(date.fromisoformat(prediction_day)).isoformat()

    # After-market pass can optionally validate prior predictions against realized
    # intraday highs/lows. Never overwrite today's stored predictions when the
    # provider returns no candle data (e.g., DNS outage, holiday, rate limit).
    if validate_history:
        history_symbols = sorted({
            p.get("symbol", "")
            for payload in history.get("days", {}).values()
            for p in payload.get("predictions", [])
            if p.get("symbol")
        }) or [p.get("symbol") for p in top if p.get("symbol")]
        frames_5d = _fetch_intraday_frames(history_symbols[:8], "1m", "5d")
        history = _update_prediction_history(history, today_key, frames_5d)
        # Persist outcome updates even if today's live scan yields no data.
        if not top:
            _history_save(history)

    warnings_out = []
    # Late-scan guard: a signal minutes before the close cannot resolve a
    # multi-percent target, so every such prediction ends up unresolved.
    late = [x for x in top if x.get("lateSignal")]
    if late:
        mins = [x.get("minutesToClose") for x in late if isinstance(x.get("minutesToClose"), (int, float))]
        soonest = min(mins) if mins else None
        warnings_out.append(
            f"{len(late)} of {len(top)} signals fired within {MIN_MINUTES_TO_CLOSE} minutes of the "
            f"13:00 PT close (closest: {soonest} min). Too little runway to reach target; "
            "scan mid-session for scoreable predictions."
        )
    tight = [x for x in top if x.get("stopTooTight")]
    if tight:
        warnings_out.append(
            f"{len(tight)} of {len(top)} setups have a stop closer than {MIN_STOP_ATR_MULT}x ATR "
            "and will likely be stopped out by normal noise."
        )
    if not top:
        warnings_out.append("No intraday candle data returned; leaving stored prediction history unchanged.")
        if _LAST_YFINANCE_ERROR:
            warnings_out.append(f"Provider error: {_LAST_YFINANCE_ERROR}")
            hint = _provider_hint(_LAST_YFINANCE_ERROR)
            if hint:
                warnings_out.append(f"Provider hint: {hint}")
    else:
        if prediction_day != today_key:
            warnings_out.append(f"Signal candles map to {prediction_day} (PT); storing predictions under that trading day instead of {today_key}.")
        existing_day = history.get("days", {}).get(prediction_day, {})
        existing_day_count = len(existing_day.get("predictions", []) or [])
        if existing_day_count and prediction_day != today_key:
            warnings_out.append(f"Predictions already exist for {prediction_day}; leaving stored history unchanged.")
        else:
            day_payload = {
                "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "method": "NandaEdge speculative 1m candle skill",
                "predictions": [_compact_setup(x) for x in top],
            }
            merged = _history_merge_day(prediction_day, day_payload)
            if merged is not None:
                history = merged
            else:
                history.setdefault("days", {})[prediction_day] = day_payload

    lightweight_top = [_compact_live_setup(x) for x in top]
    active_alerts = [x for x in lightweight_top if x.get("alertStatus") not in ("invalid", "exit_hit")]
    # Refresh the rollup in-memory too: a read-only pass (no save, so no prune)
    # must still report accuracy that includes the raw days currently on disk.
    _accuracy_absorb(history)
    compact_history = _compact_history(history)
    accuracy = _accuracy_summary(history)
    data = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "interval": interval,
        "period": period,
        "universe": scanned_universe,
        "results": lightweight_top,
        "categories": categories,
        "activeAlerts": active_alerts,
        "history": compact_history,
        "accuracy": accuracy,
        "learningNotes": _learning_notes(history),
        "warnings": warnings_out,
        "storedPredictionsToday": existing_today_count,
        "predictionDay": prediction_day,
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
            _trim_cache(_tech_cache, 256)
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
            _trim_cache(_fcst_cache, 256)
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
        spot_info, provider_log = _quiet_call(lambda: tk.fast_info)
        if provider_log:
            _set_last_yfinance_error(provider_log.splitlines()[0])
        spot = float(getattr(spot_info, "last_price", 0) or 0)
    except Exception:
        spot = 0.0
    if spot <= 0:
        try:
            hist, provider_log = _quiet_call(lambda: tk.history(period="5d", interval="1d", auto_adjust=False))
            if provider_log:
                _set_last_yfinance_error(provider_log.splitlines()[0])
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
    if delta_min is None:
        delta_min = 0.0
    if delta_max is None:
        delta_max = 1.0
    if delta_min > delta_max:
        delta_min, delta_max = delta_max, delta_min
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
                _trim_cache(_hist_cache, 256)
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
            _trim_cache(_earn_cache, 256)
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

    # ── Account endpoints (POST) ──────────────────────
    MAX_BODY = 8 * 1024          # these payloads are tiny; refuse anything larger

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > self.MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def _json(self, code, payload):
        self._send(code, "application/json", json.dumps(payload).encode())

    def _site_base(self):
        """Public URL used to build verification links."""
        return (os.environ.get("PUBLIC_SITE_URL")
                or "https://nanda0210.github.io/edge-advisor").rstrip("/")

    def do_POST(self):
        path = urlparse(self.path).path
        if not ACCOUNTS_ENABLED:
            self._json(503, {"error": "accounts_unavailable",
                             "message": "User accounts are not configured on this server."})
            return

        body = self._read_json()
        if body is None:
            self._json(400, {"error": "bad_request", "message": "Expected a small JSON body."})
            return

        ip = _client_ip(self)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""

        try:
            if path == "/api/signup":
                # Rate-limited with the same counter as password attempts so
                # signup cannot be used to bypass the lockout.
                if _rate_limited(ip):
                    self._json(429, {"error": "rate_limited"}); return
                try:
                    _, token = accounts.create_user(email, password)
                except ValueError as e:
                    _record_fail(ip)
                    self._json(400, {"error": "invalid", "message": str(e)}); return
                link = f"{self._site_base()}/?verify={token}"
                sent, fallback = accounts.send_link(email, link, "verify")
                self._json(200, {"ok": True, "emailSent": sent,
                                 "verifyLink": fallback,
                                 "message": "Account created. Check your email to verify it."})
                return

            if path == "/api/login":
                if _rate_limited(ip):
                    self._json(429, {"error": "rate_limited"}); return
                user, err = accounts.authenticate(email, password)
                if not user:
                    _record_fail(ip)
                    self._json(401, {"error": "unauthorized", "message": err}); return
                token = accounts.start_session(user["id"])
                self._json(200, {"ok": True, "session": token, "email": user["email"],
                                 "role": "user",
                                 "sessionMinutes": accounts.SESSION_TTL_MIN})
                return

            if path == "/api/logout":
                accounts.end_session(body.get("session") or "")
                self._json(200, {"ok": True}); return

            if path == "/api/forgot":
                # Always reports success: a different response for unknown
                # addresses would reveal who has an account.
                token = accounts.set_reset_token(email)
                payload = {"ok": True,
                           "message": "If that email has an account, a reset link is on its way."}
                if token:
                    link = f"{self._site_base()}/?reset={token}"
                    sent, fallback = accounts.send_link(email, link, "reset")
                    payload["emailSent"] = sent
                    if fallback:
                        payload["resetLink"] = fallback
                self._json(200, payload); return

            if path == "/api/reset":
                try:
                    accounts.reset_password(body.get("token") or "", password)
                except ValueError as e:
                    self._json(400, {"error": "invalid", "message": str(e)}); return
                self._json(200, {"ok": True,
                                 "message": "Password updated. You can sign in now."})
                return

            if path == "/api/verify":
                user = accounts.consume_token(body.get("token") or "", "verify")
                if not user:
                    self._json(400, {"error": "invalid",
                                     "message": "That link is invalid or has expired."}); return
                self._json(200, {"ok": True, "email": user["email"],
                                 "message": "Email verified. You can sign in now."})
                return
        except Exception as e:
            print(f"  WARN account endpoint {path}: {type(e).__name__}: {e}", flush=True)
            self._json(500, {"error": "server_error"}); return

        self._json(404, {"error": "not_found"})

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
            self._send(200, "application/json", json.dumps({
                "ok": True,
                "authRequired": bool(AUTH_HASH or DEMO_HASH or ACCOUNTS_ENABLED),
                "demoAvailable": bool(DEMO_HASH),
                "userLoginAvailable": bool(AUTH_HASH) or ACCOUNTS_ENABLED,
                "accountsEnabled": ACCOUNTS_ENABLED,
                "sessionMinutes": SESSION_MINUTES_BY_ROLE,
            }).encode())
            return
        if path == "/docs/whatsapp-stock-feed":
            try:
                body = open(os.path.join(BASE_DIR, "WHATSAPP_STOCK_FEED.md"), "rb").read()
                self._send(200, "text/markdown; charset=utf-8", body)
            except FileNotFoundError:
                self._send(404, "text/plain", b"WHATSAPP_STOCK_FEED.md not found")
            return

        # All endpoints below require auth (when a hash is configured)
        ok, status, role = _auth_state(self)
        if path == "/auth":
            payload = {
                "ok": ok,
                "status": status,
                "role": role,
                "authRequired": bool(AUTH_HASH or DEMO_HASH),
                "sessionMinutes": SESSION_MINUTES_BY_ROLE.get(role or "", 15),
            }
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

        elif path == "/accuracy":
            # ?validate=0 reads stored outcomes without a provider call.
            validate = (qs.get("validate", ["1"])[0] or "1").lower() in {"1", "true", "yes"}
            print(f"  [{time.strftime('%H:%M:%S')}] /accuracy validate={validate} ...", end=" ", flush=True)
            t0 = time.time()
            data = fetch_accuracy(validate)
            cw = (data.get("currentWeek") or {}).get("accuracyPct")
            pw = (data.get("previousWeek") or {}).get("accuracyPct")
            print(f"this={cw}% prev={pw}% in {time.time()-t0:.1f}s", flush=True)
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

# Threaded server so /technicals doesn't block /quotes.
# Concurrency is bounded: one unbounded thread per connection let a burst (or a
# slow client holding sockets open) exhaust memory on a small cloud instance.
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "32"))
REQUEST_SOCKET_TIMEOUT  = float(os.environ.get("REQUEST_SOCKET_TIMEOUT", "120"))
_REQUEST_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)


class ThreadedServer(HTTPServer):
    daemon_threads = True

    def process_request(self, request, client_address):
        if not _REQUEST_SLOTS.acquire(timeout=5):
            # Shed load rather than queue unboundedly.
            print("  WARN request rejected: concurrency limit reached", flush=True)
            self.shutdown_request(request)
            return
        threading.Thread(target=self._handle, args=(request, client_address), daemon=True).start()

    def _handle(self, request, client_address):
        try:
            # Without a timeout a half-open connection pins a thread forever.
            request.settimeout(REQUEST_SOCKET_TIMEOUT)
            self.finish_request(request, client_address)
        except (socket.timeout, TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.shutdown_request(request)
            _REQUEST_SLOTS.release()

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
    print("  Press Ctrl+C to stop.\n", flush=True)

    server = ThreadedServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
