import numpy as np
import pandas as pd
import pytest


def make_prices(n=1500, seed=1, drift=0.0004, vol=0.015, start=100.0):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    spread = np.abs(rng.normal(0, 0.006, n))
    return pd.DataFrame({
        "Open": close * (1 + rng.normal(0, 0.003, n)),
        "High": close * (1 + spread),
        "Low": close * (1 - spread),
        "Close": close,
        "Volume": rng.integers(1e6, 5e6, n).astype(float),
    }, index=idx)


class FakeProvider:
    """Deterministic stand-in for Yahoo Finance."""

    def history(self, symbol):
        from quantlab.data.market import DataError

        if symbol == "NOPE":
            raise DataError("No price data found for NOPE")
        return make_prices(seed=sum(map(ord, symbol)))

    def history_bulk(self, symbols, period="2y"):
        n = 520 if period == "2y" else 1300
        return {s: make_prices(n, seed=sum(map(ord, s))) for s in symbols if s != "NOPE"}

    def fundamentals(self, symbol):
        soon = (pd.Timestamp.today() + pd.Timedelta(days=5)).date().isoformat()
        return {"type": "EQUITY", "sector": "Technology", "pe": 25.0, "forward_pe": 22.0, "market_cap": 5e10,
                "debt_to_equity": 0.8, "profit_margin": 0.2, "dividend_yield": 0.01,
                "earnings_dates": ["2001-01-01", soon]}

    def name(self, symbol):
        return f"{symbol} Corp"

    def expiries(self, symbol):
        return [] if symbol.endswith(".AX") else [(pd.Timestamp.today() + pd.Timedelta(days=60)).strftime("%Y-%m-%d")]

    def chain(self, symbol, expiry):
        from quantlab.analytics.options import bs_price

        spot = float(self.history(symbol)["Close"].iloc[-1])
        strikes = [round(spot * m, 2) for m in (0.9, 0.95, 1.0, 1.05, 1.1)]
        def side(kind):
            rows = []
            for k in strikes:
                mid = bs_price(spot, k, 60 / 365, 0.04, 0.30, kind)
                rows.append({"strike": k, "bid": mid * 0.99, "ask": mid * 1.01, "lastPrice": mid,
                             "volume": 10, "openInterest": 100})
            return pd.DataFrame(rows)
        return side("call"), side("put")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own data directory, a fake market and no network."""
    from quantlab import signals
    from quantlab.config import settings
    from quantlab.data import market, news

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "password", "")
    monkeypatch.setattr(news, "_download", lambda symbol: [
        {"title": f"{symbol} shares surge after earnings beat", "link": "http://x/1", "source": "Test",
         "published": pd.Timestamp.now(tz="UTC").isoformat()},
        {"title": f"Analyst downgrades {symbol} on weak outlook", "link": "http://x/2", "source": "Test",
         "published": pd.Timestamp.now(tz="UTC").isoformat()},
    ])
    news._last_fetch.clear()
    signals._cache.clear()
    market.set_provider(FakeProvider())
    yield
