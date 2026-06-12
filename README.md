# CodeTrade — Multi-Pair Crypto Pairs Trading Bot

A Python bot that **simulates** statistical-arbitrage pairs trading across
the top-10 cryptos with fake money using real live market data.

> **Paper trading only. Spot-only, long-only.** The bot only *reads* public
> price data. It never places real orders, never shorts, never uses leverage.

## Strategy

1. **Universe:** BTC, ETH, SOL, XRP, ADA, DOGE, AVAX, LINK, DOT, LTC
   (configurable; stablecoins excluded).
2. **Daily pair selection:** every possible pair (45 combinations) is tested
   over a 7-day hourly lookback with an Engle-Granger **cointegration test**
   (statsmodels) plus return correlation. Pairs are ranked by cointegration
   p-value; the **top 3** that also pass the correlation/p-value thresholds
   become "tradeable" for the day. All rankings are logged to `pairs.csv`.
3. **Signal:** for each tradeable pair, the bot watches the spread
   `log(A) − β·log(B)` and its z-score vs. the lookback mean/std.
   **Enter** when `|z| > 2.0`, **exit** when `|z| < 0.5`.
4. **Long-only twist:** instead of the classic long/short pair trade, the
   bot only buys the relatively *cheap* leg — `z > +2` means A is rich vs B,
   so it buys B; `z < −2` means A is cheap, so it buys A — and sells when
   the spread reverts.
5. **Sleeves:** capital is split equally across the tradeable pairs (max 3
   concurrent positions). Each sleeve trades independently with its own cash
   and state.
6. **Risk (per sleeve):** 95% max of sleeve cash per entry, 0.1% fee per
   trade, 4% hard stop-loss below entry, z-blowout stop at `|z| > 3.5`, and
   a volatility filter that blocks new entries when the coin's recent
   5-minute volatility is too high.
7. **Kill switch (total portfolio):** if equity draws down **15%** from its
   high-water mark, everything is liquidated and trading halts until you
   reset (`"halted": false` in `state.json`, or delete the file).

Prices are fetched every 5 minutes in **one batched CoinGecko request**
(Coinbase public spot as fallback), with retry + exponential backoff. The
daily history fetch is throttled to respect free-tier rate limits.

## Install

Requires Python 3.10+.

```bash
pip install -r requirements.txt
```

## Run (easy way — one command)

```bash
python3 start.py
```

This starts the trading bot **and** the pixel dashboard together and opens
http://localhost:8000 in your browser. Press `Ctrl+C` once to stop both
(state is saved; restarting resumes your balance and positions).

## Run (manual way — separate pieces)

```bash
python3 bot.py
```

Stop with `Ctrl+C` (state is saved; restart resumes balance and positions).

On startup each day the bot fetches 7 days of hourly history for all 10
coins (takes ~30s, throttled) and prints the day's pair rankings, then
ticks every 5 minutes:

```
[select] 2026-06-12: tested 45 pairs, tradeable today: ['BTC/ETH', 'SOL/AVAX', 'XRP/ADA']
[2026-06-12 14:05:00] equity $10,000.00 | total P/L +0.00% | drawdown 0.0% | free cash $0.00
    BTC/ETH    z=-1.18 | flat | sleeve $3,333.33
    SOL/AVAX   z=-0.43 | flat | sleeve $3,333.33
    XRP/ADA    z=+2.50 | LONG 7112.09 ADA (entry $0.4448, z@entry +2.50) | sleeve $3,330.17
```

## Pixel dashboard (watch it trade)

In a **second terminal**, in the same folder where the bot is running:

```bash
python3 dashboard.py
```

Then open **http://localhost:8000** in your browser. You get a retro 8-bit
CRT-style dashboard (green phosphor, scanlines, chunky pixel chart) that
auto-refreshes every 5 seconds:

- big stat readouts: equity, total P/L %, drawdown vs. the 15% kill level,
  free cash, and the date pairs were last picked
- a pixelated equity chart for the last 24h, with dashed reference lines
  for your starting balance (amber) and the kill-switch level (red)
- one row per sleeve: pair, live z-score, position, sleeve value
- the recent trade log
- a big blinking red banner if the kill switch ever fires

It's read-only — it just renders the same `state.json`/`trades.csv` the bot
writes, so it can't affect trading, and you can start/stop it anytime.
Uses only the Python standard library (no extra installs). It binds to
localhost only, so nothing is exposed to the network.

## Backtest (test ideas in seconds, not weeks)

```bash
python3 backtest.py        # replay the last 60 days
python3 backtest.py 30     # or any 8-90 day window
```

Downloads hourly history once (cached in `backtest_data.json`; delete it to
refresh), then replays it through the live bot's exact selection/signal/risk
code, walk-forward (each simulated day re-selects pairs using only data
available up to that moment). Prints a scorecard: P/L, after-tax P/L, max
drawdown, trade count, fees paid, exit-reason breakdown, and the
buy-and-hold-BTC benchmark. To experiment, change `CONFIG` in `bot.py` and
re-run — results are comparable because the data is cached.

## Verify offline (no network needed)

```bash
python3 test_simulation.py
```

Plants cointegrated pairs in synthetic data, checks the selector finds
them, then drives engineered prices through every code path: entries on
both legs, spread-reversion exit, stop-loss, kill switch, and halt.

## Configuration

Everything lives in the `CONFIG` dict at the top of `bot.py`:

| Key | Default | Meaning |
|---|---|---|
| `UNIVERSE` | 10 coins | symbol → CoinGecko id map |
| `POLL_INTERVAL_SEC` | 300 | seconds between price ticks |
| `LOOKBACK_DAYS` | 7 | hourly history window for pair tests |
| `MAX_TRADEABLE_PAIRS` | 3 | sleeves / concurrent pair positions |
| `MIN_CORRELATION` / `MAX_COINT_PVALUE` | 0.6 / 0.10 | qualification thresholds |
| `ENTRY_Z` / `EXIT_Z` / `Z_STOP` | 2.0 / 0.5 / 3.5 | spread z-score levels |
| `TRADE_SIZE_PCT` | 0.95 | fraction of sleeve cash per entry |
| `STOP_LOSS_PCT` | 0.04 | per-sleeve hard stop below entry |
| `VOL_FILTER_WINDOW` / `MAX_TICK_VOL_PCT` | 24 / 1.0 | entry volatility filter |
| `MAX_DRAWDOWN_PCT` | 0.15 | total-portfolio kill switch |

## Files

| File | Purpose |
|---|---|
| `bot.py` | The bot — feed, selection, strategy, sleeves, risk, main loop |
| `dashboard.py` | Retro pixel web dashboard (run alongside the bot) |
| `backtest.py` | Replay historical data through the bot's logic |
| `test_simulation.py` | Offline synthetic-data verification |
| `state.json` | Saved sleeves/positions/equity high-water mark (auto-created) |
| `trades.csv` | Append-only trade log (auto-created) |
| `pairs.csv` | Daily pair rankings log (auto-created) |

## Notes & caveats

- Long-only pairs trading is **not** market-neutral like the classic
  long/short version — when the whole market dumps, the cheap leg dumps
  too. That's what the stop-loss and kill switch are for.
- Cointegration measured over 7 days can break down at any time
  (regime change); the daily re-selection and z-blowout stop are the
  defenses, not guarantees.
- Simulated fills assume the quoted spot price — no slippage or spread —
  so live results would be slightly worse than paper results.
- Educational tool, not financial advice.
