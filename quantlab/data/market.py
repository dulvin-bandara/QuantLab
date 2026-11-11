"""Market data access: daily price history, quotes and option chains.

Data comes from Yahoo Finance through ``yfinance``. It is free, delayed and
unofficial, so every fetch is cached and the last good copy on disk is used when
Yahoo is unreachable. Tests swap in a synthetic provider with ``set_provider``.

Symbols follow Yahoo's convention: plain tickers for US listings (``AAPL``) and
an ``.AX`` suffix for ASX listings (``BHP.AX``). ``ASX:BHP`` is also accepted.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import pandas as pd

from ..config import settings

OHLCV = ["Open", "High", "Low", "Close", "Volume"]
_SYMBOL_RE = re.compile(r"^[\^A-Z0-9][A-Z0-9.\-=]{0,14}$")


class DataError(Exception):
    """Raised when data for a symbol cannot be obtained."""


def normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if s.startswith("ASX:"):
        s = s[4:] + ".AX"
    if not _SYMBOL_RE.match(s):
        raise DataError(f"'{symbol}' is not a valid ticker symbol")
    return s


def is_asx(symbol: str) -> bool:
    return symbol.endswith(".AX") or symbol in ("^AXJO", "^AORD")


def currency_of(symbol: str) -> str:
    return "AUD" if is_asx(symbol) else "USD"


def benchmark_of(symbol: str) -> str:
    """Broad market index used for beta and as market context in forecasts."""
    return "^AXJO" if is_asx(symbol) else "^GSPC"


@dataclass
class OptionChain:
    symbol: str
    expiry: str
    spot: float
    calls: pd.DataFrame
    puts: pd.DataFrame


class YahooProvider:
    def history(self, symbol: str) -> pd.DataFrame:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period="10y", interval="1d", auto_adjust=True)
        if df is None or df.empty:
            raise DataError(f"No price data found for {symbol}")
        return df

    def history_bulk(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Two years of daily bars for many tickers in one request. Unknown tickers are left out."""
        import logging

        import yfinance as yf

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # it logs every missing ticker
        data = yf.download(symbols, period="2y", interval="1d", auto_adjust=True, group_by="ticker",
                           progress=False, threads=True)
        out = {}
        for sym in symbols:
            try:
                df = data[sym].dropna(how="all")
            except KeyError:
                continue
            if not df.empty:
                out[sym] = df
        return out

    def name(self, symbol: str) -> str:
        import yfinance as yf

        info = yf.Ticker(symbol).info or {}
        return info.get("shortName") or info.get("longName") or symbol

    def expiries(self, symbol: str) -> list[str]:
        import yfinance as yf

        return list(yf.Ticker(symbol).options)

    def chain(self, symbol: str, expiry: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        import yfinance as yf

        c = yf.Ticker(symbol).option_chain(expiry)
        return c.calls, c.puts


_provider = YahooProvider()
_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def set_provider(provider) -> None:
    """Replace the data source (used by tests) and drop anything cached."""
    global _provider
    _provider = provider
    _cache.clear()


def _cached(key: str, ttl: float, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fetch()
    _cache[key] = (time.time(), value)
    return value


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[[c for c in OHLCV if c in df.columns]].copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df.index.name = "Date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["Close"])
    return df.astype(float)


def _fetch_history(symbol: str) -> pd.DataFrame:
    path = settings.data_dir / "prices" / f"{symbol.replace('^', '_')}.csv"
    try:
        df = _clean(_provider.history(symbol))
    except DataError:
        raise
    except Exception as exc:  # network trouble: fall back to the last saved copy
        if path.exists():
            return pd.read_csv(path, index_col=0, parse_dates=True)
        raise DataError(f"Could not fetch prices for {symbol}: {exc}") from exc
    if df.empty:
        raise DataError(f"No price data found for {symbol}")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    return df


def get_history(symbol: str, years: float | None = None) -> pd.DataFrame:
    """Daily, split- and dividend-adjusted OHLCV bars, oldest first."""
    symbol = normalize_symbol(symbol)
    with _lock:
        df = _cached(f"hist:{symbol}", settings.price_ttl, lambda: _fetch_history(symbol))
    if years:
        start = df.index[-1] - pd.Timedelta(days=int(365.25 * years))
        df = df[df.index >= start]
    return df.copy()


def get_history_bulk(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Two years of daily bars for each of many tickers, fetched together for screening.

    Tickers that fail to download are simply missing from the result.
    """
    symbols = [normalize_symbol(s) for s in symbols]

    def fetch():
        out = {}
        for i in range(0, len(symbols), 60):
            try:
                chunk = _provider.history_bulk(symbols[i:i + 60])
            except Exception:
                continue
            for sym, df in chunk.items():
                df = _clean(df)
                if not df.empty:
                    out[sym] = df
        return out

    with _lock:
        return _cached("bulk:" + ",".join(symbols), settings.price_ttl, fetch)


def get_name(symbol: str) -> str:
    symbol = normalize_symbol(symbol)

    def fetch():
        try:
            return _provider.name(symbol)
        except Exception:
            return symbol

    return _cached(f"name:{symbol}", 86400, fetch)


def get_quote(symbol: str) -> dict:
    symbol = normalize_symbol(symbol)
    df = get_history(symbol)
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
    return {
        "symbol": symbol,
        "name": get_name(symbol),
        "currency": currency_of(symbol),
        "price": last,
        "change": last - prev,
        "change_pct": (last / prev - 1) if prev else 0.0,
        "as_of": df.index[-1].strftime("%Y-%m-%d"),
    }


def get_expiries(symbol: str) -> list[str]:
    symbol = normalize_symbol(symbol)

    def fetch():
        try:
            return list(_provider.expiries(symbol))
        except Exception as exc:
            raise DataError(f"Could not fetch option expiries for {symbol}: {exc}") from exc

    return _cached(f"exp:{symbol}", 3600, fetch)


def get_chain(symbol: str, expiry: str) -> OptionChain:
    symbol = normalize_symbol(symbol)
    if expiry not in get_expiries(symbol):
        raise DataError(f"{symbol} has no listed expiry {expiry}")

    def fetch():
        try:
            return _provider.chain(symbol, expiry)
        except Exception as exc:
            raise DataError(f"Could not fetch the option chain for {symbol}: {exc}") from exc

    calls, puts = _cached(f"chain:{symbol}:{expiry}", settings.price_ttl, fetch)
    spot = float(get_history(symbol)["Close"].iloc[-1])
    return OptionChain(symbol, expiry, spot, calls.copy(), puts.copy())
