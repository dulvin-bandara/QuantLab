import pytest
from fastapi.testclient import TestClient

from quantlab import paper, signals
from quantlab.api import app
from quantlab.config import settings
from quantlab.data import market, news

client = TestClient(app)


def test_symbol_normalisation():
    assert market.normalize_symbol(" asx:bhp ") == "BHP.AX"
    assert market.currency_of("BHP.AX") == "AUD" and market.currency_of("AAPL") == "USD"
    with pytest.raises(market.DataError):
        market.normalize_symbol("../etc")


def test_news_sentiment_direction():
    assert news.score_headline("Shares surge after earnings beat and upgrade") > 0.3
    assert news.score_headline("Stock plunges as company misses and faces fraud probe") < -0.3
    s = news.news_sentiment("AAPL")
    assert s["count"] == 2 and s["positive"] == 1 and s["negative"] == 1


def test_signal_shape_and_logging():
    s = signals.compute_signal("AAPL")
    assert -1 <= s["score"] <= 1 and s["label"] in ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell")
    assert set(s["components"]) >= {"trend", "momentum", "news"}
    assert sum(c["weight"] for c in s["components"].values()) == pytest.approx(1)
    assert signals.signal_performance()["logged"] == 1


def test_paper_round_trip():
    start = settings.paper_start_cash
    buy = paper.place_order("AAPL", "buy", 10)
    acct = paper.account()
    usd = next(b for b in acct["books"] if b["currency"] == "USD")
    assert usd["cash"] == pytest.approx(start - buy["price"] * 10 - buy["commission"])
    assert acct["positions"][0]["qty"] == 10
    sell = paper.place_order("AAPL", "sell", 10)
    assert sell["realized"] < 0  # same price both ways, so slippage and commission are the loss
    assert paper.account()["positions"] == []
    with pytest.raises(paper.OrderError):
        paper.place_order("AAPL", "sell", 1)
    with pytest.raises(paper.OrderError):
        paper.place_order("AAPL", "buy", 10**9)
    paper.place_order("BHP.AX", "buy", 5)
    aud = next(b for b in paper.account()["books"] if b["currency"] == "AUD")
    assert aud["invested"] > 0 and usd["currency"] == "USD"


def test_api_endpoints():
    assert client.get("/api/health").json()["status"] == "ok"
    assert "AAPL" in client.get("/api/watchlist").json()
    assert client.post("/api/watchlist", json={"symbol": "NOPE"}).status_code == 404
    assert "MSFT" in client.post("/api/watchlist", json={"symbol": "msft"}).json()
    assert "MSFT" not in client.delete("/api/watchlist", params={"symbol": "MSFT"}).json()
    rows = client.get("/api/overview").json()
    assert len(rows) == len(settings.default_watchlist) and all("label" in r for r in rows)
    h = client.get("/api/history", params={"symbol": "AAPL", "years": 1}).json()
    assert len(h["candles"]) > 200 and h["stats"]["beta"] is not None
    bt = client.post("/api/backtest", json={"symbol": "AAPL", "strategy": "breakout"}).json()
    assert bt["equity"] and "sharpe" in bt["stats"]
    assert client.post("/api/backtest", json={"symbol": "AAPL", "strategy": "zzz"}).status_code == 400
    exp = client.get("/api/options/expiries", params={"symbol": "AAPL"}).json()["expiries"]
    chain = client.get("/api/options/chain", params={"symbol": "AAPL", "expiry": exp[0]}).json()
    assert chain["calls"] and chain["summary"]["atm_iv"] == pytest.approx(0.30, abs=0.02)
    assert client.get("/api/options/expiries", params={"symbol": "BHP.AX"}).json()["expiries"] == []
    px = client.post("/api/options/price", json={"spot": 42, "strike": 40, "days": 182.5, "vol": 0.2, "rate": 0.1}).json()
    assert px["price"] == pytest.approx(4.76, abs=0.01)
    assert client.post("/api/paper/orders", json={"symbol": "AAPL", "side": "buy", "qty": 0}).status_code == 400
    assert client.get("/").status_code == 200


def test_password_protects_api(monkeypatch):
    monkeypatch.setattr(settings, "password", "hunter2")
    c = TestClient(app)
    assert c.get("/api/watchlist").status_code == 401
    assert c.get("/api/session").json() == {"login_required": True, "logged_in": False}
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert c.post("/api/login", json={"password": "hunter2"}).status_code == 200
    assert c.get("/api/watchlist").status_code == 200
    c.cookies.set("ql_session", "9999999999.forged")
    assert c.get("/api/watchlist").status_code == 401
