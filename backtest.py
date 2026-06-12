#!/usr/bin/env python3
"""
Backtester — replay historical data through the bot's exact trading logic.

Why this exists: tweaking CONFIG and waiting weeks of real time is a
terrible feedback loop. This replays the last N days of hourly prices
through the SAME pair-selection, signal, and risk code the live bot runs
(it imports bot.py — there is no separate "backtest version" that could
drift out of sync), and prints a scorecard: P/L, after-tax P/L, max
drawdown, fees paid, and the buy-and-hold benchmark to beat.

Usage:
    python3 backtest.py            # 60 days
    python3 backtest.py 30         # custom number of days (max 90 hourly)

Downloaded data is cached in backtest_data.json so re-runs are instant —
delete that file to fetch fresh data. To test different settings, edit
CONFIG at the top of bot.py and run this again. Walk-forward design:
pairs are re-selected each simulated day using only data available up to
that moment (no peeking into the future).
"""

import contextlib
import csv
import io
import json
import os
import sys
import time

import bot

CACHE_FILE = "backtest_data.json"
DEFAULT_DAYS = 60
HOURS_PER_DAY = 24


# ============================================================================
# Data: download once (throttled), cache to disk
# ============================================================================


def load_or_fetch(days: int) -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            cache = json.load(f)
        print(f"[data] using cached data ({cache['days']}d, fetched "
              f"{cache['fetched']}) — delete {CACHE_FILE} to re-download")
        return cache["data"]

    print(f"[data] downloading {days}d hourly history for "
          f"{len(bot.CONFIG['UNIVERSE'])} coins (~3 min, throttled)...")
    saved_lookback = bot.CONFIG["LOOKBACK_DAYS"]
    bot.CONFIG["LOOKBACK_DAYS"] = days  # fetch_history reads this
    try:
        data = {}
        total = len(bot.CONFIG["UNIVERSE"])
        for i, (sym, cid) in enumerate(bot.CONFIG["UNIVERSE"].items(), 1):
            print(f"  [data] {i}/{total}: {sym}...", flush=True)
            series = bot.fetch_history(cid)
            if series is None:
                sys.exit(f"could not download history for {sym}; try again")
            data[sym] = series
            time.sleep(bot.CONFIG["HISTORY_REQUEST_GAP_SEC"])
    finally:
        bot.CONFIG["LOOKBACK_DAYS"] = saved_lookback

    with open(CACHE_FILE, "w") as f:
        json.dump({"days": days, "data": data,
                   "fetched": time.strftime("%Y-%m-%d %H:%M")}, f)
    return data


# ============================================================================
# Simulation: walk forward hour by hour
# ============================================================================


def run_backtest(data: dict) -> dict:
    # Never touch the live bot's files.
    bot.CONFIG["STATE_FILE"] = "backtest_state.json"
    bot.CONFIG["TRADES_FILE"] = "backtest_trades.csv"
    bot.CONFIG["PAIRS_FILE"] = "backtest_pairs.csv"
    for f in ("backtest_trades.csv", "backtest_pairs.csv",
              "backtest_state.json"):
        if os.path.exists(f):
            os.remove(f)

    # Align all series to the same length.
    n = min(len(s) for s in data.values())
    data = {sym: series[-n:] for sym, series in data.items()}
    warmup = bot.CONFIG["LOOKBACK_DAYS"] * HOURS_PER_DAY  # selection window

    if n <= warmup + HOURS_PER_DAY:
        sys.exit(f"not enough data: {n} hourly points, need > {warmup + 24}")

    state = bot.default_state()
    curve = []
    sim_days = (n - warmup) // HOURS_PER_DAY + 1

    print(f"[sim] replaying {n - warmup} hourly ticks "
          f"(~{sim_days} trading days, first {warmup} points reserved "
          f"for the initial lookback)...")

    sink = io.StringIO()  # swallow the bot's per-tick console chatter
    for t in range(warmup, n):
        with contextlib.redirect_stdout(sink):
            # Re-select pairs each simulated day, using ONLY past data.
            if (t - warmup) % HOURS_PER_DAY == 0 and not state["halted"]:
                window = {sym: series[t - warmup:t]
                          for sym, series in data.items()}
                bot.run_pair_selection(state, window)
            prices = {sym: series[t] for sym, series in data.items()}
            bot.run_tick(state, prices)
        curve.append(bot.portfolio_equity(state, prices))

    return {"state": state, "curve": curve,
            "btc_start": data["BTC"][warmup], "btc_end": data["BTC"][-1],
            "ticks": n - warmup, "days": (n - warmup) / HOURS_PER_DAY}


# ============================================================================
# Scorecard
# ============================================================================


def report(result: dict) -> None:
    state, curve = result["state"], result["curve"]
    start = state["starting_equity"]
    final = curve[-1]

    peak, max_dd = curve[0], 0.0
    for x in curve:
        peak = max(peak, x)
        max_dd = max(max_dd, (peak - x) / peak)

    trades, fees, reasons = [], 0.0, {}
    if os.path.exists("backtest_trades.csv"):
        with open("backtest_trades.csv") as f:
            trades = list(csv.DictReader(f))
        fees = sum(float(t["fee_usd"]) for t in trades)
        for t in trades:
            if t["side"] == "sell":
                reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    realized = state["realized_pnl_total"]
    est_tax = max(0.0, realized) * bot.CONFIG["TAX_RATE_PCT"]
    pnl = (final - start) / start * 100
    after_tax = (final - est_tax - start) / start * 100
    hodl = (result["btc_end"] - result["btc_start"]) / result["btc_start"] * 100

    print()
    print("=" * 62)
    print("  BACKTEST SCORECARD")
    print("=" * 62)
    print(f"  period                {result['days']:.1f} days "
          f"({result['ticks']} hourly ticks)")
    print(f"  starting equity       ${start:,.2f}")
    print(f"  final equity          ${final:,.2f}")
    print(f"  total P/L             {pnl:+.2f}%")
    print(f"  P/L after est. tax    {after_tax:+.2f}%  "
          f"(tax ${est_tax:,.2f} @ {bot.CONFIG['TAX_RATE_PCT']:.0%})")
    print(f"  max drawdown          {max_dd:.1%}"
          f"{'   *** KILL SWITCH FIRED — HALTED ***' if state['halted'] else ''}")
    print(f"  trades                {len(trades)} "
          f"(fees paid ${fees:,.2f})")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"      exits: {count:3d} x {reason}")
    print("-" * 62)
    print(f"  buy & hold BTC        {hodl:+.2f}%   <- the benchmark to beat")
    print("=" * 62)
    print("  details: backtest_trades.csv, backtest_pairs.csv")
    print("  to test other settings: edit CONFIG in bot.py, run this again")


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    if not 8 <= days <= 90:
        sys.exit("days must be between 8 and 90 (hourly data limit)")
    data = load_or_fetch(days)
    report(run_backtest(data))


if __name__ == "__main__":
    main()
