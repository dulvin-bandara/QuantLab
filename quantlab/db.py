"""SQLite storage for the watchlist, news archive, signal log and paper account."""
from __future__ import annotations

import sqlite3
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
CREATE TABLE IF NOT EXISTS paper_equity (date TEXT, currency TEXT, equity REAL, PRIMARY KEY (date, currency));
"""


@contextmanager
def connect():
    conn = sqlite3.connect(settings.data_dir / "quantlab.db", timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()
