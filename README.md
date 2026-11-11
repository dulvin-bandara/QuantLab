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
| **Ideas** | Scans about 170 of the largest US and ASX companies and shortlists buy candidates, each with reasons, a stop, a target and a position size. |
| **Portfolio** | Builds a whole-share portfolio for an amount, a period and a target return, and says how realistic the target is. |
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

## Trade ideas

The **Ideas** screen does the first pass of research for you. Press **Scan now** and, in about
a minute, it:

1. screens every stock in a fixed list of the largest US (about 115) and ASX (about 55)
   companies on price trend and momentum;
2. runs the full signal (trend, momentum, ML forecast, news) on the eight strongest per market;
3. shows only those the full signal rates Buy or better, ranked by score.

What to keep in mind:

- The shortlist is driven mostly by trend and momentum, so it favours stocks that have already
  risen. An idea flagged "overbought" may be a late entry.
- It has no view of fundamentals (earnings, valuation, debt) and no knowledge of upcoming
  events such as earnings dates.
- When few stocks are in an uptrend, the list is short or empty. That is intended.
- The universe is edited in `quantlab/ideas.py`.

Use the ideas as candidates to check, then paper trade them and watch the signal track record
before committing real money.

## How to buy, and the option alternative

Every idea, and every stock's Research page, has a **How to buy** panel:

- **Minimum money needed**: the smallest purchase (one share in the US; at least A$500 on a
  first ASX purchase), the account size at which that purchase risks no more than 1% at the
  stop, and the cost of one option contract.
- **Ways to buy**: market, limit, pullback-limit and breakout buy-stop orders, each with a
  suggested price, and which one suits the exchange's session at the time you plan to buy.
  Pick a date and time, or leave it on now. Outside market hours it steers you to limit
  orders; in the first half hour and last minutes of a session it advises caution; for a time
  days away it suggests a resting conditional order instead.
- **Option alternative** (US stocks): one call option about 45 days out with a delta near
  0.65, with its cost, breakeven, what it makes at the stock's target and loses at its stop,
  and warnings about stale quotes, thin trading or wide spreads.

The session logic knows exchange hours and daylight saving but not public holidays. Order
types are advice for your broker; paper trading inside the app still fills market orders only.

## Portfolio builder

On the **Portfolio** screen, enter an amount, a market (US or ASX), a period in months and the
return you are aiming for. It opens with an example: 10,000 over 6 months aiming for 5%.

The portfolio is a broad index fund (IVV for the US, VAS.AX for the ASX) for about 40% of the
invested money, up to six current ideas from the latest scan for the rest, and cash. Your
target decides how much is invested rather than held as cash, up to 100%; it never borrows.

The screen then says plainly whether the target is realistic, with an estimated chance of
reaching it, the expected return and a likely range. Those figures rest on stated assumptions:
cash earns the risk-free rate, the share market earns 5% a year more, and each holding moves
with the market according to its beta. They deliberately do not assume the ideas keep rising at
their recent pace, so aggressive targets come back marked unlikely rather than being matched
with a riskier portfolio. Tax, brokerage and currency conversion are ignored.

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
  ideas.py              the stock scanner behind the Ideas screen
  planner.py            buy guide, option suggestion and portfolio builder
  paper.py              paper-trading account (stocks and options)
  chat.py               the assistant and its tools
  api.py                FastAPI server
  web/                  the browser interface (no build step)
tests/                  run with: python -m pytest
```
