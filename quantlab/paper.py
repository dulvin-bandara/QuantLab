"""Paper trading: a simulated account for stocks and options, one cash balance per currency.

Stock orders are market orders filled immediately at the latest available
price (Yahoo's delayed last/close) plus slippage and commission.

Option orders buy or sell-to-close calls and puts (100 shares per contract).
They fill at the ask (buys) or bid (sells) when the chain has a live quote and
at the last traded price otherwise. Expired contracts are settled
automatically at intrinsic value. Long positions only: writing options and
short selling are not simulated.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from .analytics import options as opt
from .analytics import risk
from .config import settings
from .data import market
from .db import connect


class OrderError(Exception):
    pass


def _ensure_cash(conn) -> None:
    for cur in ("USD", "AUD"):
        conn.execute("INSERT OR IGNORE INTO paper_cash (currency, amount, start) VALUES (?,?,?)",
                     (cur, settings.paper_start_cash, settings.paper_start_cash))


def place_order(symbol: str, side: str, qty: float, note: str = "") -> dict:
    symbol = market.normalize_symbol(symbol)
    side = side.lower()
    if side not in ("buy", "sell"):
        raise OrderError("Side must be 'buy' or 'sell'.")
    if not qty or qty <= 0 or int(qty) != qty:
        raise OrderError("Quantity must be a positive whole number of shares.")
    if symbol.startswith("^"):
        raise OrderError("Indexes cannot be traded directly; use an ETF instead.")
    last = market.get_quote(symbol)["price"]
    currency = market.currency_of(symbol)
    slip = settings.paper_slippage_bps / 1e4
    price = last * (1 + slip if side == "buy" else 1 - slip)
    commission = price * qty * settings.paper_commission_bps / 1e4

    with connect() as conn:
        _ensure_cash(conn)
        cash = conn.execute("SELECT amount FROM paper_cash WHERE currency=?", (currency,)).fetchone()["amount"]
        pos = conn.execute("SELECT qty, avg_cost FROM paper_positions WHERE symbol=?", (symbol,)).fetchone()
        held, avg = (pos["qty"], pos["avg_cost"]) if pos else (0.0, 0.0)
        realized = None
        if side == "buy":
            cost = price * qty + commission
            if cost > cash + 1e-9:
                raise OrderError(f"Not enough {currency} cash: order costs {cost:,.2f}, {cash:,.2f} available.")
            new_qty = held + qty
            new_avg = (held * avg + price * qty) / new_qty
            cash -= cost
        else:
            if qty > held + 1e-9:
                raise OrderError(f"Cannot sell {qty:g} {symbol}: only {held:g} held (short selling is not supported).")
            new_qty, new_avg = held - qty, avg
            realized = (price - avg) * qty - commission
            cash += price * qty - commission
        conn.execute("UPDATE paper_cash SET amount=? WHERE currency=?", (cash, currency))
        if new_qty > 1e-9:
            conn.execute("INSERT OR REPLACE INTO paper_positions (symbol, qty, avg_cost, currency) VALUES (?,?,?,?)",
                         (symbol, new_qty, new_avg, currency))
        else:
            conn.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("INSERT INTO paper_trades (ts, symbol, side, qty, price, commission, currency, realized, note) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (ts, symbol, side, qty, price, commission, currency, realized, note[:200]))
    return {"symbol": symbol, "side": side, "qty": qty, "price": price, "commission": commission,
            "currency": currency, "realized": realized, "ts": ts}


# ---------------------------------------------------------------- options

MULTIPLIER = 100


def contract_id(symbol: str, expiry: str, strike: float, kind: str) -> str:
    return f"{symbol}|{expiry}|{strike:g}|{kind}"


def contract_label(symbol: str, expiry: str, strike: float, kind: str) -> str:
    return f"{symbol} {expiry} {strike:g} {kind}"


def _intrinsic(spot: float, strike: float, kind: str) -> float:
    return max(spot - strike, 0.0) if kind == "call" else max(strike - spot, 0.0)


def option_mark(symbol: str, expiry: str, strike: float, kind: str) -> dict:
    """Current value of one contract (per share) and where that value came from."""
    spot = market.get_quote(symbol)["price"]
    out = {"spot": spot, "bid": 0.0, "ask": 0.0, "last": 0.0, "iv": None, "delta": None,
           "mark": _intrinsic(spot, strike, kind), "source": "intrinsic value (no market price available)"}
    try:
        chain = market.get_chain(symbol, expiry)
    except market.DataError:
        return out
    df = chain.calls if kind == "call" else chain.puts
    rows = df[(df["strike"] - strike).abs() < 1e-6] if "strike" in df else df.iloc[0:0]
    if rows.empty:
        return out
    rec = rows.iloc[0].to_dict()
    bid, ask, last = opt._num(rec.get("bid")), opt._num(rec.get("ask")), opt._num(rec.get("lastPrice"))
    out.update(bid=bid, ask=ask, last=last)
    if bid > 0 and ask > 0:
        out.update(mark=(bid + ask) / 2, source="live quote")
    elif last > 0:
        out.update(mark=last, source="last trade (no live quote)")
    else:
        return out
    T = opt.years_to_expiry(expiry)
    r = settings.risk_free(market.currency_of(symbol))
    iv = opt.implied_vol(out["mark"], spot, strike, T, r, kind)
    if iv == iv:  # not NaN
        out.update(iv=iv, delta=opt.bs_greeks(spot, strike, T, r, iv, kind)["delta"])
    return out


def place_option_order(symbol: str, expiry: str, strike: float, kind: str, side: str, contracts: float,
                       note: str = "") -> dict:
    symbol = market.normalize_symbol(symbol)
    side, kind = side.lower(), kind.lower()
    if side not in ("buy", "sell"):
        raise OrderError("Side must be 'buy' or 'sell'.")
    if kind not in ("call", "put"):
        raise OrderError("Type must be 'call' or 'put'.")
    if not contracts or contracts <= 0 or int(contracts) != contracts:
        raise OrderError("Quantity must be a positive whole number of contracts.")
    if expiry not in market.get_expiries(symbol):
        raise OrderError(f"{symbol} has no listed option expiry {expiry}.")
    m = option_mark(symbol, expiry, strike, kind)
    quoted = m["bid"] > 0 and m["ask"] > 0
    price = (m["ask"] if side == "buy" else m["bid"]) if quoted else m["last"]
    if price <= 0:
        raise OrderError("That contract has no bid/ask or last traded price right now, so it cannot be filled.")
    currency = market.currency_of(symbol)
    commission = settings.paper_option_fee * contracts
    cid, label = contract_id(symbol, expiry, strike, kind), contract_label(symbol, expiry, strike, kind)

    with connect() as conn:
        _ensure_cash(conn)
        cash = conn.execute("SELECT amount FROM paper_cash WHERE currency=?", (currency,)).fetchone()["amount"]
        pos = conn.execute("SELECT qty, avg_cost FROM paper_options WHERE contract=?", (cid,)).fetchone()
        held, avg = (pos["qty"], pos["avg_cost"]) if pos else (0.0, 0.0)
        realized = None
        if side == "buy":
            cost = price * contracts * MULTIPLIER + commission
            if cost > cash + 1e-9:
                raise OrderError(f"Not enough {currency} cash: order costs {cost:,.2f}, {cash:,.2f} available.")
            new_qty = held + contracts
            new_avg = (held * avg + price * contracts) / new_qty
            cash -= cost
        else:
            if contracts > held + 1e-9:
                raise OrderError(f"Cannot sell {contracts:g} {label}: only {held:g} held "
                                 "(writing options is not supported).")
            new_qty, new_avg = held - contracts, avg
            realized = (price - avg) * contracts * MULTIPLIER - commission
            cash += price * contracts * MULTIPLIER - commission
        conn.execute("UPDATE paper_cash SET amount=? WHERE currency=?", (cash, currency))
        if new_qty > 1e-9:
            conn.execute("INSERT OR REPLACE INTO paper_options (contract, symbol, expiry, strike, kind, qty, avg_cost, "
                         "currency) VALUES (?,?,?,?,?,?,?,?)", (cid, symbol, expiry, strike, kind, new_qty, new_avg, currency))
        else:
            conn.execute("DELETE FROM paper_options WHERE contract=?", (cid,))
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("INSERT INTO paper_trades (ts, symbol, side, qty, price, commission, currency, realized, note) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (ts, label, side, contracts, price, commission, currency, realized, note[:200]))
    return {"contract": label, "side": side, "contracts": contracts, "price": price, "commission": commission,
            "currency": currency, "realized": realized, "ts": ts,
            "fill_source": "ask" if quoted and side == "buy" else "bid" if quoted else "last trade"}


def _settle_expired() -> None:
    """Close out contracts past expiry at intrinsic value, using the underlying's close on expiry day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as conn:
        expired = [dict(r) for r in conn.execute("SELECT * FROM paper_options WHERE expiry < ?", (today,))]
    for o in expired:
        try:
            close = market.get_history(o["symbol"])["Close"]
        except market.DataError:
            continue
        upto = close[close.index <= o["expiry"]]
        if upto.empty:
            continue
        value = _intrinsic(float(upto.iloc[-1]), o["strike"], o["kind"])
        realized = (value - o["avg_cost"]) * o["qty"] * MULTIPLIER
        with connect() as conn:
            conn.execute("UPDATE paper_cash SET amount = amount + ? WHERE currency=?",
                         (value * o["qty"] * MULTIPLIER, o["currency"]))
            conn.execute("DELETE FROM paper_options WHERE contract=?", (o["contract"],))
            conn.execute("INSERT INTO paper_trades (ts, symbol, side, qty, price, commission, currency, realized, note) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (o["expiry"] + "T21:00:00+00:00", contract_label(o["symbol"], o["expiry"], o["strike"], o["kind"]),
                          "expired", o["qty"], value, 0.0, o["currency"], realized, "Settled at intrinsic value"))


def _option_positions() -> list[dict]:
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM paper_options ORDER BY symbol, expiry, strike")]
    today = datetime.now(timezone.utc).date()
    for o in rows:
        m = option_mark(o["symbol"], o["expiry"], o["strike"], o["kind"])
        o["label"] = contract_label(o["symbol"], o["expiry"], o["strike"], o["kind"])
        o["mark"], o["mark_source"], o["iv"], o["spot"] = m["mark"], m["source"], m["iv"], m["spot"]
        o["value"] = m["mark"] * o["qty"] * MULTIPLIER
        o["pnl"] = (m["mark"] - o["avg_cost"]) * o["qty"] * MULTIPLIER
        o["pnl_pct"] = m["mark"] / o["avg_cost"] - 1 if o["avg_cost"] else 0.0
        # Delta in share terms: how many shares of stock the position currently behaves like.
        o["delta_shares"] = m["delta"] * o["qty"] * MULTIPLIER if m["delta"] is not None else None
        o["days_left"] = (datetime.strptime(o["expiry"], "%Y-%m-%d").date() - today).days
    return rows


# ---------------------------------------------------------------- account

def _beta(symbol: str) -> float | None:
    try:
        a = market.get_history(symbol, years=2)["Close"].pct_change()
        m = market.get_history(market.benchmark_of(symbol), years=2)["Close"].pct_change()
        b = risk.beta(a, m)
        return None if np.isnan(b) else b
    except market.DataError:
        return None


def account() -> dict:
    """Balances, positions and P&L per currency. Also records today's equity."""
    _settle_expired()
    with connect() as conn:
        _ensure_cash(conn)
        cash_rows = {r["currency"]: dict(r) for r in conn.execute("SELECT * FROM paper_cash")}
        positions = [dict(r) for r in conn.execute("SELECT * FROM paper_positions ORDER BY symbol")]
    for p in positions:
        try:
            p["price"] = market.get_quote(p["symbol"])["price"]
        except market.DataError:
            p["price"] = p["avg_cost"]
        p["value"] = p["price"] * p["qty"]
        p["pnl"] = (p["price"] - p["avg_cost"]) * p["qty"]
        p["pnl_pct"] = p["price"] / p["avg_cost"] - 1 if p["avg_cost"] else 0.0
        p["beta"] = _beta(p["symbol"])

    options = _option_positions()
    betas = {}
    for o in options:
        if o["symbol"] not in betas:
            betas[o["symbol"]] = _beta(o["symbol"])

    books = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as conn:
        for cur, row in cash_rows.items():
            mine = [p for p in positions if p["currency"] == cur]
            opts = [o for o in options if o["currency"] == cur]
            invested = sum(p["value"] for p in mine) + sum(o["value"] for o in opts)
            equity = row["amount"] + invested
            # Portfolio beta: each holding's beta weighted by its share of equity (cash has beta 0).
            # Options count at their delta-equivalent stock exposure.
            exposure = [(p["value"], p["beta"]) for p in mine]
            exposure += [(o["delta_shares"] * o["spot"], betas[o["symbol"]]) for o in opts if o["delta_shares"] is not None]
            pbeta = sum(v / equity * b for v, b in exposure if b is not None) if equity else 0.0
            conn.execute("INSERT OR REPLACE INTO paper_equity (date, currency, equity) VALUES (?,?,?)",
                         (today, cur, equity))
            history = [dict(r) for r in conn.execute(
                "SELECT date, equity FROM paper_equity WHERE currency=? ORDER BY date", (cur,))]
            books.append({"currency": cur, "cash": row["amount"], "invested": invested, "equity": equity,
                          "start": row["start"], "pnl": equity - row["start"],
                          "pnl_pct": equity / row["start"] - 1 if row["start"] else 0.0,
                          "beta": pbeta, "history": history})
    return {"books": books, "positions": positions, "options": options}


def trades(limit: int = 200) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,))]


def reset() -> None:
    with connect() as conn:
        for t in ("paper_cash", "paper_positions", "paper_options", "paper_trades", "paper_equity"):
            conn.execute(f"DELETE FROM {t}")
        _ensure_cash(conn)
