"""Composite trade signal: trend + momentum + ML forecast + news sentiment.

Each component is scored from -1 (bearish) to +1 (bullish) and combined with
fixed weights. The ML component is scaled by its out-of-sample skill, so a
model that has not shown it can beat chance contributes nothing.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .analytics import indicators as ind
from .config import settings
from .data import market, news
from .db import connect
from .forecast import forecast

WEIGHTS = {"trend": 0.35, "momentum": 0.20, "ml": 0.25, "news": 0.20}
_cache: dict[tuple, dict] = {}


def label_for(score: float) -> str:
    if score >= 0.5:
        return "Strong Buy"
    if score >= 0.2:
        return "Buy"
    if score <= -0.5:
        return "Strong Sell"
    if score <= -0.2:
        return "Sell"
    return "Hold"


def _sign(x: float) -> float:
    return 0.0 if (x is None or math.isnan(x)) else float(np.sign(x))


def trend_component(df: pd.DataFrame) -> tuple[float, str]:
    c = df["Close"]
    last = c.iloc[-1]
    sma50, sma200 = ind.sma(c, 50).iloc[-1], ind.sma(c, 200).iloc[-1]
    ema20, ema50 = ind.ema(c, 20).iloc[-1], ind.ema(c, 50).iloc[-1]
    votes = [_sign(last - sma50), _sign(ema20 - ema50)]
    if not math.isnan(sma200):
        votes += [_sign(last - sma200), _sign(sma50 - sma200)]
    score = float(np.mean(votes))
    where = "above" if last > sma50 else "below"
    long_term = "" if math.isnan(sma200) else f", {'above' if last > sma200 else 'below'} its 200-day"
    return score, f"Price is {where} its 50-day average{long_term}."


def momentum_component(df: pd.DataFrame) -> tuple[float, str]:
    c = df["Close"]
    rsi = ind.rsi(c).iloc[-1]
    hist = ind.macd(c)["hist"].iloc[-1]
    ret20 = c.iloc[-1] / c.iloc[-21] - 1 if len(c) > 21 else 0.0
    daily_vol = np.log(c).diff().rolling(60).std().iloc[-1]
    z = ret20 / (daily_vol * math.sqrt(20)) if daily_vol and not math.isnan(daily_vol) else 0.0
    parts = [float(np.clip((rsi - 50) / 25, -1, 1)) if not math.isnan(rsi) else 0.0,
             _sign(hist), float(np.clip(z / 2, -1, 1))]
    note = f"RSI {rsi:.0f}, 20-day return {ret20:+.1%}."
    if rsi >= 70:
        note += " Overbought: pullback risk."
    elif rsi <= 30:
        note += " Oversold: bounce possible."
    return float(np.mean(parts)), note


def trade_plan(df: pd.DataFrame, score: float, equity: float, risk_pct: float = 0.01) -> dict:
    """ATR-based stop, target and position size risking ``risk_pct`` of equity."""
    price = float(df["Close"].iloc[-1])
    atr = float(ind.atr(df).iloc[-1])
    direction = 1 if score >= 0 else -1
    stop_dist = 2 * atr
    shares = math.floor(equity * risk_pct / stop_dist) if stop_dist > 0 else 0
    shares = min(shares, math.floor(equity * 0.25 / price))  # never more than 25% in one name
    return {
        "atr": atr,
        "stop": price - direction * stop_dist,
        "target": price + direction * 3 * atr,
        "shares": shares,
        "position_value": shares * price,
        "risk_amount": shares * stop_dist,
    }


def compute_signal(symbol: str, use_ml: bool = True, use_news: bool = True) -> dict:
    symbol = market.normalize_symbol(symbol)
    df = market.get_history(symbol)
    if len(df) < 60:
        raise market.DataError(f"{symbol} has only {len(df)} days of history; at least 60 are needed.")
    as_of = df.index[-1].strftime("%Y-%m-%d")

    sentiment = news.news_sentiment(symbol) if use_news else None
    key = (symbol, as_of, round(float(df["Close"].iloc[-1]), 4), use_ml,
           sentiment["count"] if sentiment else -1, round(sentiment["score"], 2) if sentiment else 0)
    if key in _cache:
        return _cache[key]

    comps: dict[str, dict] = {}
    s, note = trend_component(df)
    comps["trend"] = {"score": s, "note": note}
    s, note = momentum_component(df)
    comps["momentum"] = {"score": s, "note": note}

    fc = None
    if use_ml:
        try:
            bench = market.get_history(market.benchmark_of(symbol))
        except market.DataError:
            bench = None
        fc = forecast(df, bench)
        if fc["available"]:
            s = (fc["prob_up"] - 0.5) * 2 * fc["skill"]
            v = fc["validation"]
            note = (f"Model gives {fc['prob_up']:.0%} odds of a higher close in {fc['horizon']} days; "
                    f"out-of-sample accuracy {v['accuracy']:.0%} (AUC {v['auc']:.2f}).")
            if fc["skill"] == 0:
                note += " No demonstrated edge, so it is ignored."
            comps["ml"] = {"score": float(s), "note": note}

    if sentiment and sentiment["count"]:
        confidence = min(1.0, sentiment["count"] / 5)
        s = float(np.clip(sentiment["score"] / 0.3, -1, 1)) * confidence
        comps["news"] = {"score": s, "note": f"{sentiment['count']} headlines in 7 days: "
                         f"{sentiment['positive']} positive, {sentiment['negative']} negative."}

    total_w = sum(WEIGHTS[k] for k in comps)
    score = sum(WEIGHTS[k] * c["score"] for k, c in comps.items()) / total_w
    for k, c in comps.items():
        c["weight"] = WEIGHTS[k] / total_w

    result = {
        "symbol": symbol,
        "as_of": as_of,
        "price": float(df["Close"].iloc[-1]),
        "currency": market.currency_of(symbol),
        "score": float(score),
        "label": label_for(score),
        "components": comps,
        "forecast": fc,
        "news": sentiment,
        "plan": trade_plan(df, score, settings.paper_start_cash),
    }
    if use_ml and use_news:
        with connect() as conn:  # log the full signal once per bar so its record can be checked later
            conn.execute("INSERT OR REPLACE INTO signal_log (symbol, date, score, label, close) VALUES (?,?,?,?,?)",
                         (symbol, as_of, result["score"], result["label"], result["price"]))
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = result
    return result


def signal_performance(horizon: int = 5) -> dict:
    """How logged signals actually did: forward return ``horizon`` trading days after each one."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM signal_log ORDER BY date")]
    by_label: dict[str, list[float]] = {}
    pending = 0
    for r in rows:
        try:
            close = market.get_history(r["symbol"])["Close"]
        except market.DataError:
            continue
        after = close[close.index > pd.Timestamp(r["date"])]
        if len(after) < horizon:
            pending += 1
            continue
        by_label.setdefault(r["label"], []).append(float(after.iloc[horizon - 1] / r["close"] - 1))
    order = ["Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"]
    return {
        "horizon": horizon,
        "logged": len(rows),
        "pending": pending,
        "by_label": [{"label": k, "count": len(by_label[k]), "avg_return": float(np.mean(by_label[k])),
                      "pct_up": float(np.mean([x > 0 for x in by_label[k]]))} for k in order if k in by_label],
    }
