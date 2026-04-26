#!/usr/bin/env python3
"""
NandaEdge Data Server — v2.2
  Dashboard   → /
  Quotes      → /quotes        ?symbols=NVDA,TSLA,...   realtime quotes
  Technicals  → /technicals    ?symbols=...             EMA/RSI/MACD/BB/ATR/VWAP
  Forecast    → /forecast      ?symbols=...             GBM projections (1w..5y)
  Fear/Greed  → /feargreed                              CNN proxy
  Cloud-ready: reads PORT/HOST from env; binds 0.0.0.0 when PORT is set.
"""
import os, sys, signal, time, json, math, socket, warnings, urllib.request, threading, hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

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
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "index.html")

WATCH = ["NVDA","TSLA","PLTR","AMD","MU","CRWD","INTC","IONQ","RGTI"]
MARKETS = [
    "^GSPC","^IXIC","^DJI","^RUT","^VIX","^TNX",
    "DX-Y.NYB","CL=F","GC=F","BTC-USD","ETH-USD",
    "^N225","^HSI","000001.SS","^GDAXI","^FTSE","^FCHI","SOL-USD",
]
ALL = WATCH + MARKETS

# ── Caching — per-symbol so user watchlists share cache ───
_tech_cache  = {}                     # sym -> {"t": float, "data": dict}
_fcst_cache  = {}                     # sym -> {"t": float, "data": dict}
_opts_cache  = {}                     # (sym, type) -> {"t": float, "data": list}
_rate_cache  = {"t": 0, "rate": 0.045}  # risk-free rate from ^TNX
_fg_cache    = {"t": 0, "data": None}
TECH_TTL = 300                        # 5 min
FCST_TTL = 3600                       # 1 hour — forecasts don't need to update often
OPTS_TTL = 600                        # 10 min — options chains move fast but pulling is expensive
RATE_TTL = 3600                       # 1 hour for risk-free rate
FG_TTL   = 600                        # 10 min

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
def fetch_options(sym, opt_type, dte_min, dte_max, delta_min, delta_max, top_n=5):
    """
    Returns top-N candidate contracts matching the skill's gates.
    opt_type: 'puts' (cash-secured puts) or 'calls' (covered calls)
    """
    cache_key = (sym, opt_type, dte_min, dte_max, round(delta_min, 2), round(delta_max, 2))
    now = time.time()
    c = _opts_cache.get(cache_key)
    if c and (now - c["t"] < OPTS_TTL):
        return c["data"]

    import yfinance as yf
    try:
        tk = yf.Ticker(sym)
        spot_info = tk.fast_info
        S = float(spot_info.last_price or 0)
        if S <= 0:
            return {"symbol": sym, "type": opt_type, "spot": None, "candidates": [], "error": "no spot price"}
    except Exception as e:
        return {"symbol": sym, "type": opt_type, "spot": None, "candidates": [], "error": f"ticker fetch: {e}"}

    r = get_risk_free_rate()

    try:
        expiries = list(tk.options or ())
    except Exception as e:
        return {"symbol": sym, "type": opt_type, "spot": S, "candidates": [], "error": f"no options listed: {e}"}
    if not expiries:
        return {"symbol": sym, "type": opt_type, "spot": S, "candidates": [], "error": "no expiries available"}

    today = time.strftime("%Y-%m-%d")
    today_t = time.mktime(time.strptime(today, "%Y-%m-%d"))

    candidates = []
    for exp in expiries:
        try:
            exp_t = time.mktime(time.strptime(exp, "%Y-%m-%d"))
        except Exception:
            continue
        dte = max(0, int(round((exp_t - today_t) / 86400.0)))
        if dte < dte_min or dte > dte_max:
            continue
        try:
            chain = tk.option_chain(exp)
            df = chain.puts if opt_type == "puts" else chain.calls
        except Exception:
            continue

        for _, row in df.iterrows():
            try:
                strike = float(row["strike"])
                bid    = float(row.get("bid", 0) or 0)
                ask    = float(row.get("ask", 0) or 0)
                last   = float(row.get("lastPrice", 0) or 0)
                oi     = int(row.get("openInterest", 0) or 0)
                vol    = int(row.get("volume", 0) or 0)
                iv     = float(row.get("impliedVolatility", 0) or 0)
            except Exception:
                continue

            if iv <= 0 or strike <= 0:
                continue
            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last
            if mid <= 0:
                continue
            spread_pct = ((ask - bid) / mid * 100.0) if (mid > 0 and ask > bid > 0) else None

            T = dte / 365.0
            if opt_type == "puts":
                delta = bs_put_delta(S, strike, T, r, iv)
            else:
                delta = bs_call_delta(S, strike, T, r, iv)
            if delta is None:
                continue
            delta_abs = abs(delta)
            if delta_abs < delta_min or delta_abs > delta_max:
                continue

            # Liquidity flags (don't reject — surface them)
            liq_oi   = oi >= 500
            liq_spread = (spread_pct is not None) and (spread_pct <= 5.0)

            if opt_type == "puts":
                breakeven = strike - mid
                cap_required = strike * 100
                ann_return = (mid / strike) * (365.0 / max(dte, 1)) * 100.0 if dte > 0 else None
            else:
                breakeven = strike + mid    # call BE for short call from cost-basis perspective: strike + premium
                cap_required = None         # CC sized against held shares
                ann_return = (mid / S) * (365.0 / max(dte, 1)) * 100.0 if dte > 0 and S > 0 else None

            candidates.append({
                "symbol": sym,
                "type": opt_type,
                "expiry": exp,
                "dte": dte,
                "strike": round(strike, 2),
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "mid": round(mid, 2),
                "lastPrice": round(last, 2),
                "openInterest": oi,
                "volume": vol,
                "iv": round(iv * 100, 1),
                "delta": round(delta, 3),
                "deltaAbs": round(delta_abs, 3),
                "breakeven": round(breakeven, 2),
                "capitalRequired": cap_required,
                "annualizedReturnPct": round(ann_return, 2) if ann_return is not None else None,
                "spreadPct": round(spread_pct, 2) if spread_pct is not None else None,
                "liqOK_OI": liq_oi,
                "liqOK_Spread": liq_spread,
                "liqOK": bool(liq_oi and liq_spread),
            })

    # Rank: liquidity passed first, then by annualized return desc
    candidates.sort(key=lambda c: (
        0 if c["liqOK"] else 1,
        -(c["annualizedReturnPct"] or 0)
    ))

    out = {
        "symbol": sym,
        "type": opt_type,
        "spot": round(S, 2),
        "riskFreeRate": round(r, 4),
        "filter": {
            "dte_min": dte_min, "dte_max": dte_max,
            "delta_min": delta_min, "delta_max": delta_max,
        },
        "candidates": candidates[:top_n],
        "totalFound": len(candidates),
    }
    _opts_cache[cache_key] = {"t": now, "data": out}
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
    print(f"\n  NandaEdge Server v2.1 — {HOST}:{PORT}", flush=True)

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
