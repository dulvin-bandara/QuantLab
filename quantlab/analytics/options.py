"""Option pricing and option-chain analytics.

Pricing is Black-Scholes-Merton with a continuous dividend yield. Listed US and
ASX equity options are American-style, so treat these values as close
approximations: accurate for calls on low-dividend stocks, slightly low for
deep in-the-money puts.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

from ..data.market import OptionChain


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float) -> tuple[float, float]:
    vt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vt
    return d1, d1 - vt


def bs_price(S: float, K: float, T: float, r: float, sigma: float, kind: str = "call", q: float = 0.0) -> float:
    """Option value. T is in years, r/q/sigma are annual decimals, kind is 'call' or 'put'."""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if kind == "call" else max(K - S, 0.0)
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    if kind == "call":
        return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, kind: str = "call", q: float = 0.0) -> dict:
    """Delta, gamma, vega (per 1 vol point), theta (per calendar day), rho (per 1% rate)."""
    if T <= 0 or sigma <= 0:
        itm = (S > K) if kind == "call" else (S < K)
        return {"delta": (1.0 if kind == "call" else -1.0) if itm else 0.0,
                "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    eq, er, pdf = math.exp(-q * T), math.exp(-r * T), norm.pdf(d1)
    common = -S * eq * pdf * sigma / (2 * math.sqrt(T))
    if kind == "call":
        delta = eq * norm.cdf(d1)
        theta = common - r * K * er * norm.cdf(d2) + q * S * eq * norm.cdf(d1)
        rho = K * T * er * norm.cdf(d2)
    else:
        delta = -eq * norm.cdf(-d1)
        theta = common + r * K * er * norm.cdf(-d2) - q * S * eq * norm.cdf(-d1)
        rho = -K * T * er * norm.cdf(-d2)
    return {
        "delta": delta,
        "gamma": eq * pdf / (S * sigma * math.sqrt(T)),
        "vega": S * eq * pdf * math.sqrt(T) / 100,
        "theta": theta / 365,
        "rho": rho / 100,
    }


def implied_vol(price: float, S: float, K: float, T: float, r: float, kind: str = "call", q: float = 0.0) -> float:
    """Volatility that reproduces ``price``; NaN when the price admits none."""
    if T <= 0 or price <= 0 or S <= 0 or K <= 0:
        return float("nan")
    f = lambda sig: bs_price(S, K, T, r, sig, kind, q) - price
    lo, hi = 1e-4, 6.0
    try:
        if f(lo) > 0 or f(hi) < 0:
            return float("nan")
        return float(brentq(f, lo, hi, xtol=1e-6))
    except ValueError:
        return float("nan")


def years_to_expiry(expiry: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    end = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=21, tzinfo=timezone.utc)  # ~US close
    return max((end - now).total_seconds() / (365 * 86400), 0.5 / 365)


def _num(v) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _side(df: pd.DataFrame, kind: str, S: float, T: float, r: float, q: float) -> list[dict]:
    rows = []
    for rec in df.to_dict("records"):
        K = _num(rec.get("strike"))
        if K <= 0 or not (0.6 * S <= K <= 1.4 * S):
            continue
        bid, ask, last = _num(rec.get("bid")), _num(rec.get("ask")), _num(rec.get("lastPrice"))
        quoted = bid > 0 and ask > 0
        mid = (bid + ask) / 2 if quoted else last
        iv = implied_vol(mid, S, K, T, r, kind, q)
        row = {"strike": K, "bid": bid, "ask": ask, "last": last, "mid": mid, "quoted": quoted,
               "volume": int(_num(rec.get("volume"))), "open_interest": int(_num(rec.get("openInterest"))),
               "iv": None if math.isnan(iv) else iv}
        if row["iv"]:
            row.update(bs_greeks(S, K, T, r, iv, kind, q))
        rows.append(row)
    return rows


def max_pain(calls: list[dict], puts: list[dict]) -> float | None:
    """Strike at which option holders collectively lose the most at expiry."""
    strikes = sorted({r["strike"] for r in calls + puts})
    if not strikes or not any(r["open_interest"] for r in calls + puts):
        return None
    def payout(px: float) -> float:
        return (sum(max(px - c["strike"], 0) * c["open_interest"] for c in calls)
                + sum(max(p["strike"] - px, 0) * p["open_interest"] for p in puts))
    return min(strikes, key=payout)


def analyze_chain(chain: OptionChain, r: float, q: float = 0.0, now: datetime | None = None) -> dict:
    """Implied vols, Greeks and headline statistics for one expiry.

    Implied vol is recomputed from the bid/ask midpoint (or the last trade when
    there is no two-sided quote), because Yahoo's own IV column is unreliable
    outside market hours.
    """
    S, T = chain.spot, years_to_expiry(chain.expiry, now)
    calls, puts = _side(chain.calls, "call", S, T, r, q), _side(chain.puts, "put", S, T, r, q)
    summary: dict = {"spot": S, "days_to_expiry": round(T * 365, 1)}

    by_k = {c["strike"]: [c, None] for c in calls}
    for p in puts:
        by_k.setdefault(p["strike"], [None, None])[1] = p
    both = [k for k, (c, p) in by_k.items() if c and p]
    if both:
        atm = min(both, key=lambda k: abs(k - S))
        c, p = by_k[atm]
        ivs = [x["iv"] for x in (c, p) if x["iv"]]
        straddle = c["mid"] + p["mid"]
        summary.update({
            "atm_strike": atm,
            "atm_iv": float(np.mean(ivs)) if ivs else None,
            # The ATM straddle price approximates the move the market is pricing in by expiry.
            "expected_move": straddle,
            "expected_move_pct": straddle / S,
        })
    c_oi, p_oi = sum(c["open_interest"] for c in calls), sum(p["open_interest"] for p in puts)
    c_vol, p_vol = sum(c["volume"] for c in calls), sum(p["volume"] for p in puts)
    summary["put_call_oi"] = p_oi / c_oi if c_oi else None
    summary["put_call_volume"] = p_vol / c_vol if c_vol else None
    summary["max_pain"] = max_pain(calls, puts)

    # Skew: how much more volatility downside puts cost than upside calls (~5% out of the money).
    otm_put = min((p for p in puts if p["iv"] and p["strike"] < S), key=lambda p: abs(p["strike"] - 0.95 * S), default=None)
    otm_call = min((c for c in calls if c["iv"] and c["strike"] > S), key=lambda c: abs(c["strike"] - 1.05 * S), default=None)
    summary["skew"] = otm_put["iv"] - otm_call["iv"] if otm_put and otm_call else None
    return {"symbol": chain.symbol, "expiry": chain.expiry, "summary": summary, "calls": calls, "puts": puts}
