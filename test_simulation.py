#!/usr/bin/env python3
"""
Offline verification for bot.py — feeds a synthetic price sequence through
run_cycle() to exercise every code path: warm-up, bullish-crossover buy,
stop-loss sell, re-buy, and bearish-crossover sell. No network needed.

Run: python3 test_simulation.py
"""

import os

import bot

# Use separate files so a real run's state/log is never touched.
bot.CONFIG["STATE_FILE"] = "test_state.json"
bot.CONFIG["TRADES_FILE"] = "test_trades.csv"
for f in (bot.CONFIG["STATE_FILE"], bot.CONFIG["TRADES_FILE"]):
    if os.path.exists(f):
        os.remove(f)

# Synthetic BTC price path:
#   1. 32 periods drifting slightly down  -> warm-up, short MA below long MA
#   2. sharp rally                        -> short crosses above long => BUY
#   3. sharp crash                        -> price hits 2% stop => STOP-LOSS
#   4. recovery rally                     -> bullish crossover again => RE-BUY
#   5. slow bleed                         -> short crosses below long => SELL
prices = []
p = 70_000.0
for _ in range(32):                 # 1. warm-up, gentle downtrend
    p -= 20
    prices.append(p)
for _ in range(8):                  # 2. rally => bullish crossover
    p += 300
    prices.append(p)
for _ in range(7):                  # 3. crash => stop-loss
    p -= 600
    prices.append(p)
for _ in range(12):                 # 4. recovery => re-buy
    p += 400
    prices.append(p)
for _ in range(20):                 # 5. slow bleed => bearish crossover sell
    p -= 150
    prices.append(p)

state = bot.default_state()
for price in prices:
    bot.run_cycle(state, price)
    bot.save_state(state)

print("\n--- final state ---")
equity = state["balance_usd"] + state["btc_amount"] * prices[-1]
print(f"cash ${state['balance_usd']:,.2f}, BTC {state['btc_amount']:.8f}, "
      f"equity ${equity:,.2f}")

print("\n--- trades.csv contents ---")
with open(bot.CONFIG["TRADES_FILE"]) as f:
    print(f.read())
