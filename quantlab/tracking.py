"""Your real holdings, and saved portfolios followed over time.

* Holdings are imported from a CSV file exported by a broker (or typed by hand).
  They are read-only context: the app never trades them.
* A portfolio built on the Portfolio screen can be saved, then tracked against
  its target and the market index, with suggestions when it drifts.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timezone

import pandas as pd

from . import signals
from .analytics import risk
from .data import market
from .db import connect

SYMBOL_COLS = ("symbol", "ticker", "code", "asx code", "security code", "instrument", "stock")
QTY_COLS = ("quantity", "qty", "shares", "units", "holding", "holdings", "position", "available units")
COST_COLS = ("average cost", "avg cost", "average price", "avg price", "cost", "cost price", "purchase price",
             "cost basis per share", "average cost price", "avg. price")
DRIFT = 0.05  # suggest rebalancing when a weight is this far from its starting weight


def _number(text) -> float | None:
    try:
        return float(str(text).replace(",", "").replace("$", "").replace("A", "").strip())
    except ValueError:
        return None


def import_holdings(csv_text: str, default_market: str = "US", broker: str = "") -> dict:
    """Replace the holdings recorded at ``broker`` with the rows of a CSV file.

    Needs a column for the ticker and one for the quantity; an average-cost column is used when
    present. Column names are matched loosely (Symbol/Ticker/Code, Quantity/Shares/Units, ...).
    With ``default_market`` "ASX", bare codes such as BHP are read as BHP.AX.
    """
    text = csv_text.lstrip("﻿").strip()
    if not text:
        raise ValueError("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    cols = {(c or "").strip().lower(): c for c in reader.fieldnames or []}
    find = lambda names: next((cols[n] for n in names if n in cols), None)
    sym_col, qty_col, cost_col = find(SYMBOL_COLS), find(QTY_COLS), find(COST_COLS)
    if not sym_col or not qty_col:
        raise ValueError("Could not find the ticker and quantity columns. The file needs a header row with a column "
                         "named Symbol (or Ticker/Code) and one named Quantity (or Shares/Units). "
                         f"Columns found: {', '.join(reader.fieldnames or []) or 'none'}.")
    rows, skipped = {}, []
    for n, row in enumerate(reader, start=2):
        raw = (row.get(sym_col) or "").strip()
        qty = _number(row.get(qty_col))
        if not raw and qty is None:
            continue
        try:
            symbol = market.normalize_symbol(raw.split()[0] if raw else "")
            if default_market == "ASX" and "." not in symbol and not symbol.startswith("^"):
                symbol += ".AX"
            if qty is None or qty <= 0:
                raise market.DataError("quantity is missing or not positive")
            market.get_history(symbol)
        except market.DataError as exc:
            skipped.append(f"Line {n} ({raw or 'blank'}): {exc}")
            continue
        cost = _number(row.get(cost_col)) if cost_col else None
        if symbol in rows:  # the same stock on two lines: combine
            old = rows[symbol]
            total = old["qty"] + qty
            cost = (old["qty"] * old["cost"] + qty * cost) / total if old["cost"] and cost else old["cost"] or cost
            qty = total
        rows[symbol] = {"qty": qty, "cost": cost}
    if not rows:
        raise ValueError("No usable rows were found. " + " ".join(skipped[:3]))
    with connect() as conn:
        conn.execute("DELETE FROM holdings WHERE broker=?", (broker,))
        conn.executemany("INSERT INTO holdings (symbol, broker, qty, cost, currency) VALUES (?,?,?,?,?)",
                         [(s, broker, r["qty"], r["cost"], market.currency_of(s)) for s, r in rows.items()])
    return {"imported": len(rows), "skipped": skipped, "broker": broker}


def set_holding(symbol: str, qty: float, cost: float | None = None, broker: str = "") -> None:
    """Add or replace one holding by hand. A quantity of zero removes it."""
    symbol = market.normalize_symbol(symbol)
    with connect() as conn:
        if not qty or qty <= 0:
            conn.execute("DELETE FROM holdings WHERE symbol=? AND broker=?", (symbol, broker))
            return
    market.get_history(symbol)  # rejects unknown tickers
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO holdings (symbol, broker, qty, cost, currency) VALUES (?,?,?,?,?)",
                     (symbol, broker, qty, cost or None, market.currency_of(symbol)))


def held_shares() -> dict[str, float]:
    """Total shares of each stock across all brokers."""
    with connect() as conn:
        return {r["symbol"]: r["q"] for r in conn.execute("SELECT symbol, SUM(qty) AS q FROM holdings GROUP BY symbol")}


def clear_holdings(broker: str | None = None) -> None:
    with connect() as conn:
        if broker is None:
            conn.execute("DELETE FROM holdings")
        else:
            conn.execute("DELETE FROM holdings WHERE broker=?", (broker,))


def _beta(symbol: str) -> float | None:
    try:
        a = market.get_history(symbol, years=2)["Close"].pct_change()
        m = market.get_history(market.benchmark_of(symbol), years=2)["Close"].pct_change()
        b = risk.beta(a, m)
        return None if b != b else b
    except market.DataError:
        return None


def holdings_summary() -> dict:
    """Value, profit, risk and the app's current read on everything you own."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM holdings ORDER BY symbol, broker")]
    for h in rows:
        try:
            q = market.get_quote(h["symbol"])
            sig = signals.compute_signal(h["symbol"], use_ml=False, use_news=False)
            h.update(name=q["name"], price=q["price"], signal=sig["label"], score=sig["score"])
        except market.DataError:
            h.update(name=h["symbol"], price=h["cost"] or 0.0, signal=None, score=None)
        h["value"] = h["price"] * h["qty"]
        h["pnl"] = (h["price"] - h["cost"]) * h["qty"] if h["cost"] else None
        h["pnl_pct"] = h["price"] / h["cost"] - 1 if h["cost"] else None
        h["beta"] = _beta(h["symbol"])
        h["days_to_earnings"] = market.get_fundamentals(h["symbol"]).get("days_to_earnings")
    books, warnings = [], []
    for cur in sorted({h["currency"] for h in rows}):
        mine = [h for h in rows if h["currency"] == cur]
        total = sum(h["value"] for h in mine)
        for h in mine:
            h["weight"] = h["value"] / total if total else 0.0
        for sym in sorted({h["symbol"] for h in mine}):
            combined = sum(h["weight"] for h in mine if h["symbol"] == sym)
            if combined > 0.25 and len({h["symbol"] for h in mine}) > 1:
                warnings.append(f"{sym} is {combined:.0%} of your {cur} holdings: a single stock that large "
                                "dominates your results.")
        costed = [h for h in mine if h["pnl"] is not None]
        books.append({"currency": cur, "value": total, "pnl": sum(h["pnl"] for h in costed) if costed else None,
                      "beta": sum(h["weight"] * h["beta"] for h in mine if h["beta"] is not None), "count": len(mine)})
    for h in {h["symbol"]: h for h in rows}.values():
        if h["score"] is not None and h["score"] <= -0.2:
            warnings.append(f"{h['symbol']} is in a downtrend (price signal: {h['signal']}).")
        if h["days_to_earnings"] is not None and 0 <= h["days_to_earnings"] <= 7:
            warnings.append(f"{h['symbol']} reports earnings in {h['days_to_earnings']} day(s).")
    # The same stock at two brokers is one exposure: judge concentration on the combined position.
    split = []
    for cur in sorted({h["currency"] for h in rows}):
        mine = [h for h in rows if h["currency"] == cur]
        total = sum(h["value"] for h in mine)
        for name in sorted({h["broker"] for h in mine}):
            value = sum(h["value"] for h in mine if h["broker"] == name)
            split.append({"broker": name or "Unassigned", "currency": cur, "value": value,
                          "share": value / total if total else 0.0, "positions": sum(h["broker"] == name for h in mine)})
    return {"books": books, "holdings": rows, "by_broker": split, "warnings": warnings,
            "note": "Signals here use price trend and momentum only, so the list loads quickly."}


# ---------------------------------------------------------------- saved portfolios

def save_portfolio(name: str, plan: dict) -> int:
    """Save the output of ``planner.build_portfolio`` so it can be followed over time."""
    if not plan.get("positions"):
        raise ValueError("This portfolio holds no shares, so there is nothing to track.")
    cur_market = plan["inputs"]["market"]
    index_symbol = "^AXJO" if cur_market == "ASX" else "^GSPC"
    data = {
        "inputs": plan["inputs"], "cash": plan["cash"], "index": index_symbol,
        "index_start": float(market.get_history(index_symbol)["Close"].iloc[-1]),
        "expected_return": plan["expected_return"],
        "positions": [{k: p[k] for k in ("symbol", "name", "role", "shares", "price", "stop", "weight")} for p in plan["positions"]],
    }
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute("INSERT INTO portfolios (created, name, data) VALUES (?,?,?)",
                           (created, (name or "Portfolio").strip()[:60], json.dumps(data)))
    return cur.lastrowid


def list_portfolios() -> list[dict]:
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT id, created, name, data FROM portfolios ORDER BY id DESC")]
    return [{"id": r["id"], "created": r["created"], "name": r["name"], "inputs": json.loads(r["data"])["inputs"]} for r in rows]


def delete_portfolio(portfolio_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM portfolios WHERE id=?", (portfolio_id,)).rowcount > 0


def track_portfolio(portfolio_id: int) -> dict:
    """How a saved portfolio has done since it was saved, and what, if anything, to do about it."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
    if not row:
        raise KeyError("No such portfolio.")
    data = json.loads(row["data"])
    inputs, start = data["inputs"], datetime.fromisoformat(row["created"])
    start_day = pd.Timestamp(start.date())
    equity = inputs["equity"]
    closes = {}
    for p in data["positions"]:
        try:
            closes[p["symbol"]] = market.get_history(p["symbol"])["Close"]
        except market.DataError:
            closes[p["symbol"]] = pd.Series([p["price"]], index=[start_day])
    positions, value_now = [], data["cash"]
    for p in data["positions"]:
        c = closes[p["symbol"]]
        price = float(c.iloc[-1])
        since = c[c.index > start_day]
        try:
            sig = signals.compute_signal(p["symbol"], use_ml=False, use_news=False)
            label, score = sig["label"], sig["score"]
        except market.DataError:
            label, score = None, None
        positions.append({**p, "price_now": price, "value_now": price * p["shares"], "return": price / p["price"] - 1,
                          "stop_hit": bool(p["stop"] and len(since) and since.min() <= p["stop"]),
                          "signal": label, "score": score})
        value_now += price * p["shares"]
    for p in positions:
        p["weight_now"] = p["value_now"] / value_now

    frame = pd.DataFrame({s: c[c.index >= start_day] for s, c in closes.items()}).ffill()
    history = (frame * pd.Series({p["symbol"]: p["shares"] for p in data["positions"]})).sum(axis=1) + data["cash"]
    history = pd.concat([pd.Series([equity], index=[start_day]), history[history.index > start_day]])
    try:
        idx = market.get_history(data["index"])["Close"]
        idx = idx[idx.index > start_day] / data["index_start"] * equity
        index_history = pd.concat([pd.Series([equity], index=[start_day]), idx])
        index_return = float(index_history.iloc[-1] / equity - 1)
    except market.DataError:
        index_history, index_return = None, None

    days = max((date.today() - start.date()).days, 0)
    total_days = inputs["months"] * 30.4375
    progress = min(days / total_days, 1.0)
    ret = value_now / equity - 1
    on_track = (1 + inputs["target_return"]) ** progress - 1

    suggestions = []
    for p in positions:
        if p["stop_hit"]:
            suggestions.append(f"Sell {p['symbol']}: it has traded below its stop of {p['stop']:,.2f} since you bought it.")
        elif p["role"] == "Idea" and p["score"] is not None and p["score"] <= -0.2:
            suggestions.append(f"Review {p['symbol']}: its price signal has turned to {p['signal']}.")
        drift = p["weight_now"] - p["weight"]
        if abs(drift) >= DRIFT and not p["stop_hit"]:
            shares = round(abs(drift) * value_now / p["price_now"], 2 if inputs.get("fractional") else None)
            if shares:
                suggestions.append(f"{'Trim' if drift > 0 else 'Add to'} {p['symbol']} by about {shares:g} share(s): it is now "
                                   f"{p['weight_now']:.0%} of the portfolio against {p['weight']:.0%} at the start.")
    if progress >= 1:
        suggestions.append("The planned period has ended. Build a fresh portfolio from the current ideas.")
    if not suggestions:
        suggestions.append("Nothing to do: no stop has been hit and the weights are close to where they started.")
    line = lambda s: [{"time": d.strftime("%Y-%m-%d"), "value": float(v)} for d, v in s.items()]
    return {
        "id": row["id"], "name": row["name"], "created": row["created"], "inputs": inputs,
        "value": value_now, "return": ret, "target_return": inputs["target_return"], "on_track_return": on_track,
        "ahead_of_target_path": ret - on_track, "index": data["index"], "index_return": index_return,
        "days_elapsed": days, "days_total": round(total_days), "cash": data["cash"],
        "positions": positions, "suggestions": suggestions,
        "history": line(history), "index_history": line(index_history) if index_history is not None else [],
    }
