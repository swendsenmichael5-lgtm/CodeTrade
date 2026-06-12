# CodeTrade — BTC/USD Paper Trading Bot

A simple Python bot that **simulates** day trading Bitcoin with fake money
using **real live market data**, so you can safely test strategies before
ever risking real funds.

> **Paper trading only.** The bot only *reads* public price data. It never
> places real orders and has no exchange credentials.

## How it works

- Fetches the live BTC/USD price every 60 seconds from CoinGecko (with
  Coinbase's public spot-price endpoint as an automatic fallback). No API
  key needed.
- **Strategy:** moving-average crossover. It buys when the 10-period MA
  crosses above the 30-period MA, and sells when it crosses back below.
- **Paper engine:** starts with a virtual $10,000, simulates fills at the
  live price, and charges a 0.1% fee per trade to mimic real exchanges.
- **Risk management:** uses at most 95% of the balance per position, has a
  hard 2% stop-loss below entry, and never shorts or uses leverage.
- **Persistence:** balance, open position, and recent price history are
  saved to `state.json` after every cycle, so you can stop and restart
  the bot without losing anything. Every trade is appended to `trades.csv`.

## Install

Requires Python 3.10+.

```bash
pip install -r requirements.txt
```

## Run

```bash
python3 bot.py
```

Stop with `Ctrl+C` (state is saved). To start over from $10,000, delete
`state.json` (and `trades.csv` if you want a clean log).

Example output:

```
[14:02:11] BTC $67,412.18 | signal: hold | no position | cash $10,000.00 | equity $10,000.00 | total P/L +0.00%
[14:03:11] BTC $67,455.02 | signal: buy | LONG 0.14077 BTC (entry $67,455.02) | cash $500.00 | equity $9,990.50 | total P/L -0.09%
```

## Configuration

All strategy parameters live in the `CONFIG` dict at the top of `bot.py`:

| Key | Default | Meaning |
|---|---|---|
| `POLL_INTERVAL_SEC` | 60 | Seconds between price fetches (= one "period") |
| `SHORT_MA` / `LONG_MA` | 10 / 30 | Moving-average lengths in periods |
| `STARTING_BALANCE` | 10000 | Virtual USD starting balance |
| `TRADE_SIZE_PCT` | 0.95 | Fraction of cash used per buy |
| `STOP_LOSS_PCT` | 0.02 | Auto-sell if price drops this far below entry |
| `FEE_PCT` | 0.001 | Simulated fee per trade (0.1%) |

## Files

| File | Purpose |
|---|---|
| `bot.py` | The bot — price feed, strategy, paper engine, main loop |
| `state.json` | Saved balance/position/price history (auto-created) |
| `trades.csv` | Append-only trade log (auto-created) |

## Notes & caveats

- With a 60-second interval, the 30-period MA needs ~31 minutes of data
  before the first signal can fire (`signal: warming_up` until then).
- Simulated fills assume you trade exactly at the quoted spot price; real
  trading also has slippage and spread, so live results would be slightly
  worse than the paper results.
- This is an educational tool, not financial advice. MA crossovers on
  1-minute data trade often and tend to get chewed up by fees in sideways
  markets — that's exactly the kind of lesson paper trading is for.
