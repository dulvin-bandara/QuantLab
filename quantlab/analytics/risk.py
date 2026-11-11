"""Return and risk statistics for a price or equity series."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def returns(prices: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    return prices.pct_change().dropna()


def cagr(equity: pd.Series) -> float:
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0 or equity.iloc[0] <= 0 or equity.iloc[-1] <= 0:
        return float("nan")
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def annual_vol(rets: pd.Series) -> float:
    return float(rets.std(ddof=1) * np.sqrt(TRADING_DAYS))


def sharpe_ratio(rets: pd.Series, risk_free: float = 0.0) -> float:
    """Annualised Sharpe ratio of daily returns. ``risk_free`` is an annual rate."""
    excess = rets - risk_free / TRADING_DAYS
    sd = excess.std(ddof=1)
    return float(excess.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else float("nan")


def sortino_ratio(rets: pd.Series, risk_free: float = 0.0) -> float:
    """Like Sharpe, but only downside moves count as risk."""
    excess = rets - risk_free / TRADING_DAYS
    downside = np.sqrt((excess.clip(upper=0) ** 2).mean())
    return float(excess.mean() / downside * np.sqrt(TRADING_DAYS)) if downside > 0 else float("nan")


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def max_drawdown(equity: pd.Series) -> float:
    return float(drawdown(equity).min())


def value_at_risk(rets: pd.Series, level: float = 0.95) -> float:
    """Historical one-day VaR: the loss exceeded on only (1 - level) of days."""
    return float(-np.quantile(rets, 1 - level)) if len(rets) else float("nan")


def beta(asset_rets: pd.Series, market_rets: pd.Series) -> float:
    """Sensitivity of an asset's returns to the market's (sample covariance / variance)."""
    merged = pd.concat([asset_rets, market_rets], axis=1, join="inner").dropna()
    if len(merged) < 3:
        return float("nan")
    cov = np.cov(merged.iloc[:, 0], merged.iloc[:, 1], ddof=1)
    return float(cov[0, 1] / cov[1, 1]) if cov[1, 1] != 0 else float("nan")


def portfolio_beta(weights: dict[str, float], betas: dict[str, float]) -> float:
    """Weighted average beta. Weights are fractions of the portfolio."""
    return float(sum(w * betas[s] for s, w in weights.items() if not np.isnan(betas[s])))


def summary(equity: pd.Series, risk_free: float = 0.0) -> dict:
    rets = equity.pct_change().dropna()
    return {
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1),
        "cagr": cagr(equity),
        "volatility": annual_vol(rets),
        "sharpe": sharpe_ratio(rets, risk_free),
        "sortino": sortino_ratio(rets, risk_free),
        "max_drawdown": max_drawdown(equity),
        "var_95": value_at_risk(rets),
    }
