"""The assistant: ask questions in plain English, answered from QuantLab's live data.

Claude answers through tools that read the same data the rest of the app uses
(quotes, signals, news, option chains, backtests, the paper account). The tools
are read-only: the assistant can look things up but cannot place trades.

Needs an Anthropic API key in the ANTHROPIC_API_KEY environment variable.
Each question is billed to that key.
"""
from __future__ import annotations

import json
import os
from datetime import date

from . import backtest, paper, signals
from .analytics import options as opt
from .analytics import risk
from .config import settings
from .data import market, news
from .db import connect

MAX_HISTORY = 40
MAX_CHARS = 8000

SYSTEM = """You are the assistant inside QuantLab, a personal quant research app used by one \
person, its owner, for swing trading (days to weeks) in US and ASX stocks and US options.

Answer from the app's data: call the tools rather than relying on memory for anything about \
prices, signals, news, options or the paper account. Your own knowledge of markets is useful for \
explaining concepts and interpreting what the tools return, but it is not current.

What to know about the data:
- Prices are free Yahoo Finance data: end of day or delayed, never real time.
- ASX tickers end in .AX (BHP.AX). ASX option chains are not available; US ones are.
- Option quotes are often blank outside US market hours, which makes implied volatility unreliable then.
- The signal score runs from -1 (bearish) to +1 (bullish) and blends trend, momentum, an ML \
forecast and news sentiment. The ML forecast is only counted when it has shown out-of-sample skill; \
for most stocks it has little or none, so say so when it matters.
- The paper account is simulated money.

How to answer: lead with the answer, then the numbers behind it. Be direct about uncertainty and \
about what the data cannot tell you. Give your honest read when asked what you think, including \
when the evidence is weak or mixed; the owner makes the decision. Keep replies short unless asked \
for depth.

Formatting: replies are shown as plain text. Use short paragraphs, simple "- " bullet lists and \
**bold** for key figures. Do not use headings, tables or code blocks."""


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _json(obj) -> str:
    from .api import clean

    return json.dumps(clean(obj), default=str)


def _safe(fn):
    """Tool failures go back to the model as text so it can explain them, rather than crashing the chat."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (market.DataError, ValueError, KeyError) as exc:
            return f"Error: {exc}"

    return wrapper


@_safe
def get_watchlist() -> str:
    """List the owner's watchlist with each ticker's latest price, day change and signal.

    Use this for questions about the watchlist as a whole, such as which names look strongest.
    """
    with connect() as conn:
        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist ORDER BY symbol")]
    rows = []
    for s in symbols or list(settings.default_watchlist):
        try:
            q, sig = market.get_quote(s), signals.compute_signal(s)
            rows.append({"symbol": s, "name": q["name"], "price": q["price"], "currency": q["currency"],
                         "day_change_pct": q["change_pct"], "as_of": q["as_of"],
                         "signal": sig["label"], "score": sig["score"]})
        except market.DataError as exc:
            rows.append({"symbol": s, "error": str(exc)})
    return _json(rows)


@_safe
def get_signal(symbol: str) -> str:
    """Get the full QuantLab signal for one ticker.

    Returns the latest price, the composite score and label, each component (trend, momentum,
    ML forecast, news) with its weight and explanation, the forecast's out-of-sample validation
    statistics, and an ATR-based stop, target and position size.

    Args:
        symbol: Ticker in Yahoo format, e.g. AAPL or BHP.AX.
    """
    return _json(signals.compute_signal(symbol))


@_safe
def get_price_stats(symbol: str, years: float = 1.0) -> str:
    """Get price performance and risk statistics for a ticker over a lookback period.

    Returns the period return, annual volatility, Sharpe ratio, maximum drawdown, beta against
    the market index, 52-week style high and low for the period, and the last 10 daily closes.

    Args:
        symbol: Ticker in Yahoo format, e.g. AAPL or BHP.AX.
        years: Lookback in years, between 0.1 and 10.
    """
    symbol = market.normalize_symbol(symbol)
    df = market.get_history(symbol, years=max(0.1, min(float(years), 10)))
    close = df["Close"]
    rets = close.pct_change().dropna()
    bench = market.get_history(market.benchmark_of(symbol), years=2)["Close"].pct_change()
    return _json({
        "quote": market.get_quote(symbol), "from": df.index[0].strftime("%Y-%m-%d"),
        "period_return": close.iloc[-1] / close.iloc[0] - 1, "volatility": risk.annual_vol(rets),
        "sharpe": risk.sharpe_ratio(rets, settings.risk_free(market.currency_of(symbol))),
        "max_drawdown": risk.max_drawdown(close), "period_high": close.max(), "period_low": close.min(),
        "beta": risk.beta(market.get_history(symbol, years=2)["Close"].pct_change(), bench),
        "benchmark": market.benchmark_of(symbol),
        "last_10_closes": {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in close.tail(10).items()},
    })


@_safe
def get_news(symbol: str) -> str:
    """Get the last 7 days of news headlines for a ticker with a sentiment score for each.

    Sentiment is a word-based score of the headline only, from -1 (negative) to +1 (positive);
    read the headlines yourself rather than trusting the scores blindly.

    Args:
        symbol: Ticker in Yahoo format, e.g. AAPL or BHP.AX.
    """
    items = news.get_news(symbol, limit=25)
    return _json({"summary": news.summarize(items),
                  "headlines": [{k: it[k] for k in ("title", "source", "published", "sentiment")} for it in items]})


@_safe
def get_option_expiries(symbol: str) -> str:
    """List the option expiry dates available for a US ticker. Call this before get_option_chain.

    Args:
        symbol: US ticker, e.g. AAPL. ASX tickers have no chains.
    """
    return _json(market.get_expiries(symbol))


@_safe
def get_option_chain(symbol: str, expiry: str) -> str:
    """Get one expiry of a ticker's option chain: summary statistics plus contracts near the money.

    The summary has at-the-money implied volatility, the expected move priced in by expiry,
    put/call ratios, max pain and skew. Contracts within 12% of the share price are listed with
    bid, ask, last, implied volatility, delta, volume and open interest. `quoted` is false when
    there is no live bid/ask, in which case implied volatility comes from a stale last trade.

    Args:
        symbol: US ticker, e.g. AAPL.
        expiry: Expiry date as YYYY-MM-DD, taken from get_option_expiries.
    """
    chain = market.get_chain(symbol, expiry)
    res = opt.analyze_chain(chain, settings.risk_free(market.currency_of(chain.symbol)))
    keep = ("strike", "bid", "ask", "last", "iv", "delta", "volume", "open_interest", "quoted")
    near = lambda rows: [{k: r.get(k) for k in keep} for r in rows if abs(r["strike"] / chain.spot - 1) <= 0.12]
    return _json({"symbol": res["symbol"], "expiry": expiry, "summary": res["summary"],
                  "calls": near(res["calls"]), "puts": near(res["puts"])})


@_safe
def price_option(spot: float, strike: float, days: float, volatility: float, kind: str = "call",
                 rate: float = 0.04, dividend_yield: float = 0.0) -> str:
    """Price a hypothetical option with Black-Scholes and return its value and Greeks.

    Useful for what-if questions and for ASX names, which have no chain data.

    Args:
        spot: Share price.
        strike: Strike price.
        days: Calendar days until expiry.
        volatility: Annual volatility as a decimal, e.g. 0.25 for 25%.
        kind: "call" or "put".
        rate: Annual risk-free rate as a decimal.
        dividend_yield: Annual dividend yield as a decimal.
    """
    if kind not in ("call", "put") or min(spot, strike, days, volatility) <= 0:
        raise ValueError("kind must be 'call' or 'put', and spot, strike, days and volatility must be positive")
    args = (spot, strike, days / 365, rate, volatility, kind, dividend_yield)
    return _json({"price_per_share": opt.bs_price(*args), **opt.bs_greeks(*args)})


@_safe
def run_backtest(symbol: str, strategy: str = "ema_cross", years: float = 5.0) -> str:
    """Backtest a built-in strategy on a ticker with default parameters and compare it with buy and hold.

    Returns return, volatility, Sharpe, drawdown, trade count and win rate for the strategy and
    for buy and hold over the same period. Costs of 5 basis points per trade are included.

    Args:
        symbol: Ticker in Yahoo format, e.g. AAPL or BHP.AX.
        strategy: One of ema_cross, rsi_reversion, breakout, macd_trend.
        years: Years of history to test, between 1 and 10.
    """
    df = market.get_history(symbol, years=max(1, min(float(years), 10)))
    res = backtest.backtest_strategy(df, strategy)
    return _json({"strategy": strategy, "from": df.index[0].strftime("%Y-%m-%d"), "to": df.index[-1].strftime("%Y-%m-%d"),
                  "strategy_stats": res["stats"], "buy_and_hold": res["benchmark"]})


@_safe
def get_paper_account() -> str:
    """Get the simulated paper-trading account: cash and equity per currency, open stock and
    option positions with profit and loss, and the 20 most recent trades."""
    acct = paper.account()
    for b in acct["books"]:
        b.pop("history", None)
    return _json({**acct, "recent_trades": paper.trades(20)})


TOOL_FUNCTIONS = [get_watchlist, get_signal, get_price_stats, get_news, get_option_expiries, get_option_chain,
                  price_option, run_backtest, get_paper_account]


class ChatError(Exception):
    pass


def _client():
    import anthropic

    return anthropic.Anthropic()


def reply(messages: list[dict], viewing: str | None = None) -> dict:
    """Answer the latest user message. ``messages`` is the plain-text conversation so far."""
    import anthropic
    from anthropic import beta_tool

    if not is_enabled():
        raise ChatError("The assistant is not set up. Set the ANTHROPIC_API_KEY environment variable and restart.")
    history = [{"role": m["role"], "content": str(m["content"])[:MAX_CHARS]} for m in messages[-MAX_HISTORY:]
               if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]
    while history and history[0]["role"] != "user":
        history.pop(0)
    if not history or history[-1]["role"] != "user":
        raise ChatError("Send a question to get a reply.")

    context = f"Today is {date.today():%A %d %B %Y}."
    if viewing:
        context += f" The owner currently has {viewing} open in the app."
    tools: list = [beta_tool(fn) for fn in TOOL_FUNCTIONS]
    if settings.chat_web_search:
        tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 3})

    used: list[str] = []
    try:
        runner = _client().beta.messages.tool_runner(
            model=settings.chat_model,
            max_tokens=16000,
            system=[{"type": "text", "text": SYSTEM}, {"type": "text", "text": context}],
            tools=tools,
            messages=history,
            max_iterations=12,
            # If a request is declined by a safety classifier, let the API retry it on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        final = None
        for message in runner:
            final = message
            used += [b.name for b in message.content if b.type == "tool_use"]
    except anthropic.AuthenticationError as exc:
        raise ChatError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.") from exc
    except anthropic.RateLimitError as exc:
        raise ChatError("The Anthropic API rate limit was hit. Wait a moment and try again.") from exc
    except anthropic.APIStatusError as exc:
        raise ChatError(f"The Anthropic API returned an error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ChatError("Could not reach the Anthropic API. Check the internet connection.") from exc

    if final is None:
        raise ChatError("The assistant returned nothing.")
    if final.stop_reason == "refusal":
        raise ChatError("The assistant declined to answer that request.")
    text = "\n\n".join(b.text for b in final.content if b.type == "text").strip()
    if not text:
        text = "I ran out of steps before reaching an answer. Try a narrower question."
    return {"reply": text, "tools_used": used}
