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
| **Dashboard** also shows | Alerts, and when the daily update last ran. |
| **Ideas** | Scans large US and ASX companies (optionally mid-sized companies, funds, or one sector) and shortlists buy candidates and bearish candidates, with a test of how the scanner did in the past. |
| **Portfolio** | Builds a whole-share portfolio for an amount, a period and a target return, and says how realistic the target is. |
| **Research** | Candlestick chart with moving averages, risk statistics (volatility, Sharpe, drawdown, beta), the signal broken into its parts, an ATR-based trade plan, and recent headlines scored for sentiment. |
| **Options** | Option chain per expiry with implied volatility and Greeks, expected move, put/call ratio, max pain, skew, a volatility smile chart, and a Black-Scholes calculator. |
| **Backtest** | Run EMA crossover, RSI mean reversion, Donchian breakout or MACD strategies over up to 10 years and compare with buy and hold. |
| **My portfolio** | Your real holdings by broker, your brokers' fees, and the cheapest broker for any trade. |
| **Paper trading** | Market, limit, stop and bracket orders in simulated USD and AUD accounts for stocks and options, positions with P&L, beta and delta, trade history, and a track record of how past signals turned out. |
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

Options on the scan: tick **Mid-sized** to add about 70 mid-sized and growth companies, tick
**Funds** to add index and sector funds, or pick one **sector**. The four weakest US stocks are
checked too, and any the full signal rates Sell appear as **bearish ideas**, to be acted on
with put options. Each idea shows its price/earnings ratio and next earnings date, and warns
when earnings are within two weeks.

**Has this scanner worked in the past?** The buttons at the bottom of the screen replay the
price screen week by week over about four years, using only prices known at each point, and
compare its picks with holding every stock in the list equally. When this was built, the US
replay beat its universe by about 5% a year and the ASX replay trailed its universe by about
12% a year, so treat ASX ideas with more suspicion. The replay covers the price screen only
and uses today's list of big companies, which flatters all the results.

What to keep in mind:

- The shortlist is driven mostly by trend and momentum, so it favours stocks that have already
  risen. An idea flagged "overbought" may be a late entry.
- Company facts (valuation, debt, margins, earnings date) are shown for context on each
  Research page, but the signal itself still runs on price and news only.
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
types are advice for your broker, and the stock ticket on the Paper trading screen lets you practise the same market, limit, stop and bracket orders.

## Daily update and alerts

Once a day QuantLab refreshes every watchlist signal, archives news, fills any paper orders
that triggered, records the paper account's value, runs the ideas scan and raises alerts. The
signal track record and news archive only grow on days this runs.

- **While the app is open** it runs by itself at 18:00 local time (`QUANTLAB_DAILY_TIME`, or
  `off`), and catches up at start-up if it missed a day. The Dashboard has a button to run it now.
- **With the app closed**, have your computer's scheduler run this once a day:

  ```bash
  python -m quantlab daily
  ```

  On Windows, this registers it for 6 pm every day (adjust the folder; not yet tried on a real
  Windows machine):

  ```powershell
  schtasks /Create /SC DAILY /ST 18:00 /TN "QuantLab daily" /TR "cmd /c cd /d C:\Users\you\QuantLab && python -m quantlab daily"
  ```

**Alerts** appear on the Dashboard when a watchlist signal changes, a paper order fills, a new
idea appears, or a stock you follow reports earnings within three days. To get them away from
the app:

- **Phone**: install the free [ntfy](https://ntfy.sh) app, subscribe to a long made-up topic
  name, and set `QUANTLAB_NTFY_TOPIC` to the same name. Anyone who knows the topic can read
  the alerts, so make it hard to guess.
- **Email**: set `QUANTLAB_SMTP_HOST`, `QUANTLAB_SMTP_USER`, `QUANTLAB_SMTP_PASSWORD` and
  `QUANTLAB_ALERT_EMAIL`.

Use **Send a test alert** on the Dashboard to check delivery. Neither channel could be tried
during development, so that test is worth doing.

## My portfolio and brokers

The **My portfolio** screen holds what you really own and where.

- **Brokers.** Add each trading provider with the fees from its current price list: fixed fee,
  percentage and minimum for US and ASX shares, the fee per option contract, the currency
  conversion percentage, and what it supports. QuantLab does not know any broker's real fees;
  it only uses what you type.
- **Holdings.** Import a CSV export per broker, or add positions by hand. You see value,
  profit, weight, beta and trend for each, the split across brokers, and warnings about
  concentration, downtrends and upcoming earnings. The app never trades these.
- **Which broker for this trade?** Enter a ticker, what you want to do and the size. It costs
  the trade at each broker (commission plus conversion), leaves out brokers that cannot do it,
  and only considers brokers holding the shares when you are selling. The same recommendation
  appears in each stock's How to buy panel and beside each line of the portfolio builder.

Holdings you import are counted by the portfolio builder (a **To buy** column shows what is
missing) and by the daily earnings alerts.

## Portfolio builder

On the **Portfolio** screen, enter an amount, a market (US or ASX), a period in months and the
return you are aiming for. It opens with an example: 10,000 over 6 months aiming for 5%.

The portfolio is a broad index fund (IVV for the US, VAS.AX for the ASX) for about 40% of the
invested money, up to six current ideas from the latest scan for the rest, and cash. Your
target decides how much is invested rather than held as cash, up to 100%; it never borrows.

Positions are in whole shares unless you tick **Allow fractional shares**, which sizes each
holding to four decimal places so it matches its intended weight exactly. That matters most
for small amounts, where a single share of an expensive stock would otherwise be left out.
Fractions are only sold by some brokers, usually for US shares and often for market orders
only, and rarely for ASX shares; a broker is suggested for a part-share order only if you
marked it as offering fractions under My portfolio.

The screen then says plainly whether the target is realistic, with an estimated chance of
reaching it, the expected return and a likely range. Those figures rest on stated assumptions:
cash earns the risk-free rate, the share market earns 5% a year more, and each holding moves
with the market according to its beta. They deliberately do not assume the ideas keep rising at
their recent pace, so aggressive targets come back marked unlikely rather than being matched
with a riskier portfolio. Tax is ignored.

**Save and track.** Give a built portfolio a name and save it. Tracking then shows its value
against the path your target needs and against the same money in the index, and suggests what
to do: sell a holding that has gone through its stop, review one whose trend has turned, or
trim and add when weights drift more than five points from where they started.

## Paper trading orders

The stock ticket on the Paper trading screen takes three order types:

- **Market** fills now at the latest delayed price.
- **Limit** and **stop** orders wait. Each later day's high and low is checked: a buy limit
  fills when the price trades down to it, a buy stop when it trades up through it. A day that
  opens beyond the order price fills at the open.
- A buy can carry a **stop-loss** and a **take-profit**. When the buy fills, both sell orders
  are placed; whichever triggers first cancels the other.

Because only daily highs and lows are known, the order of events inside one day is not. If a
stop-loss and a take-profit could both have triggered on the same day, the stop is assumed to
have come first.

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

The Options screen also has an **Option strategies** panel: long call, bull call spread, long
put, bear put spread, covered call and protective put, each built from contracts about 45 days
out, with cost, best and worst case, breakeven and a payoff chart. Spreads and covered calls
are for study and for your real broker; the paper account still buys single calls and puts.

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

## News, read two ways

By default each headline gets a word-based sentiment score. With an Anthropic API key and
`QUANTLAB_AI_NEWS=1`, Claude reads each new headline instead: it judges what the news means
for the share price and discards headlines that are not really about the company. That costs
a small amount per batch of headlines on your key (`QUANTLAB_NEWS_MODEL` sets the model,
`claude-opus-5-5` by default). This path has been tested with a stand-in for the API, not a
live key.

Once 60 days of headlines are archived for a stock, their sentiment becomes an input to that
stock's forecast model. Until then the Research page reports how many days have been collected.

## Known limits

- **ASX option chains** are not available from the free source. The option calculator works for any ticker.
- **Option quotes go blank outside US market hours** (Yahoo returns zero bid/ask), so implied
  volatilities are only reliable during the session. Pricing is Black-Scholes, an approximation
  for American-style options.
- **News history cannot be backfilled.** Headlines are archived from the day you start running
  the app, so the forecast only starts using news after about two months of daily runs.
- **Headline sentiment is coarse**: a word-based score of the headline only.
- **Paper trading** is long-only: no short selling and no writing options. Resting orders are
  judged on daily highs and lows, not tick by tick.
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
| `QUANTLAB_DAILY_TIME` | `18:00` | Local time of the automatic daily update while the app is open; `off` disables it |
| `QUANTLAB_NTFY_TOPIC` | none | ntfy topic for phone alerts |
| `QUANTLAB_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `QUANTLAB_ALERT_EMAIL` | none | Email alerts |
| `QUANTLAB_AI_NEWS` | off | Set to `1` to have Claude score news headlines |
| `ANTHROPIC_API_KEY` | none | Enables the assistant and Claude-scored news |
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
  strategies.py         option strategies and payoffs
  brokers.py            your brokers' fees and the cheapest broker for a trade
  tracking.py           real holdings and saved portfolios
  daily.py              the daily update and its scheduler
  alerts.py             alerts and their delivery
  paper.py              paper-trading account (stocks and options)
  chat.py               the assistant and its tools
  api.py                FastAPI server
  web/                  the browser interface (no build step)
tests/                  run with: python -m pytest
```
