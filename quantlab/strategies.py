"""Option strategies for a stock, built from its live chain, with payoff at expiry.

Six standard structures, each from contracts about 45 days out:

* bullish: long call, bull call spread
* bearish: long put, bear put spread
* for shares you already own: covered call, protective put

Each comes with its legs, cost, best and worst case, breakeven and a payoff
curve. The paper account can only buy single calls and puts, so spreads and
covered calls here are for study and for your broker, not for paper trading.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from . import signals
from .analytics import options as opt
from .config import settings
from .data import market

CONTRACT = 100


def _expiry(symbol: str) -> tuple[str | None, int, str]:
    try:
        expiries = market.get_expiries(symbol)
    except market.DataError:
        expiries = []
    if not expiries:
        return None, 0, ("No option chain is available for this stock from the free data source"
                         + (" (ASX chains are not published there)." if market.is_asx(symbol) else "."))
    today = datetime.now(timezone.utc).date()
    dte = {e: (datetime.strptime(e, "%Y-%m-%d").date() - today).days for e in expiries}
    ok = [e for e in expiries if 21 <= dte[e] <= 90]
    if not ok:
        return None, 0, "No expiry between 3 weeks and 3 months out is listed."
    best = min(ok, key=lambda e: abs(dte[e] - 45))
    return best, dte[best], ""


def _pick(rows: list[dict], delta: float, exclude: float | None = None) -> dict | None:
    """Contract with a usable price whose delta is closest to ``delta`` (live quotes preferred)."""
    usable = [r for r in rows if r.get("delta") is not None and (r["quoted"] or r["last"] > 0) and r["strike"] != exclude]
    usable = [r for r in usable if abs(r["delta"] - delta) <= 0.25]
    quoted = [r for r in usable if r["quoted"]]
    pool = quoted or usable
    return min(pool, key=lambda r: abs(r["delta"] - delta)) if pool else None


def _leg(action: str, kind: str, row: dict) -> dict:
    px = (row["ask"] if action == "buy" else row["bid"]) if row["quoted"] else row["last"]
    return {"action": action, "kind": kind, "strike": row["strike"], "premium": px, "quoted": row["quoted"],
            "delta": row["delta"], "open_interest": row["open_interest"]}


def _payoff(legs: list[dict], shares: int, spot: float, prices: np.ndarray) -> np.ndarray:
    total = shares * (prices - spot)
    for leg in legs:
        intrinsic = np.maximum(prices - leg["strike"], 0) if leg["kind"] == "call" else np.maximum(leg["strike"] - prices, 0)
        sign = 1 if leg["action"] == "buy" else -1
        total = total + sign * (intrinsic - leg["premium"]) * CONTRACT
    return total


def _build(name: str, outlook: str, legs: list[dict], shares: int, spot: float, what: str, risk_note: str) -> dict:
    wide = np.linspace(0, spot * 4, 4001)
    pnl = _payoff(legs, shares, spot, wide)
    # Beyond the highest strike the payoff is a straight line; a positive slope there means unlimited profit.
    rising = pnl[-1] - pnl[-2] > 1e-9
    crossings = [float(wide[i] - pnl[i] * (wide[i + 1] - wide[i]) / (pnl[i + 1] - pnl[i]))
                 for i in range(len(wide) - 1) if pnl[i] == 0 or pnl[i] * pnl[i + 1] < 0]
    grid = np.linspace(spot * 0.75, spot * 1.25, 61)
    option_cost = sum((1 if leg["action"] == "buy" else -1) * leg["premium"] for leg in legs) * CONTRACT
    warnings = []
    if any(not leg["quoted"] for leg in legs):
        warnings.append("At least one leg has no live quote, so prices are last trades and may be stale.")
    if any(leg["open_interest"] < 100 for leg in legs):
        warnings.append("At least one leg is thinly traded and may be hard to exit at a fair price.")
    return {
        "name": name, "outlook": outlook, "legs": legs, "shares": shares, "description": what, "risk": risk_note,
        "option_cost": option_cost, "capital": option_cost + shares * spot,
        "max_profit": None if rising else float(pnl.max()), "max_loss": float(-pnl.min()),
        "breakevens": [round(x, 2) for x in crossings][:2],
        "payoff": [{"price": float(p), "pnl": float(v)} for p, v in zip(grid, _payoff(legs, shares, spot, grid))],
        "warnings": warnings,
    }


def option_strategies(symbol: str) -> dict:
    symbol = market.normalize_symbol(symbol)
    expiry, dte, reason = _expiry(symbol)
    if not expiry:
        return {"available": False, "reason": reason}
    res = opt.analyze_chain(market.get_chain(symbol, expiry), settings.risk_free(market.currency_of(symbol)))
    spot, calls, puts = res["summary"]["spot"], res["calls"], res["puts"]
    sig = signals.compute_signal(symbol, use_ml=False, use_news=False)

    out = []
    c65, c30 = _pick(calls, 0.65), None
    if c65:
        out.append(_build("Long call", "bullish", [_leg("buy", "call", c65)], 0, spot,
                          "Buy one call. Profits if the stock rises well above the strike before expiry.",
                          "The whole premium is lost if the stock is below the strike at expiry. Time works against you every day."))
        c30 = _pick([c for c in calls if c["strike"] > c65["strike"]], 0.30)
        if c30:
            out.append(_build("Bull call spread", "bullish", [_leg("buy", "call", c65), _leg("sell", "call", c30)], 0, spot,
                              "Buy a call and sell a higher-strike call against it. Cheaper than the call alone, with a lower "
                              "breakeven, in exchange for capping the profit at the higher strike.",
                              "Loss is limited to what you paid; profit stops growing above the higher strike."))
    p65 = _pick(puts, -0.65)
    if p65:
        out.append(_build("Long put", "bearish", [_leg("buy", "put", p65)], 0, spot,
                          "Buy one put. Profits if the stock falls well below the strike before expiry. The simplest way "
                          "to act on a bearish view without short selling.",
                          "The whole premium is lost if the stock is above the strike at expiry."))
        p30 = _pick([p for p in puts if p["strike"] < p65["strike"]], -0.30)
        if p30:
            out.append(_build("Bear put spread", "bearish", [_leg("buy", "put", p65), _leg("sell", "put", p30)], 0, spot,
                              "Buy a put and sell a lower-strike put against it. Cheaper than the put alone, with the profit "
                              "capped at the lower strike.",
                              "Loss is limited to what you paid; profit stops growing below the lower strike."))
    c_otm = _pick([c for c in calls if c["strike"] > spot], 0.30)
    if c_otm:
        out.append(_build("Covered call", "own the shares", [_leg("sell", "call", c_otm)], CONTRACT, spot,
                          "Own 100 shares and sell a call above the price. You keep the premium as income; in return you "
                          "must sell the shares at the strike if the stock rises past it.",
                          "You still carry nearly all of the shares' downside, and give up gains above the strike."))
    p_otm = _pick([p for p in puts if p["strike"] < spot], -0.30)
    if p_otm:
        out.append(_build("Protective put", "own the shares", [_leg("buy", "put", p_otm)], CONTRACT, spot,
                          "Own 100 shares and buy a put below the price. Insurance: your loss stops at the strike until expiry.",
                          "The premium is a cost that drags on returns if the stock does not fall."))
    if not out:
        return {"available": False, "reason": f"No contracts for {expiry} have usable prices right now. Option quotes are "
                "often blank outside US market hours."}
    want = "bullish" if sig["score"] >= 0.2 else "bearish" if sig["score"] <= -0.2 else None
    suggested = next((s["name"] for s in out if s["outlook"] == want), None)
    note = (f"The price signal on {symbol} is {sig['label']}, which fits the {want} strategies." if want else
            f"The price signal on {symbol} is Hold: there is no directional edge to express. The strategies for shares "
            "you already own are the relevant ones.")
    return {"available": True, "symbol": symbol, "expiry": expiry, "days_to_expiry": dte, "spot": spot,
            "signal": sig["label"], "suggested": suggested, "note": note, "strategies": out}
