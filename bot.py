#!/usr/bin/env python3
"""
Multi-Pair Crypto Pairs Trading Bot (SPOT-ONLY, LONG-ONLY, PAPER MONEY)
=======================================================================

Strategy overview
-----------------
1. Universe: top-10 cryptos by market cap (configurable, stablecoins excluded).
2. Once per day, every possible pair in the universe is tested for
   cointegration (Engle-Granger test via statsmodels) plus return
   correlation over a lookback window of hourly data. The top 3 most
   cointegrated pairs become "tradeable" for that day (rankings -> pairs.csv).
3. For each tradeable pair, the bot watches the spread
       spread = log(price_A) - beta * log(price_B)
   and its z-score against the lookback mean/std:
       ENTER when |z| > 2.0   (the spread is stretched)
       EXIT  when |z| < 0.5   (the spread has reverted)
   LONG-ONLY twist: instead of the classic long/short pair trade, we only
   BUY the relatively *cheap* leg (z > +2 means A is rich vs B -> buy B;
   z < -2 means A is cheap -> buy A) and sell it when the spread reverts.
4. Capital is split into equal "sleeves", one per tradeable pair (max 3
   concurrent pair positions). Each sleeve trades independently.
5. Risk rules apply PER SLEEVE (volatility filter on entries, hard
   stop-loss, z-score blowout stop). A 15% max-drawdown kill switch on
   the TOTAL portfolio liquidates everything and halts trading.

PAPER TRADING ONLY — the bot only reads public price data; it never
places real orders, never shorts, never uses leverage.

Usage:  python3 bot.py        (Ctrl+C to stop; state persists in state.json)
"""

import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from itertools import combinations

import numpy as np
import requests
from statsmodels.tsa.stattools import coint

# ============================================================================
# CONFIG — tweak everything here
# ============================================================================

CONFIG = {
    # --- Universe: display symbol -> CoinGecko id (stablecoins excluded) ---
    "UNIVERSE": {
        "BTC":  "bitcoin",
        "ETH":  "ethereum",
        "SOL":  "solana",
        "XRP":  "ripple",
        "ADA":  "cardano",
        "DOGE": "dogecoin",
        "AVAX": "avalanche-2",
        "LINK": "chainlink",
        "DOT":  "polkadot",
        "LTC":  "litecoin",
    },
    "VS_CURRENCY": "usd",

    # --- Polling ---
    "POLL_INTERVAL_SEC": 300,        # one tick = 5 minutes (single batched request)

    # --- Daily pair selection ---
    "LOOKBACK_DAYS": 7,              # hourly history window for the tests
    "MAX_TRADEABLE_PAIRS": 3,        # keep only the top N cointegrated pairs
    "MIN_CORRELATION": 0.6,          # pairs below this return-correlation are skipped
    "MAX_COINT_PVALUE": 0.10,        # pairs above this p-value never qualify
    "HISTORY_REQUEST_GAP_SEC": 3.0,  # pause between the 10 daily history calls
                                     # (respects CoinGecko's free rate limit)

    # --- Spread z-score strategy ---
    "ENTRY_Z": 2.0,                  # open when |z| exceeds this
    "EXIT_Z": 0.5,                   # close when |z| falls back inside this

    # --- Money & risk (per sleeve unless noted) ---
    "STARTING_BALANCE": 10_000.0,    # virtual USD
    "TRADE_SIZE_PCT": 0.95,          # fraction of sleeve cash used per entry
    "FEE_PCT": 0.001,                # 0.1% fee per simulated trade
    "STOP_LOSS_PCT": 0.04,           # sell if the held coin drops 4% below entry
    "Z_STOP": 3.5,                   # abandon trade if the spread blows out past this
    "VOL_FILTER_WINDOW": 24,         # ticks (~2h) of returns used by the vol filter
    "MAX_TICK_VOL_PCT": 1.0,         # skip entries if 5-min return std > 1.0%
    "MAX_DRAWDOWN_PCT": 0.15,        # TOTAL-portfolio kill switch: liquidate + halt

    # --- Files ---
    "STATE_FILE": "state.json",
    "TRADES_FILE": "trades.csv",
    "PAIRS_FILE": "pairs.csv",

    # --- Networking ---
    "MAX_RETRIES": 5,
    "RETRY_BACKOFF_SEC": 2,
    "REQUEST_TIMEOUT_SEC": 15,
}

# ============================================================================
# Price feed — CoinGecko (batched) with per-coin Coinbase fallback
# ============================================================================


def _http_get(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, params=params, timeout=CONFIG["REQUEST_TIMEOUT_SEC"])
    resp.raise_for_status()
    return resp.json()


def _fetch_batch_coingecko() -> dict:
    """All universe prices in ONE request: {symbol: usd_price}."""
    ids = ",".join(CONFIG["UNIVERSE"].values())
    data = _http_get(
        "https://api.coingecko.com/api/v3/simple/price",
        {"ids": ids, "vs_currencies": CONFIG["VS_CURRENCY"]},
    )
    return {sym: float(data[cid][CONFIG["VS_CURRENCY"]])
            for sym, cid in CONFIG["UNIVERSE"].items()}


def _fetch_batch_coinbase() -> dict:
    """Fallback: one Coinbase public spot request per coin."""
    prices = {}
    for sym in CONFIG["UNIVERSE"]:
        data = _http_get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot")
        prices[sym] = float(data["data"]["amount"])
        time.sleep(0.3)  # be polite, it's 10 requests
    return prices


def fetch_prices() -> dict | None:
    """
    Fetch all live prices with retry + exponential backoff.
    Returns {symbol: price} or None (caller skips the tick — never crashes).
    """
    for attempt in range(CONFIG["MAX_RETRIES"]):
        for source in (_fetch_batch_coingecko, _fetch_batch_coinbase):
            try:
                return source()
            except (requests.RequestException, KeyError, ValueError) as exc:
                print(f"  [warn] {source.__name__} failed: {exc}")
        wait = CONFIG["RETRY_BACKOFF_SEC"] * (2 ** attempt)
        print(f"  [warn] all price sources failed "
              f"(attempt {attempt + 1}/{CONFIG['MAX_RETRIES']}), retry in {wait}s")
        time.sleep(wait)
    return None


def fetch_history(coin_id: str) -> list | None:
    """
    Hourly close prices for the last LOOKBACK_DAYS from CoinGecko
    (used once per day by pair selection). Returns a list of floats.
    """
    for attempt in range(CONFIG["MAX_RETRIES"]):
        try:
            data = _http_get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                {"vs_currency": CONFIG["VS_CURRENCY"],
                 "days": CONFIG["LOOKBACK_DAYS"]},
            )
            return [float(p[1]) for p in data["prices"]]
        except (requests.RequestException, KeyError, ValueError) as exc:
            wait = CONFIG["RETRY_BACKOFF_SEC"] * (2 ** attempt)
            print(f"  [warn] history fetch for {coin_id} failed: {exc}; "
                  f"retry in {wait}s")
            time.sleep(wait)
    return None


# ============================================================================
# State persistence
# ============================================================================


def default_state() -> dict:
    return {
        "starting_equity": CONFIG["STARTING_BALANCE"],
        "peak_equity": CONFIG["STARTING_BALANCE"],  # high-water mark for kill switch
        "free_cash": CONFIG["STARTING_BALANCE"],    # cash not assigned to a sleeve
        "halted": False,                            # kill switch tripped
        "last_selection_date": None,                # "YYYY-MM-DD" of last pair selection
        # sleeves: {"BTC/ETH": {a, b, beta, spread_mean, spread_std, cash, holding}}
        # holding: {"symbol", "amount", "entry_price", "entry_z"} or None
        "sleeves": {},
        # short rolling price history per symbol, used by the volatility filter
        "price_history": {},
        # [iso_timestamp, equity] per tick — feeds the dashboard's chart
        "equity_history": [],
    }


def load_state() -> dict:
    path = CONFIG["STATE_FILE"]
    if os.path.exists(path):
        try:
            with open(path) as f:
                saved = json.load(f)
            state = default_state()
            state.update(saved)
            print(f"[init] resumed from {path}: "
                  f"{len(state['sleeves'])} sleeve(s), "
                  f"halted={state['halted']}")
            return state
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] could not read {path} ({exc}); starting fresh")
    print(f"[init] fresh start with ${CONFIG['STARTING_BALANCE']:,.2f}")
    return default_state()


def save_state(state: dict) -> None:
    tmp = CONFIG["STATE_FILE"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, CONFIG["STATE_FILE"])


# ============================================================================
# Logging — trades.csv and daily pairs.csv
# ============================================================================


def _append_csv(path: str, header: list, row: list) -> None:
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(header)
        writer.writerow(row)


def log_trade(pair: str, side: str, symbol: str, price: float, amount: float,
              fee: float, sleeve_cash_after: float, reason: str) -> None:
    _append_csv(
        CONFIG["TRADES_FILE"],
        ["timestamp", "pair", "side", "symbol", "price", "amount",
         "fee_usd", "sleeve_cash_after", "reason"],
        [datetime.now(timezone.utc).isoformat(), pair, side, symbol,
         f"{price:.4f}", f"{amount:.8f}", f"{fee:.4f}",
         f"{sleeve_cash_after:.2f}", reason],
    )


def log_pair_ranking(date: str, rank: int, pair: str, pvalue: float,
                     corr: float, selected: bool) -> None:
    _append_csv(
        CONFIG["PAIRS_FILE"],
        ["date", "rank", "pair", "coint_pvalue", "correlation", "selected"],
        [date, rank, pair, f"{pvalue:.4f}", f"{corr:.4f}", selected],
    )


# ============================================================================
# Daily pair selection — cointegration + correlation over the lookback window
# ============================================================================


def analyze_pair(log_a: np.ndarray, log_b: np.ndarray) -> tuple:
    """
    Return (coint_pvalue, return_correlation, beta, spread_mean, spread_std)
    for two aligned log-price series.

    beta is the hedge ratio from an OLS fit of log_a on log_b; the spread
    log_a - beta*log_b should be stationary (mean-reverting) if the pair
    is truly cointegrated.
    """
    _, pvalue, _ = coint(log_a, log_b)
    corr = float(np.corrcoef(np.diff(log_a), np.diff(log_b))[0, 1])
    beta = float(np.polyfit(log_b, log_a, 1)[0])
    spread = log_a - beta * log_b
    return pvalue, corr, beta, float(spread.mean()), float(spread.std())


def run_pair_selection(state: dict, histories: dict) -> None:
    """
    Test all pairs in the universe, log rankings to pairs.csv, keep the
    top MAX_TRADEABLE_PAIRS as today's tradeable set, and rebuild sleeves.

    `histories` is {symbol: [hourly prices]} — fetched by the caller so
    this function stays easy to test offline.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    symbols = [s for s in CONFIG["UNIVERSE"] if s in histories]

    # Align all series to the same length (truncate to the shortest).
    min_len = min(len(histories[s]) for s in symbols)
    logs = {s: np.log(np.asarray(histories[s][-min_len:], dtype=float))
            for s in symbols}

    # Score every pair.
    results = []  # (pvalue, corr, pair_name, a, b, beta, mean, std)
    for a, b in combinations(symbols, 2):
        try:
            pvalue, corr, beta, mean, std = analyze_pair(logs[a], logs[b])
        except (ValueError, np.linalg.LinAlgError) as exc:
            print(f"  [warn] pair test {a}/{b} failed: {exc}")
            continue
        if std <= 0 or not math.isfinite(pvalue):
            continue
        results.append((pvalue, corr, f"{a}/{b}", a, b, beta, mean, std))

    # Rank by cointegration p-value (lower = more cointegrated).
    results.sort(key=lambda r: r[0])
    selected = []
    for pvalue, corr, name, *_ in results:
        qualifies = (len(selected) < CONFIG["MAX_TRADEABLE_PAIRS"]
                     and pvalue <= CONFIG["MAX_COINT_PVALUE"]
                     and corr >= CONFIG["MIN_CORRELATION"])
        if qualifies:
            selected.append(name)
    for rank, (pvalue, corr, name, *_) in enumerate(results, start=1):
        log_pair_ranking(today, rank, name, pvalue, corr, name in selected)

    print(f"[select] {today}: tested {len(results)} pairs, "
          f"tradeable today: {selected or 'NONE'}")

    # --- Rebuild sleeves ----------------------------------------------------
    # Sleeves whose pair stays selected are kept (position and stats intact —
    # stats only refresh when the sleeve is flat, so an open trade's z-score
    # keeps meaning the same thing until it closes).
    by_name = {name: (a, b, beta, mean, std)
               for _, _, name, a, b, beta, mean, std in results}
    old = state["sleeves"]
    new_sleeves = {}
    for name in selected:
        if name in old:
            sleeve = old.pop(name)
            if sleeve["holding"] is None:
                a, b, beta, mean, std = by_name[name]
                sleeve.update(beta=beta, spread_mean=mean, spread_std=std)
            new_sleeves[name] = sleeve

    # Dropped sleeves: liquidate any open position at the last known price
    # and return the cash to the free pool.
    for name, sleeve in old.items():
        if sleeve["holding"]:
            sym = sleeve["holding"]["symbol"]
            hist = state["price_history"].get(sym, [])
            if hist:
                sell(state, name, sleeve, hist[-1], "pair dropped at daily selection")
            else:
                print(f"  [warn] no price for {sym}; cannot liquidate {name} yet")
                new_sleeves[name] = sleeve  # keep it until we can sell
                continue
        state["free_cash"] += sleeve["cash"]

    # Brand-new pairs share the free cash pool equally.
    fresh = [n for n in selected if n not in new_sleeves]
    if fresh and state["free_cash"] > 0:
        per_sleeve = state["free_cash"] / len(fresh)
        for name in fresh:
            a, b, beta, mean, std = by_name[name]
            new_sleeves[name] = {
                "a": a, "b": b, "beta": beta,
                "spread_mean": mean, "spread_std": std,
                "cash": per_sleeve, "holding": None,
            }
        state["free_cash"] = 0.0

    state["sleeves"] = new_sleeves
    state["last_selection_date"] = today


# ============================================================================
# Paper trading engine (per sleeve)
# ============================================================================


def buy(state: dict, pair: str, sleeve: dict, symbol: str, price: float,
        z: float, reason: str) -> None:
    """Buy `symbol` with TRADE_SIZE_PCT of this sleeve's cash."""
    spend = sleeve["cash"] * CONFIG["TRADE_SIZE_PCT"]
    if spend < 1.0:
        print(f"  [skip] {pair}: sleeve cash too small to trade")
        return
    fee = spend * CONFIG["FEE_PCT"]
    amount = (spend - fee) / price
    sleeve["cash"] -= spend
    sleeve["holding"] = {"symbol": symbol, "amount": amount,
                         "entry_price": price, "entry_z": z}
    log_trade(pair, "buy", symbol, price, amount, fee, sleeve["cash"], reason)
    print(f"  [TRADE] {pair}: BUY {amount:.6f} {symbol} @ ${price:,.4f} "
          f"(z={z:+.2f}, fee ${fee:.2f}) — {reason}")


def sell(state: dict, pair: str, sleeve: dict, price: float, reason: str) -> None:
    """Liquidate this sleeve's entire holding at `price`."""
    h = sleeve["holding"]
    proceeds = h["amount"] * price
    fee = proceeds * CONFIG["FEE_PCT"]
    sleeve["cash"] += proceeds - fee
    pnl = (price - h["entry_price"]) / h["entry_price"] * 100
    log_trade(pair, "sell", h["symbol"], price, h["amount"], fee,
              sleeve["cash"], reason)
    print(f"  [TRADE] {pair}: SELL {h['amount']:.6f} {h['symbol']} "
          f"@ ${price:,.4f} (trade P/L {pnl:+.2f}%, fee ${fee:.2f}) — {reason}")
    sleeve["holding"] = None


def portfolio_equity(state: dict, prices: dict) -> float:
    """free cash + every sleeve's cash + every holding marked to market."""
    equity = state["free_cash"]
    for sleeve in state["sleeves"].values():
        equity += sleeve["cash"]
        if sleeve["holding"]:
            h = sleeve["holding"]
            equity += h["amount"] * prices.get(h["symbol"], h["entry_price"])
    return equity


def liquidate_all(state: dict, prices: dict, reason: str) -> None:
    for name, sleeve in state["sleeves"].items():
        if sleeve["holding"]:
            sym = sleeve["holding"]["symbol"]
            sell(state, name, sleeve, prices[sym], reason)


# ============================================================================
# Risk filters
# ============================================================================


def tick_volatility_pct(state: dict, symbol: str) -> float | None:
    """
    Std-dev (in %) of the symbol's recent per-tick returns.
    Returns None until enough live history accumulates (filter passes then).
    """
    hist = state["price_history"].get(symbol, [])
    window = CONFIG["VOL_FILTER_WINDOW"]
    if len(hist) < window + 1:
        return None
    tail = np.asarray(hist[-(window + 1):], dtype=float)
    returns = np.diff(tail) / tail[:-1]
    return float(returns.std() * 100)


def entry_allowed(state: dict, pair: str, symbol: str) -> bool:
    """Per-sleeve volatility filter: block entries when the coin is too wild."""
    vol = tick_volatility_pct(state, symbol)
    if vol is not None and vol > CONFIG["MAX_TICK_VOL_PCT"]:
        print(f"  [filter] {pair}: entry blocked, {symbol} tick-vol "
              f"{vol:.2f}% > {CONFIG['MAX_TICK_VOL_PCT']}%")
        return False
    return True


# ============================================================================
# Per-tick logic
# ============================================================================


def sleeve_zscore(sleeve: dict, prices: dict) -> float:
    spread = (math.log(prices[sleeve["a"]])
              - sleeve["beta"] * math.log(prices[sleeve["b"]]))
    return (spread - sleeve["spread_mean"]) / sleeve["spread_std"]


def run_sleeve(state: dict, pair: str, sleeve: dict, prices: dict) -> str:
    """Trade one sleeve for one tick. Returns a one-line status string."""
    z = sleeve_zscore(sleeve, prices)
    h = sleeve["holding"]

    if h:
        price = prices[h["symbol"]]
        stop = h["entry_price"] * (1 - CONFIG["STOP_LOSS_PCT"])
        if price <= stop:                              # hard stop-loss first
            sell(state, pair, sleeve, price, "stop-loss")
        elif abs(z) >= CONFIG["Z_STOP"]:               # spread blew out
            sell(state, pair, sleeve, price, "z-score blowout stop")
        elif abs(z) <= CONFIG["EXIT_Z"]:               # spread reverted: take profit
            sell(state, pair, sleeve, price, "spread reverted (|z| < exit)")
    else:
        # Long-only entries: buy whichever leg the spread says is cheap.
        if z >= CONFIG["ENTRY_Z"]:
            cheap = sleeve["b"]      # A rich vs B -> B is the cheap leg
        elif z <= -CONFIG["ENTRY_Z"]:
            cheap = sleeve["a"]      # A cheap vs B
        else:
            cheap = None
        if cheap and entry_allowed(state, pair, cheap):
            buy(state, pair, sleeve, cheap, prices[cheap], z,
                f"spread entry (z={z:+.2f})")

    sleeve["last_z"] = z  # saved to state so the dashboard can display it

    h = sleeve["holding"]
    if h:
        pos = (f"LONG {h['amount']:.6f} {h['symbol']} "
               f"(entry ${h['entry_price']:,.4f}, z@entry {h['entry_z']:+.2f})")
    else:
        pos = "flat"
    value = sleeve["cash"] + (h["amount"] * prices[h["symbol"]] if h else 0)
    return f"{pair:10s} z={z:+5.2f} | {pos} | sleeve ${value:,.2f}"


def run_tick(state: dict, prices: dict) -> None:
    """One 5-minute cycle: history, kill switch, sleeves, status output."""
    # Rolling per-symbol history for the volatility filter.
    for sym, price in prices.items():
        hist = state["price_history"].setdefault(sym, [])
        hist.append(price)
        max_len = CONFIG["VOL_FILTER_WINDOW"] + 10
        if len(hist) > max_len:
            del hist[:-max_len]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # --- TOTAL-portfolio kill switch (checked before anything trades) ------
    equity = portfolio_equity(state, prices)
    state["peak_equity"] = max(state["peak_equity"], equity)
    drawdown = (state["peak_equity"] - equity) / state["peak_equity"]
    if not state["halted"] and drawdown >= CONFIG["MAX_DRAWDOWN_PCT"]:
        print(f"[{ts}] *** KILL SWITCH: drawdown {drawdown:.1%} >= "
              f"{CONFIG['MAX_DRAWDOWN_PCT']:.0%} — liquidating, halting ***")
        liquidate_all(state, prices, "max-drawdown kill switch")
        state["halted"] = True

    if state["halted"]:
        equity = portfolio_equity(state, prices)
        state["equity_history"].append(
            [datetime.now(timezone.utc).isoformat(), equity])
        print(f"[{ts}] HALTED by kill switch | equity ${equity:,.2f} | "
              f"reset by deleting {CONFIG['STATE_FILE']} "
              f"or setting \"halted\": false")
        return

    # --- Trade each sleeve independently ------------------------------------
    lines = [run_sleeve(state, pair, sleeve, prices)
             for pair, sleeve in state["sleeves"].items()]

    equity = portfolio_equity(state, prices)
    state["equity_history"].append(
        [datetime.now(timezone.utc).isoformat(), equity])
    if len(state["equity_history"]) > 2880:  # ~10 days of 5-min ticks
        del state["equity_history"][:-2880]

    pnl = (equity - state["starting_equity"]) / state["starting_equity"] * 100
    print(f"[{ts}] equity ${equity:,.2f} | total P/L {pnl:+.2f}% | "
          f"drawdown {drawdown:.1%} | free cash ${state['free_cash']:,.2f}")
    for line in lines:
        print(f"    {line}")
    if not lines:
        print("    (no tradeable pairs today)")


# ============================================================================
# Main loop
# ============================================================================


def selection_due(state: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state["last_selection_date"] != today


def fetch_all_histories() -> dict | None:
    """Fetch hourly history for every universe coin, throttled."""
    histories = {}
    for sym, cid in CONFIG["UNIVERSE"].items():
        series = fetch_history(cid)
        if series is None:
            print(f"  [warn] giving up on history for {sym}")
            return None  # try again next tick rather than select on partial data
        histories[sym] = series
        time.sleep(CONFIG["HISTORY_REQUEST_GAP_SEC"])
    return histories


def main() -> None:
    print("=" * 74)
    print("  Multi-pair crypto pairs trading bot — spot-only, long-only")
    print(f"  Universe: {', '.join(CONFIG['UNIVERSE'])}")
    print("  PAPER TRADING ONLY — no real money is ever at risk.")
    print("=" * 74)

    state = load_state()

    try:
        while True:
            if not state["halted"] and selection_due(state):
                print("[select] running daily pair selection "
                      f"({CONFIG['LOOKBACK_DAYS']}d hourly lookback)...")
                histories = fetch_all_histories()
                if histories:
                    run_pair_selection(state, histories)
                    save_state(state)
                else:
                    print("[select] history fetch failed; will retry next tick")

            prices = fetch_prices()
            if prices is None:
                print("  [warn] no prices this tick; skipping")
            else:
                run_tick(state, prices)
                save_state(state)
            time.sleep(CONFIG["POLL_INTERVAL_SEC"])
    except KeyboardInterrupt:
        save_state(state)
        print("\n[exit] state saved — restart anytime to resume.")
        sys.exit(0)


if __name__ == "__main__":
    main()
