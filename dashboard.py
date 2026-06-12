#!/usr/bin/env python3
"""
CodeTrade pixel dashboard — a retro 8-bit web UI for watching the bot.

Reads the same state.json / trades.csv the bot writes (run it in the same
folder, alongside the bot) and serves a green-phosphor, CRT-style page with
a pixelated equity chart, sleeve panels, and the trade log. Auto-refreshes
every 5 seconds. Pure Python stdlib — nothing extra to install.

Usage:
    python3 dashboard.py            # then open http://localhost:8000
    python3 dashboard.py 9999       # custom port
"""

import csv
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"
DEFAULT_PORT = 8000
MAX_TRADES_SHOWN = 12
MAX_CHART_POINTS = 288  # last ~24h of 5-min ticks


def read_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}  # bot may be mid-write; the next refresh will catch up


def read_trades() -> list:
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE) as f:
            rows = list(csv.DictReader(f))
        return rows[-MAX_TRADES_SHOWN:][::-1]  # newest first
    except OSError:
        return []


def build_payload() -> dict:
    """Everything the page needs, assembled from the bot's files."""
    state = read_state()
    if not state:
        return {"ready": False}

    history = state.get("equity_history", [])
    prices = state.get("price_history", {})
    starting = state.get("starting_equity", 0) or 1
    peak = state.get("peak_equity", starting)
    equity = history[-1][1] if history else starting

    sleeves = []
    for pair, s in state.get("sleeves", {}).items():
        h = s.get("holding")
        value = s.get("cash", 0.0)
        position = "FLAT"
        if h:
            last = prices.get(h["symbol"], [h["entry_price"]])[-1]
            value += h["amount"] * last
            pnl = (last - h["entry_price"]) / h["entry_price"] * 100
            position = (f"LONG {h['symbol']} {h['amount']:.4f} "
                        f"@ {h['entry_price']:g} ({pnl:+.2f}%)")
        sleeves.append({
            "pair": pair,
            "z": s.get("last_z"),
            "position": position,
            "value": value,
            "in_position": h is not None,
        })

    realized = state.get("realized_pnl_total", 0.0)
    tax_rate = state.get("tax_rate", 0.24)
    est_tax = max(0.0, realized) * tax_rate

    return {
        "ready": True,
        "halted": state.get("halted", False),
        "equity": equity,
        "starting": starting,
        "pnl_pct": (equity - starting) / starting * 100,
        "realized": realized,
        "est_tax": est_tax,
        "tax_rate": tax_rate,
        "after_tax_pnl_pct": (equity - est_tax - starting) / starting * 100,
        "peak": peak,
        "drawdown_pct": (peak - equity) / peak * 100 if peak else 0.0,
        "kill_level": peak * 0.85,
        "free_cash": state.get("free_cash", 0.0),
        "last_selection": state.get("last_selection_date"),
        "history": history[-MAX_CHART_POINTS:],
        "sleeves": sleeves,
        "trades": read_trades(),
    }


# ============================================================================
# The page — all pixel styling and chart drawing happens client-side
# ============================================================================

PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CODETRADE</title>
<style>
  @font-face { font-family: 'PixelFallback'; src: local('Courier New'); }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #050805; color: #33ff66;
    font-family: 'Press Start 2P', 'PixelFallback', monospace;
    font-size: 10px; padding: 16px; image-rendering: pixelated;
  }
  /* CRT scanlines over everything */
  body::after {
    content: ""; position: fixed; inset: 0; pointer-events: none;
    background: repeating-linear-gradient(0deg,
      rgba(0,0,0,0.25) 0px, rgba(0,0,0,0.25) 1px,
      transparent 1px, transparent 3px);
  }
  h1 { font-size: 18px; letter-spacing: 2px; text-shadow: 3px 3px #0a3318; }
  .blink { animation: blink 1s steps(2, start) infinite; }
  @keyframes blink { to { visibility: hidden; } }
  .topbar { display: flex; justify-content: space-between;
            align-items: baseline; margin-bottom: 14px; flex-wrap: wrap; }
  .halted { color: #ff3355; }
  .panel {
    border: 3px solid #1d7a3c; padding: 10px; margin-bottom: 14px;
    background: #07120a; box-shadow: 5px 5px 0 #0a2113;
  }
  .panel h2 { font-size: 10px; color: #ffcc33; margin-bottom: 8px;
              text-transform: uppercase; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px,1fr));
           gap: 14px; }
  .stat .label { color: #1d7a3c; font-size: 8px; margin-bottom: 6px; }
  .stat .value { font-size: 14px; }
  .good { color: #33ff66; } .bad { color: #ff3355; } .warn { color: #ffcc33; }
  canvas { width: 100%; height: auto; image-rendering: pixelated;
           background: #04140a; display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 9px; }
  th { color: #1d7a3c; text-align: left; padding: 4px 6px; font-size: 8px; }
  td { padding: 5px 6px; border-top: 2px solid #0a2113; }
  .footer { color: #1d7a3c; font-size: 8px; text-align: center; }
</style>
</head>
<body>
<div class="topbar">
  <h1>&#9608; CODETRADE &#9608;</h1>
  <div id="status" class="blink">BOOTING...</div>
</div>

<div class="panel"><div class="stats" id="stats"></div></div>

<div class="panel">
  <h2>&gt; Equity (last 24h) <span id="chartinfo" style="color:#1d7a3c"></span></h2>
  <canvas id="chart" width="240" height="80"></canvas>
</div>

<div class="panel">
  <h2>&gt; Sleeves</h2>
  <table><thead><tr><th>PAIR</th><th>Z-SCORE</th><th>POSITION</th><th>VALUE</th></tr></thead>
  <tbody id="sleeves"></tbody></table>
</div>

<div class="panel">
  <h2>&gt; Trade log</h2>
  <table><thead><tr><th>TIME</th><th>PAIR</th><th>SIDE</th><th>COIN</th>
  <th>PRICE</th><th>AMOUNT</th><th>REASON</th></tr></thead>
  <tbody id="trades"></tbody></table>
</div>

<div class="footer">PAPER TRADING ONLY &#183; INSERT COIN TO CONTINUE &#183;
refreshes every 5s</div>

<script>
const usd = x => "$" + x.toLocaleString("en-US",
    {minimumFractionDigits: 2, maximumFractionDigits: 2});

function drawChart(d) {
  // Low-res canvas scaled up by CSS = chunky pixels.
  const c = document.getElementById("chart"), g = c.getContext("2d");
  g.imageSmoothingEnabled = false;
  g.fillStyle = "#04140a"; g.fillRect(0, 0, c.width, c.height);

  const pts = d.history.map(h => h[1]);
  if (pts.length < 2) {
    g.fillStyle = "#1d7a3c";
    g.fillRect(4, c.height/2, c.width-8, 2);
    document.getElementById("chartinfo").textContent = " [waiting for data]";
    return;
  }
  let lo = Math.min(...pts, d.kill_level), hi = Math.max(...pts, d.starting);
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const Y = v => Math.round((1 - (v - lo) / (hi - lo)) * (c.height - 6)) + 3;

  // grid dots
  g.fillStyle = "#0a2113";
  for (let x = 0; x < c.width; x += 12)
    for (let y = 0; y < c.height; y += 10) g.fillRect(x, y, 1, 1);

  // dashed reference lines: start equity (amber), kill level (red)
  g.fillStyle = "#7a6420";
  for (let x = 0; x < c.width; x += 6) g.fillRect(x, Y(d.starting), 3, 1);
  g.fillStyle = "#7a2030";
  for (let x = 0; x < c.width; x += 6) g.fillRect(x, Y(d.kill_level), 3, 1);

  // equity line as 2x2 blocks, column by column
  const up = pts[pts.length-1] >= d.starting;
  g.fillStyle = up ? "#33ff66" : "#ff3355";
  for (let x = 0; x < c.width; x += 2) {
    const i = Math.min(pts.length - 1,
        Math.floor(x / c.width * (pts.length - 1)));
    g.fillRect(x, Y(pts[i]) - 1, 2, 2);
  }
  document.getElementById("chartinfo").textContent = "";
}

function render(d) {
  const st = document.getElementById("status");
  if (!d.ready) {
    st.textContent = "WAITING FOR BOT (no state.json yet)";
    st.className = "blink warn"; return;
  }
  st.textContent = d.halted ? "!! HALTED: KILL SWITCH !!" : "RUNNING";
  st.className = d.halted ? "blink halted" : "good";

  const pnlClass = d.pnl_pct >= 0 ? "good" : "bad";
  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="label">EQUITY</div>
      <div class="value">${usd(d.equity)}</div></div>
    <div class="stat"><div class="label">TOTAL P/L</div>
      <div class="value ${pnlClass}">${d.pnl_pct >= 0 ? "+" : ""}${d.pnl_pct.toFixed(2)}%</div></div>
    <div class="stat"><div class="label">DRAWDOWN (KILL @ 15%)</div>
      <div class="value ${d.drawdown_pct > 10 ? "bad" : "warn"}">${d.drawdown_pct.toFixed(1)}%</div></div>
    <div class="stat"><div class="label">FREE CASH</div>
      <div class="value">${usd(d.free_cash)}</div></div>
    <div class="stat"><div class="label">PAIRS PICKED</div>
      <div class="value">${d.last_selection || "—"}</div></div>
    <div class="stat"><div class="label">REALIZED P/L (CLOSED TRADES)</div>
      <div class="value ${d.realized >= 0 ? "good" : "bad"}">${d.realized >= 0 ? "+" : "-"}${usd(Math.abs(d.realized))}</div></div>
    <div class="stat"><div class="label">EST. TAX @ ${(d.tax_rate*100).toFixed(0)}% (SHORT-TERM)</div>
      <div class="value warn">${usd(d.est_tax)}</div></div>
    <div class="stat"><div class="label">P/L AFTER TAX</div>
      <div class="value ${d.after_tax_pnl_pct >= 0 ? "good" : "bad"}">${d.after_tax_pnl_pct >= 0 ? "+" : ""}${d.after_tax_pnl_pct.toFixed(2)}%</div></div>`;

  drawChart(d);

  document.getElementById("sleeves").innerHTML = d.sleeves.map(s => `
    <tr><td>${s.pair}</td>
    <td class="${Math.abs(s.z ?? 0) >= 2 ? "warn" : ""}">${s.z == null ? "—" : (s.z >= 0 ? "+" : "") + s.z.toFixed(2)}</td>
    <td class="${s.in_position ? "good" : ""}">${s.position}</td>
    <td>${usd(s.value)}</td></tr>`).join("")
    || `<tr><td colspan="4">(no tradeable pairs yet)</td></tr>`;

  document.getElementById("trades").innerHTML = d.trades.map(t => `
    <tr><td>${t.timestamp.slice(5, 16).replace("T", " ")}</td>
    <td>${t.pair}</td>
    <td class="${t.side === "buy" ? "good" : "bad"}">${t.side.toUpperCase()}</td>
    <td>${t.symbol}</td><td>$${t.price}</td><td>${t.amount}</td>
    <td>${t.reason}</td></tr>`).join("")
    || `<tr><td colspan="7">(no trades yet)</td></tr>`;
}

async function tick() {
  try {
    render(await (await fetch("/data")).json());
  } catch (e) {
    const st = document.getElementById("status");
    st.textContent = "DASHBOARD OFFLINE?"; st.className = "blink halted";
  }
}
tick();
setInterval(tick, 5000);
</script>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap"
      rel="stylesheet">
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data":
            body = json.dumps(build_payload()).encode()
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the console quiet
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Pixel dashboard running -> http://localhost:{port}")
    print("(run bot.py in this same folder; Ctrl+C stops the dashboard)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
