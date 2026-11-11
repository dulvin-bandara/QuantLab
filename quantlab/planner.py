"""Turning an idea into a plan: how and when to buy, an option alternative, and a portfolio.

Three tools, all rule-based and transparent rather than predictive:

* ``buy_guide`` – the minimum money needed, the order types available with
  suggested prices, and which one suits the market session at a given time.
* ``suggest_option`` – one call option that fits a swing trade in the stock.
* ``build_portfolio`` – a whole-share portfolio for a given amount, period and
  target return, with an honest estimate of how likely the target is.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import norm

from . import brokers, ideas, signals
from .analytics import indicators as ind
from .analytics import options as opt
from .analytics import risk
from .config import settings
from .data import market

# Long-run extra return assumed for holding the share market instead of cash. A planning
# assumption, deliberately modest; it is not derived from the recent run of any stock.
EQUITY_PREMIUM = 0.05
CORE_FUND = {"US": "IVV", "ASX": "VAS.AX"}       # broad index funds used as the portfolio core
CORE_SHARE = 0.40                                 # of the invested part
MAX_IDEAS = 6
ASX_MIN_FIRST_PARCEL = 500.0                      # ASX rule: a first purchase of a stock must be at least A$500

SESSIONS = {
    "US": {"tz": "America/New_York", "pre": time(4, 0), "open": time(9, 30), "close": time(16, 0),
           "post": time(20, 0), "name": "US market"},
    "ASX": {"tz": "Australia/Sydney", "pre": time(7, 0), "open": time(10, 0), "close": time(16, 0),
            "post": time(16, 10), "name": "ASX"},
}


def market_of(symbol: str) -> str:
    return "ASX" if market.is_asx(symbol) else "US"


def session_info(symbol: str, when: datetime | None = None) -> dict:
    """Where the stock's exchange is in its trading day at ``when`` (default: now).

    Public holidays are not known to the app, so a holiday looks like a normal day.
    """
    cfg = SESSIONS[market_of(symbol)]
    tz = ZoneInfo(cfg["tz"])
    when = (when or datetime.now(timezone.utc)).astimezone(tz)
    t, weekday = when.time(), when.weekday() < 5
    minutes = lambda a, b: (datetime.combine(when.date(), b) - datetime.combine(when.date(), a)).total_seconds() / 60

    if not weekday or t < cfg["pre"] or t >= cfg["post"]:
        phase = "closed"
    elif t < cfg["open"]:
        phase = "pre-market"
    elif t >= cfg["close"]:
        phase = "after-hours" if market_of(symbol) == "US" else "closing auction"
    elif minutes(cfg["open"], t) < 30:
        phase = "opening"
    elif minutes(t, cfg["close"]) <= 15:
        phase = "closing"
    else:
        phase = "open"

    nxt = when
    if not (weekday and t < cfg["open"]):
        nxt = when + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    next_open = datetime.combine(nxt.date(), cfg["open"], tz)
    return {
        "market": cfg["name"], "phase": phase, "is_open": phase in ("opening", "open", "closing"),
        "exchange_time": when.strftime("%a %d %b %H:%M"), "timezone": cfg["tz"],
        "next_open": None if phase in ("opening", "open", "closing") else next_open.astimezone(timezone.utc).isoformat(),
        "hours_from_now": (when - datetime.now(tz)).total_seconds() / 3600,
    }


def minimum_equity(symbol: str, price: float, atr: float, option_cost: float | None) -> dict:
    """The least money needed to buy the stock at all, and to buy it within the 1% risk rule."""
    asx = market.is_asx(symbol)
    shares = max(1, math.ceil(ASX_MIN_FIRST_PARCEL / price)) if asx else 1
    return {
        "currency": market.currency_of(symbol),
        "one_parcel": shares * price,
        "one_parcel_shares": shares,
        "one_parcel_note": (f"The ASX requires a first purchase of at least A${ASX_MIN_FIRST_PARCEL:,.0f}, so the smallest "
                            f"parcel is {shares} shares." if asx else
                            "One share. Some brokers also sell fractions of a US share for less."),
        # With a stop 2 ATR away, the smallest parcel risks shares * 2 * ATR; that should be at most 1% of the account.
        "for_risk_rule": shares * 2 * atr / 0.01,
        "option_contract": option_cost,
    }


def suggest_option(symbol: str, plan: dict | None = None) -> dict:
    """One call option suited to a swing trade: about 45 days to expiry, delta near 0.65.

    A little in the money and a few weeks beyond the planned holding time, so it moves most of
    the way with the stock and loses less to time decay than a cheaper, shorter contract.
    """
    symbol = market.normalize_symbol(symbol)
    try:
        expiries = market.get_expiries(symbol)
    except market.DataError:
        expiries = []
    if not expiries:
        return {"available": False, "reason": "No option chain is available for this stock from the free data source"
                + (" (ASX chains are not published there)." if market.is_asx(symbol) else ".")}
    today = datetime.now(timezone.utc).date()
    dte = {e: (datetime.strptime(e, "%Y-%m-%d").date() - today).days for e in expiries}
    in_range = [e for e in expiries if 21 <= dte[e] <= 90]
    if not in_range:
        return {"available": False, "reason": "No expiry between 3 weeks and 3 months out is listed."}
    expiry = min(in_range, key=lambda e: abs(dte[e] - 45))
    res = opt.analyze_chain(market.get_chain(symbol, expiry), settings.risk_free(market.currency_of(symbol)))
    spot = res["summary"]["spot"]

    def premium(c):
        return c["ask"] if c["quoted"] else c["last"]

    usable = [c for c in res["calls"] if c.get("delta") and 0.45 <= c["delta"] <= 0.85 and premium(c) > 0]
    if not usable:
        return {"available": False, "reason": f"No call for {expiry} has a usable price right now. "
                "Option quotes are often blank outside US market hours."}
    quoted = [c for c in usable if c["quoted"]]
    c = min(quoted or usable, key=lambda c: abs(c["delta"] - 0.65))
    px, strike = premium(c), c["strike"]
    out = {
        "available": True, "contract": f"{symbol} {expiry} {strike:g} call", "expiry": expiry, "days_to_expiry": dte[expiry],
        "strike": strike, "premium": px, "price_basis": "ask" if c["quoted"] else "last trade (no live quote)",
        "cost": px * 100, "max_loss": px * 100, "breakeven": strike + px, "breakeven_move": (strike + px) / spot - 1,
        "delta": c["delta"], "share_equivalent": c["delta"] * 100, "iv": c["iv"], "theta_per_day": (c.get("theta") or 0) * 100,
        "open_interest": c["open_interest"], "leverage": c["delta"] * spot / px,
        "spread_pct": (c["ask"] - c["bid"]) / ((c["ask"] + c["bid"]) / 2) if c["quoted"] else None,
        "expected_move_pct": res["summary"].get("expected_move_pct"), "warnings": [],
    }
    if plan:
        out["profit_at_target"] = (max(plan["target"] - strike, 0) - px) * 100
        out["loss_at_stop"] = (max(plan["stop"] - strike, 0) - px) * 100
        if strike + px > plan["target"]:
            out["warnings"].append("Breakeven is above the stock's price target, so the stock must beat its target for "
                                   "this call to profit at expiry.")
    days = market.get_fundamentals(symbol).get("days_to_earnings")
    if days is not None and 0 <= days <= dte[expiry]:
        out["warnings"].append(f"Earnings fall inside this option's life (in {days} days). Options are priced higher "
                               "ahead of results and usually lose value quickly afterwards, even if the stock moves your way.")
    if not c["quoted"]:
        out["warnings"].append("No live bid/ask right now, so the premium is the last trade and may be stale. Recheck "
                               "during US market hours.")
    if c["open_interest"] < 100:
        out["warnings"].append("Low open interest: this contract may be hard to sell at a fair price.")
    if out["spread_pct"] is not None and out["spread_pct"] > 0.10:
        out["warnings"].append(f"Wide bid/ask spread ({out['spread_pct']:.0%}): use a limit order near the midpoint.")
    return out


def buy_guide(symbol: str, when: datetime | None = None) -> dict:
    """Everything needed to act on a stock: minimum money, order choices and an option alternative."""
    symbol = market.normalize_symbol(symbol)
    df = market.get_history(symbol)
    sig = signals.compute_signal(symbol)
    price, cur = sig["price"], sig["currency"]
    # Always a plan for buying (stop below, target above), whatever the signal currently says.
    plan = signals.trade_plan(df, 1.0, settings.paper_start_cash)
    atr = plan["atr"]
    close = df["Close"]
    rsi = float(ind.rsi(close).iloc[-1])
    ema20 = float(ind.ema(close, 20).iloc[-1])
    high20 = float(df["High"].tail(20).max())
    overbought = rsi >= 70
    session = session_info(symbol, when)
    fund = market.get_fundamentals(symbol)
    phase, far = session["phase"], session["hours_from_now"] > 20
    option = suggest_option(symbol, plan)

    pullback = min(max(ema20, price - 1.5 * atr), price - 0.25 * atr)
    breakout = max(high20, price) + 0.1 * atr
    orders = [
        {"type": "Market order", "price": None,
         "what": "Buys immediately at the best price on offer. You are certain to get the shares but not the price.",
         "use_when": "The market is open, the stock is heavily traded, and getting in matters more than the exact price."},
        {"type": "Limit order at the current price", "price": price * 1.002,
         "what": "Buys only at this price or lower. A limit a touch above the last price usually fills at once but caps what you pay.",
         "use_when": "Almost always a safer substitute for a market order, and the right choice when the market is closed."},
        {"type": "Limit order on a pullback", "price": pullback,
         "what": "Waits for a dip toward the 20-day average before buying. You get a better price, or no trade if the stock keeps rising.",
         "use_when": "The stock has run up fast (overbought), or you are not in a hurry."},
        {"type": "Buy-stop order on a breakout", "price": breakout,
         "what": "Buys only if the price rises through the recent 20-day high, confirming the uptrend is continuing. "
                 "Add a limit a little above it (a stop-limit) to cap the fill price.",
         "use_when": "You want proof of strength first, or will not be watching the market."},
    ]

    if phase in ("closed", "pre-market", "after-hours", "closing auction"):
        pick = 2 if overbought else 1
        why = (f"The {session['market']} is {phase} at that time. A market order placed now would fill at the next open, "
               "wherever the price happens to open, so set the price yourself with a limit order that is good for the day.")
    elif phase == "opening":
        pick = 2 if overbought else 1
        why = ("The first half hour after the open has the widest spreads and sharpest swings of the day. Use a limit "
               "order, or wait about 30 minutes for prices to settle.")
    elif phase == "closing":
        pick = 1
        why = "The session is about to close. Use a limit order so a late swing cannot fill you at a poor price, or wait for tomorrow."
    else:
        pick = 2 if overbought else 1
        why = ("The market is open and past the volatile opening period. A limit order just above the last price gets you "
               "in promptly while capping the price; a plain market order is also reasonable for a stock this liquid.")
    if overbought:
        why += f" The stock is overbought (RSI {rsi:.0f}), so waiting for a pullback is preferred over chasing it."
    if far:
        pick = 3 if not overbought else 2
        why = (f"That time is {session['hours_from_now'] / 24:.1f} days away and the price will have moved by then. Rather than "
               "deciding on the day, place a resting order now that only triggers on your terms (good till cancelled), and "
               "re-run the scan before then to check the idea still holds.")
    for i, o in enumerate(orders):
        o["recommended"] = i == pick

    return {
        "symbol": symbol, "price": price, "currency": cur, "as_of": sig["as_of"], "signal": sig["label"], "score": sig["score"],
        "rsi": rsi, "session": session, "orders": orders, "recommendation": why,
        "after_entry": {"stop_loss": plan["stop"], "take_profit": plan["target"],
                        "note": "Once filled, place a stop-loss sell order and a limit sell at the target. Many brokers "
                                "accept both together as a bracket (one-cancels-the-other) order."},
        "minimum_equity": minimum_equity(symbol, price, atr, option["cost"] if option.get("available") else None),
        "option": option,
        # Which of the owner's brokers is cheapest for the suggested position (it has a stop and a target).
        "broker": brokers.recommend(symbol, "buy", plan["position_value"], needs_conditional=True),
        "option_broker": brokers.recommend(symbol, "buy", option["cost"], "option", 1) if option.get("available") else None,
        "fundamentals": fund,
        "earnings_warning": None if fund.get("days_to_earnings") is None or fund["days_to_earnings"] > 14 else
                            f"Earnings are due on {fund['next_earnings']} ({fund['days_to_earnings']} days away). Results can "
                            "gap the price through a stop, so many swing traders wait until after the report or buy a "
                            "smaller position.",
        "signal_warning": None if sig["score"] >= 0.2 else
                          f"The app's signal on {symbol} is currently {sig['label']}, not Buy. This guide covers how to "
                          "buy it, not whether to.",
        "caveats": ["Public holidays and half-days are not known to the app.",
                    "Prices are delayed; check your broker's live quote before placing any order.",
                    "You can practise these order types with simulated money on the Paper trading screen."],
    }


def build_portfolio(equity: float, months: float, target_return: float, market_name: str = "US",
                    use_holdings: bool = True) -> dict:
    """A whole-share portfolio for ``equity`` aiming at ``target_return`` (decimal) over ``months``.

    Structure: a broad index fund as the core, the top current ideas around it, and cash. How
    much is invested rather than held in cash is set by the target. Expected returns come from
    each holding's market sensitivity (beta) times a fixed long-run equity premium, not from
    extrapolating recent gains.
    """
    market_name = market_name.upper()
    if market_name not in CORE_FUND:
        raise ValueError("Market must be US or ASX.")
    if not (equity > 0 and 0.5 <= months <= 60 and -0.5 <= target_return <= 5):
        raise ValueError("Enter a positive amount, a period of 1 to 60 months and a sensible target return.")
    cur = "AUD" if market_name == "ASX" else "USD"
    rf = settings.risk_free(cur)
    years = months / 12
    scan = ideas.latest()
    picks = [i for i in (scan["ideas"] if scan else []) if i["market"] == market_name][:MAX_IDEAS]
    notes = []
    if not scan:
        notes.append("No ideas scan has been run yet, so this portfolio holds only the index fund and cash. Run a scan on the Ideas screen first.")
    elif not picks:
        notes.append(f"The latest scan has no {market_name} ideas rated Buy, so this portfolio holds only the index fund and cash.")

    core = CORE_FUND[market_name]
    symbols = [core] + [p["symbol"] for p in picks]
    closes, kept = {}, []
    for s in symbols:
        try:
            closes[s] = market.get_history(s, years=2)["Close"]
            kept.append(s)
        except market.DataError:
            notes.append(f"{s} was left out because its prices could not be loaded.")
    if not kept:
        raise ValueError("Prices could not be loaded for any holding.")
    rets = pd.DataFrame({s: closes[s].pct_change() for s in kept}).dropna()
    bench = market.get_history(market.benchmark_of(core), years=2)["Close"].pct_change()
    betas = {s: risk.beta(rets[s], bench) for s in kept}
    betas = {s: (1.0 if np.isnan(b) else b) for s, b in betas.items()}
    expected = {s: rf + betas[s] * EQUITY_PREMIUM for s in kept}

    # Weights inside the invested part: index fund core, ideas sized so calmer stocks get more.
    idea_syms = [s for s in kept if s != core]
    w = {}
    if core in kept:
        w[core] = CORE_SHARE if idea_syms else 1.0
    if idea_syms:
        inv_vol = {s: 1 / max(rets[s].std(), 1e-6) for s in idea_syms}
        total = sum(inv_vol.values())
        budget = 1 - w.get(core, 0.0)
        for s in idea_syms:
            w[s] = budget * inv_vol[s] / total

    # How much to invest: enough that the expected return matches the target, never more than everything.
    sleeve_expected = sum(w[s] * expected[s] for s in w)
    required = (1 + target_return) ** (1 / years) - 1
    invest_share = float(np.clip((required - rf) / max(sleeve_expected - rf, 1e-6), 0.0, 1.0))

    prices = {s: float(closes[s].iloc[-1]) for s in kept}
    plans = {p["symbol"]: p for p in picks}
    positions, invested = [], 0.0
    for s in w:
        shares = math.floor(equity * invest_share * w[s] / prices[s])
        if shares < 1:
            if invest_share > 0:
                notes.append(f"{s} was left out: one share ({prices[s]:,.2f} {cur}) is more than its slice of this portfolio.")
            continue
        value = shares * prices[s]
        invested += value
        p = plans.get(s)
        positions.append({"symbol": s, "name": market.get_name(s), "role": "Core index fund" if s == core else "Idea",
                          "shares": shares, "price": prices[s], "value": value, "beta": betas[s],
                          "stop": p["plan"]["stop"] if p else None, "signal": p["label"] if p else None})
    # Whole shares leave money over. Spend it on whichever holding is furthest below its intended
    # weight, so the amount invested ends up close to what the target calls for.
    budget = equity * invest_share
    while positions:
        short = [(budget * w[p["symbol"]] - p["value"], p) for p in positions if p["price"] <= budget - invested]
        if not short:
            break
        _, p = max(short, key=lambda x: x[0])
        p["shares"] += 1
        p["value"] += p["price"]
        invested += p["price"]
    cash = equity - invested
    from .tracking import held_shares

    owned = held_shares() if use_holdings else {}
    for p in positions:
        p["weight"] = p["value"] / equity
        # Shares you already hold (imported holdings) count toward the position.
        p["held"] = min(owned.get(p["symbol"], 0), p["shares"])
        p["to_buy"] = p["shares"] - p["held"]
        rec = brokers.recommend(p["symbol"], "buy", p["to_buy"] * p["price"]) if p["to_buy"] else {"available": False}
        p["broker"] = rec["recommended"] if rec["available"] else None
        p["fee"] = next(o["total"] for o in rec["options"] if o["broker"] == rec["recommended"]) if rec["available"] else None
    if any(p["held"] for p in positions):
        notes.append("You already own some of these shares; the 'To buy' column shows only what is missing.")
    weights = pd.Series({p["symbol"]: p["weight"] for p in positions}, dtype=float)

    cash_w = cash / equity
    exp_ann = cash_w * rf + sum(weights[s] * expected[s] for s in weights.index)
    if len(weights):
        port = rets[weights.index] @ weights          # cash contributes no day-to-day movement
        vol_ann = float(port.std(ddof=1) * np.sqrt(252))
        max_dd = risk.max_drawdown((1 + port).cumprod())
        beta = float(sum(weights[s] * betas[s] for s in weights.index))
    else:
        vol_ann, max_dd, beta = 0.0, 0.0, 0.0

    exp_period = (1 + exp_ann) ** years - 1
    sd = vol_ann * math.sqrt(years)
    prob = float(1 - norm.cdf((target_return - exp_period) / sd)) if sd > 0 else float(exp_period >= target_return)
    if required <= rf:
        verdict = (f"That target is at or below what cash is assumed to earn ({rf:.0%} a year), so little or no share-market "
                   "risk is needed to aim for it.")
    elif prob >= 0.45:
        verdict = "This target is in line with what a portfolio like this can reasonably be expected to return."
    elif prob >= 0.2:
        verdict = ("This target is ambitious. It needs a better-than-average period for shares; the portfolio is fully or "
                   "nearly fully invested and the more likely outcome is below the target.")
    else:
        verdict = ("This target is unlikely to be met without taking far more risk than a diversified share portfolio. "
                   f"Annualised, it asks for {required:.0%} a year; treat the portfolio below as the sensible limit, not a route to the target.")
    risk_at_stops = sum(p["shares"] * (p["price"] - p["stop"]) for p in positions if p["stop"])
    return {
        "inputs": {"equity": equity, "months": months, "target_return": target_return, "market": market_name, "currency": cur},
        "required_annual_return": required, "invest_share": invested / equity, "cash": cash, "cash_weight": cash_w,
        "positions": positions, "verdict": verdict, "probability_of_target": prob,
        "expected_return": exp_period, "expected_value": equity * (1 + exp_period),
        "range_low": exp_period - 1.645 * sd, "range_high": exp_period + 1.645 * sd,
        "annual_volatility": vol_ann, "worst_drop_last_2y": max_dd, "beta": beta,
        "loss_if_all_stops_hit": risk_at_stops,
        "total_fees": sum(p["fee"] for p in positions if p["fee"]) if any(p["fee"] is not None for p in positions) else None,
        "assumptions": {"cash_rate": rf, "equity_premium": EQUITY_PREMIUM,
                        "scan_date": scan["scanned_at"] if scan else None},
        "notes": notes,
    }
