import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from quantlab import chat, paper
from quantlab.api import app
from quantlab.config import settings
from quantlab.data import market
from quantlab.db import connect

client = TestClient(app)


def _contract():
    expiry = market.get_expiries("AAPL")[0]
    chain = market.get_chain("AAPL", expiry)
    strike = float(chain.calls["strike"].iloc[2])  # at the money in the fake chain
    return expiry, strike, chain


def test_option_round_trip_fills_at_ask_then_bid():
    expiry, strike, chain = _contract()
    row = chain.calls.iloc[2]
    start = settings.paper_start_cash
    buy = paper.place_option_order("AAPL", expiry, strike, "call", "buy", 2)
    assert buy["price"] == pytest.approx(row["ask"]) and buy["fill_source"] == "ask"
    acct = paper.account()
    usd = next(b for b in acct["books"] if b["currency"] == "USD")
    assert usd["cash"] == pytest.approx(start - row["ask"] * 200 - 2 * settings.paper_option_fee)
    pos = acct["options"][0]
    assert pos["qty"] == 2 and pos["value"] == pytest.approx((row["bid"] + row["ask"]) / 2 * 200)
    assert 0 < pos["delta_shares"] < 200 and pos["mark_source"] == "live quote"
    assert usd["equity"] == pytest.approx(usd["cash"] + pos["value"])

    sell = paper.place_option_order("AAPL", expiry, strike, "call", "sell", 2)
    assert sell["price"] == pytest.approx(row["bid"]) and sell["realized"] < 0  # paid the spread and fees
    assert paper.account()["options"] == []


def test_option_order_rules():
    expiry, strike, _ = _contract()
    with pytest.raises(paper.OrderError, match="only 0 held"):
        paper.place_option_order("AAPL", expiry, strike, "put", "sell", 1)
    with pytest.raises(paper.OrderError, match="no listed option expiry"):
        paper.place_option_order("AAPL", "2001-01-01", strike, "call", "buy", 1)
    with pytest.raises(paper.OrderError, match="no listed option expiry"):
        paper.place_option_order("BHP.AX", expiry, 50, "call", "buy", 1)  # ASX has no chains
    with pytest.raises(paper.OrderError, match="cannot be filled"):
        paper.place_option_order("AAPL", expiry, 1.23, "call", "buy", 1)  # strike not in the chain
    with pytest.raises(paper.OrderError, match="Not enough"):
        paper.place_option_order("AAPL", expiry, strike, "call", "buy", 10**6)
    with pytest.raises(paper.OrderError):
        paper.place_option_order("AAPL", expiry, strike, "call", "buy", 1.5)


def test_expired_options_settle_at_intrinsic_value():
    paper.account()  # creates cash rows
    close = market.get_history("AAPL")["Close"]
    expiry_day = close.index[-3]
    settle = float(close.loc[expiry_day])
    itm, otm = round(settle * 0.9, 2), round(settle * 1.1, 2)
    with connect() as conn:
        for strike in (itm, otm):
            conn.execute("INSERT INTO paper_options VALUES (?,?,?,?,?,?,?,?)",
                         (paper.contract_id("AAPL", f"{expiry_day:%Y-%m-%d}", strike, "call"), "AAPL",
                          f"{expiry_day:%Y-%m-%d}", strike, "call", 1, 5.0, "USD"))
    acct = paper.account()
    assert acct["options"] == []
    usd = next(b for b in acct["books"] if b["currency"] == "USD")
    assert usd["cash"] == pytest.approx(settings.paper_start_cash + (settle - itm) * 100)
    expired = [t for t in paper.trades() if t["side"] == "expired"]
    assert len(expired) == 2
    assert sorted(t["realized"] for t in expired) == pytest.approx(sorted([(settle - itm - 5) * 100, -500.0]))


def test_option_order_api():
    expiry, strike, _ = _contract()
    body = {"symbol": "AAPL", "expiry": expiry, "strike": strike, "kind": "put", "side": "buy", "contracts": 1}
    assert client.post("/api/paper/options/orders", json=body).json()["contract"] == f"AAPL {expiry} {strike:g} put"
    assert client.post("/api/paper/options/orders", json={**body, "side": "sell", "contracts": 5}).status_code == 400
    assert len(client.get("/api/paper/account").json()["options"]) == 1


def test_chat_tools_return_live_data_and_report_errors():
    assert json.loads(chat.get_signal("AAPL"))["label"]
    assert json.loads(chat.get_price_stats("BHP.AX", 1))["quote"]["currency"] == "AUD"
    assert len(json.loads(chat.get_news("AAPL"))["headlines"]) == 2
    expiry = json.loads(chat.get_option_expiries("AAPL"))[0]
    assert json.loads(chat.get_option_chain("AAPL", expiry))["summary"]["atm_iv"] > 0
    assert json.loads(chat.price_option(42, 40, 182.5, 0.2, "call", 0.1))["price_per_share"] == pytest.approx(4.76, abs=0.01)
    assert "buy_and_hold" in json.loads(chat.run_backtest("AAPL", "breakout", 3))
    assert "books" in json.loads(chat.get_paper_account())
    assert len(json.loads(chat.get_watchlist())) == len(settings.default_watchlist)
    assert chat.get_signal("NOPE").startswith("Error:")
    assert chat.run_backtest("AAPL", "nonsense").startswith("Error:")


def test_chat_tool_schemas_are_generated():
    from anthropic import beta_tool

    tools = {t.name: t.to_dict() for t in (beta_tool(fn) for fn in chat.TOOL_FUNCTIONS)}
    assert set(tools["get_option_chain"]["input_schema"]["required"]) == {"symbol", "expiry"}
    assert "Black-Scholes" in tools["price_option"]["description"]


class FakeRunner:
    def __init__(self, kwargs):
        self.kwargs = kwargs

    def __iter__(self):
        yield SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", name="get_signal")])
        yield SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking"),
                                                               SimpleNamespace(type="text", text="AAPL is a **Buy**.")])


def test_chat_endpoint(monkeypatch):
    assert client.get("/api/chat/status").json()["enabled"] is False
    assert client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 503

    seen = {}
    def tool_runner(**kwargs):
        seen.update(kwargs)
        return FakeRunner(kwargs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=tool_runner))))
    r = client.post("/api/chat", json={"viewing": "aapl", "messages": [
        {"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "What about AAPL?"}]})
    assert r.json() == {"reply": "AAPL is a **Buy**.", "tools_used": ["get_signal"]}
    assert seen["model"] == "claude-opus-5-5" and len(seen["messages"]) == 3
    assert "AAPL open in the app" in seen["system"][1]["text"]
    assert len(seen["tools"]) == len(chat.TOOL_FUNCTIONS) == 10
    assert client.post("/api/chat", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 422


def test_private_by_default_headers():
    assert client.get("/").headers["x-robots-tag"].startswith("noindex")
    assert "Disallow: /" in client.get("/robots.txt").text


def test_ideas_scan_ranks_and_filters():
    from quantlab import ideas

    assert client.get("/api/ideas").json()["result"] is None
    assert client.post("/api/ideas/scan", params={"market_name": "MARS"}).status_code == 400
    assert ideas.start_scan(["US", "ASX"], wait=True)
    st = client.get("/api/ideas").json()
    res = st["result"]
    assert st["status"] == "idle" and st["error"] is None
    assert res["screened"] == res["universe_size"] == len(ideas.US_UNIVERSE) + len(ideas.ASX_UNIVERSE)
    scores = [r["score"] for r in res["table"]]
    assert scores == sorted(scores, reverse=True)
    assert res["shortlisted"] <= 2 * ideas.SHORTLIST
    assert res["ideas"], "random-walk test data still yields some uptrends"
    for idea in res["ideas"]:
        assert idea["score"] >= 0.2 and idea["label"] in ("Buy", "Strong Buy")
        assert idea["plan"]["stop"] < idea["price"] < idea["plan"]["target"] and idea["reasons"]
    assert json.loads(chat.get_trade_ideas())["ideas"][0]["symbol"] == res["ideas"][0]["symbol"]


def test_universes_are_valid_unique_tickers():
    from quantlab import ideas

    for universe in ideas.UNIVERSES.values():
        assert len(universe) == len(set(universe))
        assert all(market.normalize_symbol(s) == s for s in universe)
