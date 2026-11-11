"""Your trading providers (brokers): what each charges and which is cheapest for a given trade.

You enter each broker's fees yourself, from its current price list: the app does
not know any broker's real charges and they change often. With those in place it
can estimate the cost of a trade at each broker, rule out brokers that cannot do
it, and recommend one.

Cost model for a share trade:  max(minimum, fixed fee + percentage of trade value),
plus a currency-conversion percentage when the stock trades in a different
currency from the one the account is funded in. Options cost a fee per contract,
with an optional minimum.
"""
from __future__ import annotations

import json

from .data import market
from .db import connect

FIELDS = {
    # name: (default, description)
    "base_currency": ("AUD", "Currency you fund the account in"),
    "us_stocks": (True, "Can trade US shares"),
    "asx_stocks": (True, "Can trade ASX shares"),
    "us_options": (False, "Can trade US options"),
    "fractional": (False, "Allows fractions of a share"),
    "conditional_orders": (True, "Supports stop and bracket orders"),
    "us_fixed": (0.0, "US share trade: fixed fee (USD)"),
    "us_pct": (0.0, "US share trade: % of trade value"),
    "us_min": (0.0, "US share trade: minimum fee (USD)"),
    "asx_fixed": (0.0, "ASX share trade: fixed fee (AUD)"),
    "asx_pct": (0.0, "ASX share trade: % of trade value"),
    "asx_min": (0.0, "ASX share trade: minimum fee (AUD)"),
    "option_per_contract": (0.0, "Option trade: fee per contract (USD)"),
    "option_min": (0.0, "Option trade: minimum fee (USD)"),
    "fx_pct": (0.0, "Currency conversion: % of amount converted"),
}


class BrokerError(Exception):
    pass


def _clean(data: dict) -> dict:
    out = {}
    for key, (default, _) in FIELDS.items():
        v = data.get(key, default)
        if isinstance(default, bool):
            out[key] = bool(v)
        elif isinstance(default, float):
            try:
                out[key] = float(v or 0)
            except (TypeError, ValueError):
                raise BrokerError(f"'{key}' must be a number.")
            if out[key] < 0:
                raise BrokerError("Fees cannot be negative.")
        else:
            out[key] = str(v).upper()
    if out["base_currency"] not in ("AUD", "USD"):
        raise BrokerError("Account currency must be AUD or USD.")
    return out


def save_broker(name: str, data: dict) -> dict:
    name = (name or "").strip()[:40]
    if not name:
        raise BrokerError("Give the broker a name.")
    cleaned = _clean(data)
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO brokers (name, data) VALUES (?, ?)", (name, json.dumps(cleaned)))
    return {"name": name, **cleaned}


def list_brokers() -> list[dict]:
    with connect() as conn:
        return [{"name": r["name"], **json.loads(r["data"])} for r in conn.execute("SELECT * FROM brokers ORDER BY name")]


def delete_broker(name: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM brokers WHERE name=?", (name,)).rowcount > 0


def trade_cost(broker: dict, symbol: str, value: float = 0.0, instrument: str = "stock", contracts: int = 0) -> dict | None:
    """Estimated cost of one trade at this broker, in the stock's currency. None if it cannot do the trade."""
    asx = market.is_asx(symbol)
    currency = market.currency_of(symbol)
    if instrument == "option":
        if asx or not broker["us_options"]:
            return None
        commission = max(broker["option_min"], broker["option_per_contract"] * contracts)
    elif asx:
        if not broker["asx_stocks"]:
            return None
        commission = max(broker["asx_min"], broker["asx_fixed"] + broker["asx_pct"] / 100 * value)
    else:
        if not broker["us_stocks"]:
            return None
        commission = max(broker["us_min"], broker["us_fixed"] + broker["us_pct"] / 100 * value)
    fx = broker["fx_pct"] / 100 * value if currency != broker["base_currency"] else 0.0
    total = commission + fx
    return {"broker": broker["name"], "commission": commission, "fx": fx, "total": total, "currency": currency,
            "pct_of_trade": total / value if value else None}


def recommend(symbol: str, action: str = "buy", value: float = 0.0, instrument: str = "stock", contracts: int = 1,
              needs_conditional: bool = False) -> dict:
    """Rank your brokers for one trade and pick the cheapest that can do it.

    ``action`` is "buy" or "sell". Selling shares can only happen where they are held, so for a
    sale only brokers holding the stock are considered. ``value`` is the trade size in the
    stock's currency (for options, the total premium).
    """
    symbol = market.normalize_symbol(symbol)
    brokers = list_brokers()
    if not brokers:
        return {"available": False, "reason": "No brokers have been added yet. Add yours on the My portfolio screen."}
    with connect() as conn:
        held = {r["broker"]: r["qty"] for r in conn.execute("SELECT broker, qty FROM holdings WHERE symbol=?", (symbol,))}
    options, excluded = [], []
    for b in brokers:
        cost = trade_cost(b, symbol, value, instrument, contracts)
        what = "US options" if instrument == "option" else ("ASX shares" if market.is_asx(symbol) else "US shares")
        if cost is None:
            excluded.append(f"{b['name']} does not trade {what}.")
            continue
        if action == "sell" and instrument == "stock" and held and b["name"] not in held:
            excluded.append(f"{b['name']} does not hold your {symbol} shares.")
            continue
        notes = []
        if b["name"] in held:
            notes.append(f"You hold {held[b['name']]:g} {symbol} here.")
        if needs_conditional and not b["conditional_orders"]:
            notes.append("No stop or bracket orders: you would have to watch the exit yourself.")
        if cost["fx"]:
            notes.append(f"Includes {cost['fx']:,.2f} {cost['currency']} of currency conversion.")
        options.append({**cost, "conditional_orders": b["conditional_orders"], "notes": notes,
                        # A round trip (buy then sell) pays the commission and any conversion twice.
                        "round_trip": cost["total"] * 2})
    if not options:
        return {"available": False, "reason": "None of your brokers can do this trade. " + " ".join(excluded)}
    # Cheapest first; among near-equal costs prefer one that supports the exit orders the plan needs.
    options.sort(key=lambda o: (o["total"], needs_conditional and not o["conditional_orders"]))
    best = options[0]
    if needs_conditional and not best["conditional_orders"]:
        alt = next((o for o in options if o["conditional_orders"] and o["total"] <= best["total"] * 1.5 + 1), None)
        best = alt or best
    saving = max(o["total"] for o in options) - best["total"]
    why = f"{best['broker']} is the cheapest of your brokers for this trade" if best is options[0] else \
          f"{best['broker']} costs a little more than the cheapest but supports the stop and bracket orders this plan uses"
    why += f": about {best['total']:,.2f} {best['currency']}"
    why += f" ({best['pct_of_trade']:.2%} of the trade)." if best["pct_of_trade"] else "."
    if len(options) > 1 and saving > 0.005:
        why += f" That is {saving:,.2f} less than the most expensive option."
    return {"available": True, "symbol": symbol, "action": action, "instrument": instrument, "value": value,
            "recommended": best["broker"], "why": why, "options": options, "excluded": excluded}
