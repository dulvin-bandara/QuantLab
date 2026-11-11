"""The daily run: keeps the app's records building up without anyone opening it.

Once a day it refreshes the signal and news for every watchlist ticker, fills any
paper orders that triggered, records the paper account's value, runs the ideas
scan, and raises alerts for anything that changed. The signal track record and
the news archive only grow on days this runs.

It runs automatically while the app is open (see ``QUANTLAB_DAILY_TIME``), and
can be run by itself with ``python -m quantlab daily``, which is what a system
scheduler should call so it happens even when the app is closed.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

from . import alerts, ideas, paper, signals
from .config import settings
from .data import market
from .db import connect

_lock = threading.Lock()


def _watchlist() -> list[str]:
    with connect() as conn:
        rows = [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist ORDER BY symbol")]
    return rows or list(settings.default_watchlist)


def _followed() -> list[str]:
    """Everything the owner has a stake in: watchlist, paper positions and imported holdings."""
    with connect() as conn:
        extra = [r["symbol"] for r in conn.execute("SELECT symbol FROM paper_positions UNION SELECT symbol FROM holdings")]
    return sorted(set(_watchlist()) | set(extra))


def _alert_once(kind: str, symbol: str, message: str) -> None:
    """Raise an alert unless the identical one was already raised (earnings reminders repeat daily otherwise)."""
    with connect() as conn:
        dup = conn.execute("SELECT 1 FROM alerts WHERE kind=? AND symbol=? AND message LIKE ?",
                           (kind, symbol, message + "%")).fetchone()
    if not dup:
        alerts.raise_alert(kind, symbol, message)


def run_daily(scan: bool = True) -> dict:
    """Do the whole daily routine now. Safe to run more than once a day."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "A daily run is already in progress."}
    try:
        summary = {"started": datetime.now().isoformat(timespec="seconds"), "signals": 0, "changes": [], "errors": []}
        for symbol in _watchlist():
            try:
                with connect() as conn:
                    prev = conn.execute("SELECT date, label FROM signal_log WHERE symbol=? ORDER BY date DESC LIMIT 1",
                                        (symbol,)).fetchone()
                sig = signals.compute_signal(symbol)
                summary["signals"] += 1
                if prev and prev["date"] != sig["as_of"] and prev["label"] != sig["label"]:
                    summary["changes"].append(symbol)
                    alerts.raise_alert("signal", symbol, f"{symbol} signal changed from {prev['label']} to {sig['label']} "
                                       f"(score {sig['score']:+.2f}) at {sig['price']:.2f} {sig['currency']}.")
            except Exception as exc:
                summary["errors"].append(f"{symbol}: {exc}")

        for symbol in _followed():
            days = market.get_fundamentals(symbol).get("days_to_earnings")
            if days is not None and 0 <= days <= 3:
                when = market.get_fundamentals(symbol)["next_earnings"]
                _alert_once("earnings", symbol, f"{symbol} reports earnings on {when}")

        try:
            summary["orders_filled"] = sum(e["status"] == "filled" for e in paper.process_orders())
            paper.account()  # records today's equity
        except Exception as exc:
            summary["errors"].append(f"paper account: {exc}")

        if scan:
            before = {i["symbol"] for i in (ideas.latest() or {"ideas": []})["ideas"]}
            if ideas.start_scan(list(ideas.UNIVERSE), wait=True):
                res = ideas.latest() or {"ideas": []}
                summary["ideas"] = len(res["ideas"])
                summary["new_ideas"] = [i["symbol"] for i in res["ideas"] if i["symbol"] not in before]
        summary["finished"] = datetime.now().isoformat(timespec="seconds")
        with connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('daily', ?)", (json.dumps(summary),))
        return summary
    finally:
        _lock.release()


def last_run() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='daily'").fetchone()
    return json.loads(row["value"]) if row else None


def is_due(now: datetime | None = None) -> bool:
    """True when today's scheduled time has passed without a run, or nothing has run for 36 hours."""
    if settings.daily_time.lower() == "off":
        return False
    now = now or datetime.now()
    try:
        hour, minute = (int(x) for x in settings.daily_time.split(":"))
    except ValueError:
        hour, minute = 18, 0
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    last = last_run()
    last_at = datetime.fromisoformat(last["started"]) if last and last.get("started") else None
    if last_at is None:
        return now >= scheduled
    return (now >= scheduled > last_at) or (now - last_at > timedelta(hours=36))


def start_scheduler() -> None:
    """Background loop used by the running app: checks once a minute whether the daily run is due."""
    def loop():
        time.sleep(20)  # let the server finish starting
        while True:
            try:
                if is_due():
                    run_daily()
            except Exception:
                pass  # never let a bad day kill the loop; the next minute tries again
            time.sleep(60)

    threading.Thread(target=loop, daemon=True, name="quantlab-daily").start()
