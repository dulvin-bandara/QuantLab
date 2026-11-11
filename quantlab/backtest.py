"""Strategies and a daily-bar backtester.

A strategy maps price history to a target position for each day: 1 = fully
long, 0 = in cash, -1 = fully short. The engine assumes you decide at a day's
close and hold that position over the *next* day, so no strategy can profit
from information it would not have had. Costs are charged on every change in
position.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .analytics import indicators as ind
from .analytics import risk


def _latch(entries: pd.Series, exits: pd.Series) -> pd.Series:
    """Hold 1 from each entry until the next exit."""
    state = pd.Series(np.nan, index=entries.index)
    state[entries.fillna(False)] = 1.0
    state[exits.fillna(False)] = 0.0
    return state.ffill().fillna(0.0)


def ema_cross(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    """Long while the fast EMA is above the slow EMA (trend following)."""
    f, s = ind.ema(df["Close"], int(fast)), ind.ema(df["Close"], int(slow))
    return (f > s).astype(float).where(s.notna(), 0.0)


def rsi_reversion(df: pd.DataFrame, period: int = 14, buy_below: float = 30, sell_above: float = 55) -> pd.Series:
    """Buy when RSI is oversold, sell once it recovers (mean reversion)."""
    r = ind.rsi(df["Close"], int(period))
    return _latch(r < buy_below, r > sell_above)


def breakout(df: pd.DataFrame, entry: int = 20, exit: int = 10) -> pd.Series:
    """Buy a close above the prior N-day high, sell a close below the prior M-day low (Donchian)."""
    hi = df["High"].rolling(int(entry)).max().shift(1)
    lo = df["Low"].rolling(int(exit)).min().shift(1)
    return _latch(df["Close"] > hi, df["Close"] < lo)


def macd_trend(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """Long while the MACD line is above its signal line."""
    m = ind.macd(df["Close"], int(fast), int(slow), int(signal))
    return (m["hist"] > 0).astype(float)


STRATEGIES = {
    "ema_cross": {"fn": ema_cross, "label": "EMA crossover",
                  "params": {"fast": 20, "slow": 50}},
    "rsi_reversion": {"fn": rsi_reversion, "label": "RSI mean reversion",
                      "params": {"period": 14, "buy_below": 30, "sell_above": 55}},
    "breakout": {"fn": breakout, "label": "Donchian breakout",
                 "params": {"entry": 20, "exit": 10}},
    "macd_trend": {"fn": macd_trend, "label": "MACD trend",
                   "params": {"fast": 12, "slow": 26, "signal": 9}},
}


def _trades(close: pd.Series, held: pd.Series) -> list[dict]:
    """Round trips, priced at the close on which each decision was made."""
    out, open_trade = [], None
    prev = 0.0
    decided = held.shift(-1).fillna(held.iloc[-1])  # position chosen at each close
    for date, pos in decided.items():
        if pos != prev:
            px = float(close.loc[date])
            if open_trade:
                d = open_trade.pop("_dir")
                open_trade.update(exit_date=date.strftime("%Y-%m-%d"), exit_price=px,
                                  **{"return": d * (px / open_trade["entry_price"] - 1)})
                out.append(open_trade)
                open_trade = None
            if pos != 0:
                open_trade = {"side": "long" if pos > 0 else "short", "_dir": 1 if pos > 0 else -1,
                              "entry_date": date.strftime("%Y-%m-%d"), "entry_price": px}
            prev = pos
    if open_trade:
        d = open_trade.pop("_dir")
        px = float(close.iloc[-1])
        open_trade.update(exit_date=None, exit_price=px, **{"return": d * (px / open_trade["entry_price"] - 1)})
        out.append(open_trade)
    return out


def run_backtest(df: pd.DataFrame, position: pd.Series, cost_bps: float = 5.0,
                 initial: float = 10000.0, risk_free: float = 0.0) -> dict:
    close = df["Close"]
    rets = close.pct_change().fillna(0.0)
    held = position.reindex(close.index).fillna(0.0).shift(1).fillna(0.0)
    turnover = held.diff().abs().fillna(held.abs())
    strat = held * rets - turnover * cost_bps / 1e4
    equity = initial * (1 + strat).cumprod()
    bench = initial * (1 + rets).cumprod()

    trades = _trades(close, held)
    closed = [t for t in trades if t["exit_date"]]
    stats = risk.summary(equity, risk_free)
    stats.update({
        "trades": len(trades),
        "win_rate": float(np.mean([t["return"] > 0 for t in closed])) if closed else None,
        "avg_trade": float(np.mean([t["return"] for t in closed])) if closed else None,
        "exposure": float((held != 0).mean()),
    })
    return {"stats": stats, "benchmark": risk.summary(bench, risk_free),
            "equity": equity, "benchmark_equity": bench, "trades": trades}


def backtest_strategy(df: pd.DataFrame, name: str, params: dict | None = None, **kwargs) -> dict:
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{name}'")
    spec = STRATEGIES[name]
    merged = {**spec["params"], **{k: v for k, v in (params or {}).items() if k in spec["params"]}}
    return run_backtest(df, spec["fn"](df, **merged), **kwargs)
