"""Paper trading: a simulated stock account, one cash balance per currency.

Orders are market orders filled immediately at the latest available price
(Yahoo's delayed last/close) plus slippage and commission. Long positions
only; options and short selling are not simulated yet.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

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

    books = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with connect() as conn:
        for cur, row in cash_rows.items():
            mine = [p for p in positions if p["currency"] == cur]
            invested = sum(p["value"] for p in mine)
            equity = row["amount"] + invested
            # Portfolio beta: each holding's beta weighted by its share of equity (cash has beta 0).
            pbeta = sum(p["value"] / equity * p["beta"] for p in mine if p["beta"] is not None) if equity else 0.0
            conn.execute("INSERT OR REPLACE INTO paper_equity (date, currency, equity) VALUES (?,?,?)",
                         (today, cur, equity))
            history = [dict(r) for r in conn.execute(
                "SELECT date, equity FROM paper_equity WHERE currency=? ORDER BY date", (cur,))]
            books.append({"currency": cur, "cash": row["amount"], "invested": invested, "equity": equity,
                          "start": row["start"], "pnl": equity - row["start"],
                          "pnl_pct": equity / row["start"] - 1 if row["start"] else 0.0,
                          "beta": pbeta, "history": history})
    return {"books": books, "positions": positions}


def trades(limit: int = 200) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,))]


def reset() -> None:
    with connect() as conn:
        for t in ("paper_cash", "paper_positions", "paper_trades", "paper_equity"):
            conn.execute(f"DELETE FROM {t}")
        _ensure_cash(conn)
