# QuantLab

A personal quant research platform for **US and ASX stocks and options**: live-ish market
data, news sentiment, a machine-learning forecast, strategy backtesting, option analytics
and a paper-trading account, all behind a web interface that runs on your own computer.

Everything runs on free data (Yahoo Finance prices and option chains, Yahoo and Google News
headlines). No API keys are needed, except for the optional assistant, which uses your own
Anthropic API key.

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
| **Paper trading** | Simulated USD and AUD accounts for stocks and options, positions with P&L, beta and delta, trade history, and a track record of how past signals turned out. |
| **Assistant** | Ask questions in plain English; Claude answers by looking up the app's live data. |

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

## Paper trading options

Open a US ticker's **Options** screen, pick an expiry, and use the ticket above the chain
(clicking a row in the chain loads that strike). One contract covers 100 shares.

- You can buy calls and puts and sell what you hold. Writing (shorting) options and multi-leg
  orders are not simulated.
- Buys fill at the ask and sells at the bid. When Yahoo has no live quote, which is usual
  outside US market hours, both use the last traded price, and the fill says so.
- Open positions are valued at the bid/ask midpoint, with delta shown as equivalent shares.
- Contracts past their expiry date are settled automatically at intrinsic value.
- Commission is 0.65 per contract (`QUANTLAB_PAPER_OPTION_FEE`).

## The assistant

The **Assistant** tab answers questions such as "what's driving the signal on BHP.AX?" or
"what move are options pricing in for AAPL next month?". It uses Claude with read-only tools
over this app's data: quotes, signals, news, option chains, backtests and the paper account.
It cannot place trades.

To turn it on, create an API key at https://console.anthropic.com and set it before starting the app:

```bash
export ANTHROPIC_API_KEY=your-key
```

On Windows PowerShell:

```powershell
$env:ANTHROPIC_API_KEY = "your-key"
```

Each question is billed to that key. The model defaults to `claude-opus-5-5`
(`QUANTLAB_CHAT_MODEL` changes it). Set `QUANTLAB_CHAT_WEB_SEARCH=1` to also let it search the
web, if web search is enabled for your Anthropic account. If a request is declined by a safety
filter, the API is allowed to retry it on a fallback model.

## Known limits

- **ASX option chains** are not available from the free source. The option calculator works for any ticker.
- **Option quotes go blank outside US market hours** (Yahoo returns zero bid/ask), so implied
  volatilities are only reliable during the session. Pricing is Black-Scholes, an approximation
  for American-style options.
- **News history cannot be backfilled.** Headlines are archived from the day you start running
  the app, so news sentiment feeds the live signal but not yet the ML model's training.
- **Headline sentiment is coarse**: a word-based score of the headline only.
- **Paper trading** is long-only: no short selling, no writing options, market orders only.
- **The assistant** sees the same delayed data as the rest of the app and has not been exercised
  against a live API key in development; conversations are kept only in the browser tab.
- Prices are end-of-day or delayed; this is built for swing trading (days to weeks), not intraday.

## Running it

QuantLab runs on your own computer and is only reachable from that computer: the server
listens on `127.0.0.1`, so nothing on your network or the internet can open it. Leave the
terminal window open while you use it and press Ctrl+C to stop it. Your watchlist, paper
account and news archive are kept in the `data` folder next to the code.

## Settings

All optional, set as environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `QUANTLAB_PASSWORD` | none | Optional password for the web app; not needed for local use |
| `QUANTLAB_DATA_DIR` | `data` | Where the database and price cache live |
| `QUANTLAB_PAPER_START_CASH` | `100000` | Starting cash in each paper account |
| `QUANTLAB_PAPER_SLIPPAGE_BPS` / `QUANTLAB_PAPER_COMMISSION_BPS` | `5` / `5` | Stock paper trading costs |
| `QUANTLAB_PAPER_OPTION_FEE` | `0.65` | Commission per option contract |
| `ANTHROPIC_API_KEY` | none | Enables the assistant |
| `QUANTLAB_CHAT_MODEL` | `claude-opus-5-5` | Model the assistant uses |
| `QUANTLAB_CHAT_WEB_SEARCH` | off | Set to `1` to let the assistant search the web |
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
  paper.py              paper-trading account (stocks and options)
  chat.py               the assistant and its tools
  api.py                FastAPI server
  web/                  the browser interface (no build step)
tests/                  run with: python -m pytest
```
