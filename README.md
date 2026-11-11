# QuantLab

A personal quant research platform for **US and ASX stocks and options**: live-ish market
data, news sentiment, a machine-learning forecast, strategy backtesting, option analytics
and a paper-trading account, all behind a web interface you can run locally or on your own domain.

Everything runs on free data (Yahoo Finance prices and option chains, Yahoo and Google News
headlines). No API keys are needed.

> QuantLab is a research tool. Its signals and forecasts are not financial advice, free data
> is delayed and occasionally wrong, and paper trades use simulated money.

## Quick start

```bash
pip install -e ".[dev]"
```

```bash
python -m quantlab serve
```

Open http://127.0.0.1:8000. From the command line you can also print signals directly:

```bash
python -m quantlab signal AAPL BHP.AX
```

Tickers follow Yahoo's convention: `AAPL` for US listings, `BHP.AX` (or `ASX:BHP`) for ASX.

## What is in the app

| Screen | What it does |
|---|---|
| **Dashboard** | Your watchlist with price, day change, composite signal, forecast probability and news sentiment. |
| **Research** | Candlestick chart with moving averages, risk statistics (volatility, Sharpe, drawdown, beta), the signal broken into its parts, an ATR-based trade plan, and recent headlines scored for sentiment. |
| **Options** | Option chain per expiry with implied volatility and Greeks, expected move, put/call ratio, max pain, skew, a volatility smile chart, and a Black-Scholes calculator. |
| **Backtest** | Run EMA crossover, RSI mean reversion, Donchian breakout or MACD strategies over up to 10 years and compare with buy and hold. |
| **Paper trading** | Simulated USD and AUD accounts, order ticket, positions with P&L and beta, trade history, and a track record of how past signals turned out. |

## How the signal works

Each ticker gets a score from −1 (bearish) to +1 (bullish), a weighted blend of four parts:

| Part | Weight | Source |
|---|---|---|
| Trend | 35% | Price against its 50- and 200-day averages, 20/50-day EMA alignment |
| Momentum | 20% | RSI, MACD histogram, volatility-adjusted 20-day return |
| ML forecast | 25% | Gradient-boosted model's probability of a higher close in 5 trading days |
| News | 20% | Recency-weighted sentiment of the last 7 days of headlines |

Scores of ±0.2 are Buy/Sell and ±0.5 are Strong Buy/Strong Sell.

Two design choices keep it honest:

- **The forecast is graded only on data it never trained on** (walk-forward validation with a
  gap between training and test periods). Its vote is scaled by that out-of-sample skill, so a
  model with no demonstrated edge on a stock contributes nothing. For most large stocks,
  5-day direction is close to a coin flip, and the app says so.
- **Backtests cannot see the future.** Positions are decided at a day's close and earn the
  next day's return, with trading costs charged on every change.

Every day's signal is logged, and the Paper trading screen reports what the price actually
did afterwards, so you can judge the signal on its own record before trusting it.

## Known limits

- **ASX option chains** are not available from the free source. The option calculator works for any ticker.
- **Option quotes go blank outside US market hours** (Yahoo returns zero bid/ask), so implied
  volatilities are only reliable during the session. Pricing is Black-Scholes, an approximation
  for American-style options.
- **News history cannot be backfilled.** Headlines are archived from the day you start running
  the app, so news sentiment feeds the live signal but not yet the ML model's training.
- **Headline sentiment is coarse**: a word-based score of the headline only.
- **Paper trading** covers long stock positions with market orders; options and short selling are not simulated.
- Prices are end-of-day or delayed; this is built for swing trading (days to weeks), not intraday.

## Putting it on your own domain

The repo includes a Docker setup with [Caddy](https://caddyserver.com), which obtains an HTTPS
certificate automatically. On a server (any small VPS) with Docker installed:

1. Point a DNS `A` record for your domain or subdomain at the server's IP address.
2. Create the settings file and fill in both values:

   ```bash
   cp .env.example .env
   ```

3. Start it:

   ```bash
   docker compose up -d --build
   ```

The app is then at `https://your-domain`, behind the password you set. Always set
`QUANTLAB_PASSWORD` when the app is reachable from the internet. The Docker files have not been
exercised on a live server yet, so treat the first deployment as a test.

## Settings

All optional, set as environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `QUANTLAB_PASSWORD` | none | Password for the web app; no login when unset |
| `QUANTLAB_DATA_DIR` | `data` | Where the database and price cache live |
| `QUANTLAB_PAPER_START_CASH` | `100000` | Starting cash in each paper account |
| `QUANTLAB_PAPER_SLIPPAGE_BPS` / `QUANTLAB_PAPER_COMMISSION_BPS` | `5` / `5` | Paper trading costs |
| `QUANTLAB_RISK_FREE_USD` / `QUANTLAB_RISK_FREE_AUD` | `0.04` | Annual risk-free rates for Sharpe and option pricing |

## Code layout

```
quantlab/
  data/market.py        prices, quotes, option chains (Yahoo Finance, cached)
  data/news.py          headlines and sentiment scoring
  analytics/indicators.py  EMA, RSI, MACD, ATR, Bollinger bands
  analytics/risk.py     Sharpe, Sortino, drawdown, VaR, beta
  analytics/options.py  Black-Scholes, Greeks, implied vol, chain analytics
  backtest.py           strategies and the backtest engine
  forecast.py           ML model with walk-forward validation
  signals.py            composite signal, trade plan, signal track record
  paper.py              paper-trading account
  api.py                FastAPI server
  web/                  the browser interface (no build step)
tests/                  run with: python -m pytest
```
