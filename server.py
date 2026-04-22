#!/usr/bin/env python3
"""
NandaEdge Data Server
  Dashboard → http://localhost:8765/
  Quotes    → http://localhost:8765/quotes
  Auto-kills any existing process on port 8765 before starting.
  Requires: pip3 install yfinance
"""
import os, sys, signal, time, json, socket, warnings
from http.server import HTTPServer, BaseHTTPRequestHandler

warnings.filterwarnings("ignore")          # suppress SSL / urllib3 noise

PORT     = 8765
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE= os.path.join(BASE_DIR, "index.html")

SYMS = [
    "NVDA","TSLA","PLTR","AMD","MU","CRWD","INTC","IONQ","RGTI",
    "^GSPC","^IXIC","^DJI","^RUT","^VIX","^TNX","DX-Y.NYB",
    "CL=F","GC=F","BTC-USD","ETH-USD",
]

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

# ── Fetch quotes via yfinance ──────────────────────────
def fetch_all():
    import yfinance as yf
    results = []
    tickers = yf.Tickers(" ".join(SYMS))
    for sym in SYMS:
        try:
            fi    = tickers.tickers[sym].fast_info
            price = fi.last_price or 0
            prev  = getattr(fi, "previous_close", None) or \
                    getattr(fi, "regular_market_previous_close", price)
            chg   = price - prev
            chgPct= (chg / prev * 100) if prev else 0
            results.append({
                "symbol":                     sym,
                "regularMarketPrice":         round(price, 4),
                "regularMarketChange":        round(chg,   4),
                "regularMarketChangePercent": round(chgPct,4),
                "regularMarketVolume":        getattr(fi, "three_month_average_volume", None),
                "fiftyTwoWeekHigh":           getattr(fi, "fifty_two_week_high", None),
                "fiftyTwoWeekLow":            getattr(fi, "fifty_two_week_low",  None),
            })
        except Exception as e:
            print(f"  WARN {sym}: {e}", flush=True)
    return {"quoteResponse": {"result": results, "error": None}}

# ── HTTP Handler ───────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            try:
                body = open(HTML_FILE, "rb").read()
                self._send(200, "text/html; charset=utf-8", body)
            except FileNotFoundError:
                self._send(404, "text/plain", b"index.html not found")

        elif path == "/quotes":
            print(f"  [{time.strftime('%H:%M:%S')}] Fetching quotes...", end=" ", flush=True)
            t0   = time.time()
            data = fetch_all()
            n    = len(data["quoteResponse"]["result"])
            print(f"{n} symbols in {time.time()-t0:.1f}s", flush=True)
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

# ── Main ───────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n  NandaEdge Server — port {PORT}", flush=True)

    if not port_free(PORT):
        print(f"  Port {PORT} in use — killing...", flush=True)
        kill_port(PORT)

    if not port_free(PORT):
        print(f"  ERROR: port {PORT} still busy. Run: kill $(lsof -ti:{PORT})", flush=True)
        sys.exit(1)

    print(f"  Dashboard → http://localhost:{PORT}/", flush=True)
    print(f"  Quotes    → http://localhost:{PORT}/quotes", flush=True)
    print(f"  Press Ctrl+C to stop.\n", flush=True)

    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
