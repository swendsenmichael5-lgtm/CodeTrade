#!/usr/bin/env python3
"""
BTC/USD Paper Trading Bot
=========================

Simulates day trading Bitcoin with fake money using real live market data.

Strategy: moving-average crossover.
  - BUY  when the short MA crosses ABOVE the long MA (bullish crossover)
  - SELL when the short MA crosses BELOW the long MA (bearish crossover)
  - Hard stop-loss: auto-sell if price drops STOP_LOSS_PCT below entry.

PAPER TRADING ONLY — no real orders are ever placed. The bot only *reads*
public price data and simulates trades against a virtual balance.

Usage:
    python3 bot.py

Stop with Ctrl+C. State is saved to state.json after every cycle, so you
can stop and restart without losing your balance or open position.
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ============================================================================
# CONFIG — tweak strategy parameters here
# ============================================================================

CONFIG = {
    # --- Market ---
    "TICKER": "BTC",              # display name only
    "COINGECKO_ID": "bitcoin",    # CoinGecko coin id
    "VS_CURRENCY": "usd",

    # --- Polling ---
    "POLL_INTERVAL_SEC": 60,      # fetch a new price every N seconds

    # --- Strategy (moving-average crossover) ---
    "SHORT_MA": 10,               # short moving-average length (periods)
    "LONG_MA": 30,                # long moving-average length (periods)

    # --- Risk management ---
    "STARTING_BALANCE": 10_000.0, # virtual USD to start with
    "TRADE_SIZE_PCT": 0.95,       # use at most 95% of balance per position
    "STOP_LOSS_PCT": 0.02,        # auto-sell if price drops 2% below entry
    "FEE_PCT": 0.001,             # 0.1% fee per trade (mimics exchange fees)

    # --- Files ---
    "STATE_FILE": "state.json",
    "TRADES_FILE": "trades.csv",

    # --- Networking ---
    "MAX_RETRIES": 5,             # retries per price fetch before skipping
    "RETRY_BACKOFF_SEC": 2,       # base backoff; doubles each retry (2,4,8,...)
    "REQUEST_TIMEOUT_SEC": 10,
}

# ============================================================================
# Price feed — CoinGecko primary, Coinbase public endpoint as fallback.
# Both are free and require no API key.
# ============================================================================


def _fetch_coingecko() -> float:
    """Fetch current BTC price in USD from CoinGecko."""
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": CONFIG["COINGECKO_ID"], "vs_currencies": CONFIG["VS_CURRENCY"]}
    resp = requests.get(url, params=params, timeout=CONFIG["REQUEST_TIMEOUT_SEC"])
    resp.raise_for_status()
    return float(resp.json()[CONFIG["COINGECKO_ID"]][CONFIG["VS_CURRENCY"]])


def _fetch_coinbase() -> float:
    """Fetch current BTC spot price in USD from Coinbase's public endpoint."""
    url = f"https://api.coinbase.com/v2/prices/{CONFIG['TICKER']}-USD/spot"
    resp = requests.get(url, timeout=CONFIG["REQUEST_TIMEOUT_SEC"])
    resp.raise_for_status()
    return float(resp.json()["data"]["amount"])


def fetch_price() -> float | None:
    """
    Fetch the live price, retrying with exponential backoff.

    Tries CoinGecko first, then Coinbase as a fallback on each attempt.
    Returns None if every attempt fails — the caller skips the cycle
    rather than crashing.
    """
    for attempt in range(CONFIG["MAX_RETRIES"]):
        for source in (_fetch_coingecko, _fetch_coinbase):
            try:
                return source()
            except (requests.RequestException, KeyError, ValueError) as exc:
                print(f"  [warn] {source.__name__} failed: {exc}")
        wait = CONFIG["RETRY_BACKOFF_SEC"] * (2 ** attempt)
        print(f"  [warn] all sources failed (attempt {attempt + 1}/"
              f"{CONFIG['MAX_RETRIES']}), retrying in {wait}s...")
        time.sleep(wait)
    return None


# ============================================================================
# State persistence — survives restarts
# ============================================================================


def default_state() -> dict:
    return {
        "balance_usd": CONFIG["STARTING_BALANCE"],   # cash on hand
        "btc_amount": 0.0,                           # BTC currently held
        "entry_price": None,                         # price we bought at (None = no position)
        "starting_equity": CONFIG["STARTING_BALANCE"],  # for total P/L %
        "price_history": [],                         # recent prices for the MAs
    }


def load_state() -> dict:
    """Load saved state, falling back to a fresh start if missing/corrupt."""
    path = CONFIG["STATE_FILE"]
    if os.path.exists(path):
        try:
            with open(path) as f:
                state = json.load(f)
            # Merge over defaults so new fields added later don't break old files.
            merged = default_state()
            merged.update(state)
            print(f"[init] resumed state from {path} "
                  f"(balance ${merged['balance_usd']:,.2f}, "
                  f"position {'OPEN' if merged['entry_price'] else 'none'})")
            return merged
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] could not read {path} ({exc}); starting fresh")
    print(f"[init] starting fresh with ${CONFIG['STARTING_BALANCE']:,.2f}")
    return default_state()


def save_state(state: dict) -> None:
    """Atomically write state to disk (write temp file, then rename)."""
    path = CONFIG["STATE_FILE"]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ============================================================================
# Trade logging
# ============================================================================

CSV_FIELDS = ["timestamp", "side", "price", "amount_btc", "fee_usd",
              "balance_after", "reason"]


def log_trade(side: str, price: float, amount: float, fee: float,
              balance_after: float, reason: str) -> None:
    """Append one trade to trades.csv, writing a header on first use."""
    path = CONFIG["TRADES_FILE"]
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(CSV_FIELDS)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            side,
            f"{price:.2f}",
            f"{amount:.8f}",
            f"{fee:.4f}",
            f"{balance_after:.2f}",
            reason,
        ])


# ============================================================================
# Paper trading engine
# ============================================================================


def execute_buy(state: dict, price: float, reason: str) -> None:
    """
    Simulate a market buy using TRADE_SIZE_PCT of the cash balance.
    The fee comes out of the amount spent, so we never overdraw.
    """
    spend = state["balance_usd"] * CONFIG["TRADE_SIZE_PCT"]
    if spend < 1.0:  # nothing meaningful to buy with
        print("  [skip] balance too small to open a position")
        return
    fee = spend * CONFIG["FEE_PCT"]
    btc = (spend - fee) / price

    state["balance_usd"] -= spend
    state["btc_amount"] = btc
    state["entry_price"] = price

    log_trade("buy", price, btc, fee, state["balance_usd"], reason)
    print(f"  [TRADE] BUY {btc:.8f} BTC @ ${price:,.2f} "
          f"(fee ${fee:.2f}) — {reason}")


def execute_sell(state: dict, price: float, reason: str) -> None:
    """Simulate selling the entire position at the current price."""
    btc = state["btc_amount"]
    proceeds = btc * price
    fee = proceeds * CONFIG["FEE_PCT"]

    state["balance_usd"] += proceeds - fee
    state["btc_amount"] = 0.0
    entry = state["entry_price"]
    state["entry_price"] = None

    pnl_pct = (price - entry) / entry * 100 if entry else 0.0
    log_trade("sell", price, btc, fee, state["balance_usd"], reason)
    print(f"  [TRADE] SELL {btc:.8f} BTC @ ${price:,.2f} "
          f"(fee ${fee:.2f}, trade P/L {pnl_pct:+.2f}%) — {reason}")


# ============================================================================
# Strategy — moving-average crossover
# ============================================================================


def moving_average(prices: list, length: int) -> float | None:
    """Simple moving average of the last `length` prices (None if not enough)."""
    if len(prices) < length:
        return None
    return sum(prices[-length:]) / length


def compute_signal(prices: list) -> str:
    """
    Detect a crossover between the short and long MA.

    Compares the MA relationship on the previous cycle vs. now:
      - was short <= long, now short > long  -> "buy"  (bullish crossover)
      - was short >= long, now short < long  -> "sell" (bearish crossover)
      - otherwise                            -> "hold"
    Returns "warming_up" until we have enough history for both MAs
    plus one prior period to compare against.
    """
    long_n = CONFIG["LONG_MA"]
    short_n = CONFIG["SHORT_MA"]
    if len(prices) < long_n + 1:
        return "warming_up"

    prev = prices[:-1]  # history as of the previous cycle
    short_now = moving_average(prices, short_n)
    long_now = moving_average(prices, long_n)
    short_prev = moving_average(prev, short_n)
    long_prev = moving_average(prev, long_n)

    if short_prev <= long_prev and short_now > long_now:
        return "buy"
    if short_prev >= long_prev and short_now < long_now:
        return "sell"
    return "hold"


# ============================================================================
# Main loop
# ============================================================================


def run_cycle(state: dict, price: float) -> None:
    """Process one price tick: update history, check stop-loss, act on signal."""
    prices = state["price_history"]
    prices.append(price)
    # Keep only what the long MA needs (+1 for crossover comparison, with margin).
    max_len = CONFIG["LONG_MA"] + 10
    if len(prices) > max_len:
        del prices[:-max_len]

    in_position = state["entry_price"] is not None

    # --- 1. Stop-loss has top priority: check it before the strategy signal.
    if in_position:
        stop_price = state["entry_price"] * (1 - CONFIG["STOP_LOSS_PCT"])
        if price <= stop_price:
            execute_sell(state, price, "stop-loss")
            in_position = False

    # --- 2. Strategy signal.
    signal = compute_signal(prices)
    if signal == "buy" and not in_position:
        execute_buy(state, price, "MA crossover (bullish)")
        in_position = True
    elif signal == "sell" and in_position:
        execute_sell(state, price, "MA crossover (bearish)")
        in_position = False

    # --- 3. Status line.
    equity = state["balance_usd"] + state["btc_amount"] * price
    total_pnl_pct = (equity - state["starting_equity"]) / state["starting_equity"] * 100
    if in_position:
        pos = (f"LONG {state['btc_amount']:.8f} BTC "
               f"(entry ${state['entry_price']:,.2f})")
    else:
        pos = "no position"
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {CONFIG['TICKER']} ${price:,.2f} | signal: {signal} | "
          f"{pos} | cash ${state['balance_usd']:,.2f} | "
          f"equity ${equity:,.2f} | total P/L {total_pnl_pct:+.2f}%")


def main() -> None:
    print("=" * 70)
    print(f"  {CONFIG['TICKER']}/USD paper trading bot — "
          f"MA({CONFIG['SHORT_MA']}/{CONFIG['LONG_MA']}) crossover")
    print(f"  PAPER TRADING ONLY — no real money is ever at risk.")
    print("=" * 70)

    state = load_state()

    try:
        while True:
            price = fetch_price()
            if price is None:
                print("  [warn] could not fetch price this cycle; skipping")
            else:
                run_cycle(state, price)
                save_state(state)
            time.sleep(CONFIG["POLL_INTERVAL_SEC"])
    except KeyboardInterrupt:
        save_state(state)
        print("\n[exit] state saved — restart anytime to resume.")
        sys.exit(0)


if __name__ == "__main__":
    main()
