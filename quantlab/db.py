"""SQLite storage for the watchlist, news archive, signal log and paper account."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, added TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS news (
    symbol TEXT, link TEXT, title TEXT, source TEXT, published TEXT, sentiment REAL,
    PRIMARY KEY (symbol, title)
);
CREATE TABLE IF NOT EXISTS signal_log (
    symbol TEXT, date TEXT, score REAL, label TEXT, close REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS paper_cash (currency TEXT PRIMARY KEY, amount REAL, start REAL);
CREATE TABLE IF NOT EXISTS paper_positions (symbol TEXT PRIMARY KEY, qty REAL, avg_cost REAL, currency TEXT);
CREATE TABLE IF NOT EXISTS paper_options (
    contract TEXT PRIMARY KEY, symbol TEXT, expiry TEXT, strike REAL, kind TEXT,
    qty REAL, avg_cost REAL, currency TEXT
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, qty REAL,
    price REAL, commission REAL, currency TEXT, realized REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, symbol TEXT, side TEXT, qty REAL, type TEXT, price REAL,
    status TEXT, stop_loss REAL, take_profit REAL, oco INTEGER, filled_ts TEXT, fill_price REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, symbol TEXT, message TEXT, read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS portfolios (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, name TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS holdings (
    symbol TEXT, broker TEXT DEFAULT '', qty REAL, cost REAL, currency TEXT, PRIMARY KEY (symbol, broker)
);
CREATE TABLE IF NOT EXISTS brokers (name TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS paper_equity (date TEXT, currency TEXT, equity REAL, PRIMARY KEY (date, currency));
"""


_ready: set[str] = set()
_setup_lock = threading.Lock()


def _setup(conn, path: str) -> None:
    """Create tables and upgrade older databases, once per database file per process.

    Guarded by a lock: the web app opens several connections at once when a page loads, and two
    of them must not both try to add the same column.
    """
    with _setup_lock:
        if path in _ready:
            return
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(news)")}
        for col, kind in (("ai_sentiment", "REAL"), ("relevant", "INTEGER")):
            if col not in have:
                try:
                    conn.execute(f"ALTER TABLE news ADD COLUMN {col} {kind}")
                except sqlite3.OperationalError:
                    pass  # another process added it first
        conn.commit()
        _ready.add(path)


@contextmanager
def connect():
    path = str(settings.data_dir / "quantlab.db")
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        _setup(conn, path)
        yield conn
        conn.commit()
    finally:
        conn.close()
