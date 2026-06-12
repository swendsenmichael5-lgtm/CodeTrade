#!/usr/bin/env python3
"""
Offline verification for bot.py — no network needed.

1. Builds synthetic 7-day hourly histories for the 10-coin universe where
   three pairs (BTC/ETH, SOL/AVAX, XRP/ADA) are cointegrated BY CONSTRUCTION
   and the rest are independent random walks. Pair selection should find
   the planted pairs.
2. Feeds ticks through run_tick() with prices engineered to hit exact
   z-scores, exercising: spread entry (both legs), spread-reversion exit,
   per-sleeve stop-loss, and the 15% total-portfolio kill switch.

Run: python3 test_simulation.py
"""

import math
import os

import numpy as np

import bot

rng = np.random.default_rng(42)

# --- isolate test artifacts from a real run --------------------------------
bot.CONFIG["STATE_FILE"] = "test_state.json"
bot.CONFIG["TRADES_FILE"] = "test_trades.csv"
bot.CONFIG["PAIRS_FILE"] = "test_pairs.csv"
bot.CONFIG["VOL_FILTER_WINDOW"] = 9999  # disable vol filter (tested separately)
for f in ("test_state.json", "test_trades.csv", "test_pairs.csv"):
    if os.path.exists(f):
        os.remove(f)

# ============================================================================
# 1. Synthetic histories: planted cointegrated pairs
# ============================================================================

N = 168  # 7 days of hourly points
BASE = {"BTC": 70_000, "ETH": 3_500, "SOL": 150, "XRP": 0.6, "ADA": 0.45,
        "DOGE": 0.15, "AVAX": 30, "LINK": 15, "DOT": 7, "LTC": 80}


def random_walk(start: float) -> np.ndarray:
    steps = rng.normal(0, 0.004, N)
    return start * np.exp(np.cumsum(steps))


def cointegrated_partner(driver: np.ndarray, start: float) -> np.ndarray:
    """log(partner) = log(driver) + const + small AR(1) noise => cointegrated."""
    noise = np.zeros(N)
    for i in range(1, N):
        noise[i] = 0.7 * noise[i - 1] + rng.normal(0, 0.002)
    return start / driver[0] * driver * np.exp(noise)


histories = {}
for sym in ("BTC", "SOL", "XRP", "DOGE", "LINK", "DOT", "LTC"):
    histories[sym] = list(random_walk(BASE[sym]))
histories["ETH"] = list(cointegrated_partner(np.array(histories["BTC"]), BASE["ETH"]))
histories["AVAX"] = list(cointegrated_partner(np.array(histories["SOL"]), BASE["AVAX"]))
histories["ADA"] = list(cointegrated_partner(np.array(histories["XRP"]), BASE["ADA"]))

# ============================================================================
# 2. Pair selection
# ============================================================================

state = bot.default_state()
bot.run_pair_selection(state, histories)
selected = list(state["sleeves"])
print(f"\nselected pairs: {selected}")
planted = {"BTC/ETH", "SOL/AVAX", "XRP/ADA"}
found = planted & set(selected)
print(f"planted pairs found: {sorted(found)} ({len(found)}/3)\n")
assert len(found) >= 2, "pair selection failed to find the planted pairs"

# ============================================================================
# 3. Tick simulation — drive z-scores to exact values
# ============================================================================

last_prices = {sym: histories[sym][-1] for sym in histories}


def price_for_z(sleeve: dict, prices: dict, z: float) -> dict:
    """Solve for the price of leg B that puts this sleeve's spread at z."""
    target_spread = sleeve["spread_mean"] + z * sleeve["spread_std"]
    log_b = (math.log(prices[sleeve["a"]]) - target_spread) / sleeve["beta"]
    out = dict(prices)
    out[sleeve["b"]] = math.exp(log_b)
    return out


pair = selected[0]
sleeve = state["sleeves"][pair]
print(f"--- driving sleeve {pair} (A={sleeve['a']}, B={sleeve['b']}) ---\n")

print(">>> tick 1: z = +2.5 -> expect BUY of cheap leg B")
prices = price_for_z(sleeve, last_prices, +2.5)
bot.run_tick(state, prices)
assert sleeve["holding"] and sleeve["holding"]["symbol"] == sleeve["b"]

print("\n>>> tick 2: z = +0.3 -> expect SELL (spread reverted)")
prices = price_for_z(sleeve, prices, +0.3)
bot.run_tick(state, prices)
assert sleeve["holding"] is None

print("\n>>> tick 3: z = -2.5 -> expect BUY of cheap leg A")
# Move leg A down to push the spread negative (B fixed).
target = sleeve["spread_mean"] - 2.5 * sleeve["spread_std"]
prices[sleeve["a"]] = math.exp(target + sleeve["beta"] * math.log(prices[sleeve["b"]]))
bot.run_tick(state, prices)
assert sleeve["holding"] and sleeve["holding"]["symbol"] == sleeve["a"]

print("\n>>> tick 4: held coin drops 5% -> expect per-sleeve STOP-LOSS")
prices[sleeve["a"]] *= 0.95
bot.run_tick(state, prices)
assert sleeve["holding"] is None

print("\n>>> tick 5: re-enter (z = +2.5 again)")
prices = price_for_z(sleeve, prices, +2.5)
bot.run_tick(state, prices)
assert sleeve["holding"]

print("\n>>> tick 6: held coin crashes 90% -> expect KILL SWITCH (>15% drawdown)")
prices[sleeve["holding"]["symbol"]] *= 0.10
bot.run_tick(state, prices)
assert state["halted"]

print("\n>>> tick 7: while halted -> expect NO trading")
bot.run_tick(state, prices)
assert all(s["holding"] is None for s in state["sleeves"].values())

# ============================================================================
# 4. Results
# ============================================================================

print("\n--- test_pairs.csv (top 5 rows) ---")
with open("test_pairs.csv") as f:
    for line in f.read().splitlines()[:6]:
        print(line)

print("\n--- test_trades.csv ---")
with open("test_trades.csv") as f:
    print(f.read())

print("ALL CHECKS PASSED")
