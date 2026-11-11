from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from quantlab import ideas, planner
from quantlab.api import app

client = TestClient(app)
UTC = timezone.utc


def phase(symbol, *when):
    return planner.session_info(symbol, datetime(*when, tzinfo=UTC))["phase"]


def test_sessions_follow_exchange_clocks():
    # Wednesday 11 Nov 2026: New York is UTC-5, Sydney is UTC+11.
    assert phase("AAPL", 2026, 11, 11, 13, 0) == "pre-market"     # 08:00 New York
    assert phase("AAPL", 2026, 11, 11, 14, 40) == "opening"        # 09:40
    assert phase("AAPL", 2026, 11, 11, 17, 0) == "open"            # 12:00
    assert phase("AAPL", 2026, 11, 11, 20, 50) == "closing"        # 15:50
    assert phase("AAPL", 2026, 11, 11, 22, 0) == "after-hours"     # 17:00
    assert phase("AAPL", 2026, 11, 12, 3, 0) == "closed"           # 22:00
    assert phase("AAPL", 2026, 11, 14, 17, 0) == "closed"          # Saturday
    assert phase("BHP.AX", 2026, 11, 11, 1, 0) == "open"           # 12:00 Sydney
    assert phase("BHP.AX", 2026, 11, 11, 17, 0) == "closed"        # 04:00 Sydney next day
    sat = planner.session_info("AAPL", datetime(2026, 11, 14, 17, 0, tzinfo=UTC))
    assert sat["next_open"].startswith("2026-11-16T14:30")         # Monday 09:30 New York


def test_buy_guide_recommends_by_session_and_sizes_minimums():
    # New York is closed overnight, so some hour in the next 20 always falls outside the session.
    now = datetime.now(UTC)
    when = next(now + timedelta(hours=h) for h in range(20)
                if planner.session_info("AAPL", now + timedelta(hours=h))["phase"] == "closed")
    closed = planner.buy_guide("AAPL", when)
    assert sum(o["recommended"] for o in closed["orders"]) == 1
    rec = next(o for o in closed["orders"] if o["recommended"])
    assert rec["type"] != "Market order" and "closed" in closed["recommendation"]
    assert closed["after_entry"]["stop_loss"] < closed["price"] < closed["after_entry"]["take_profit"]
    far = planner.buy_guide("AAPL", now + timedelta(days=5))
    assert "days away" in far["recommendation"]
    assert next(o for o in far["orders"] if o["recommended"])["price"] is not None  # a resting order, never market
    m = closed["minimum_equity"]
    assert m["one_parcel_shares"] == 1 and m["one_parcel"] == pytest.approx(closed["price"])
    assert m["for_risk_rule"] > m["one_parcel"]

    asx = planner.buy_guide("BHP.AX")
    assert asx["minimum_equity"]["one_parcel"] >= 500 and asx["option"]["available"] is False
    by_type = {o["type"]: o["price"] for o in asx["orders"]}
    assert by_type["Limit order on a pullback"] < asx["price"] < by_type["Buy-stop order on a breakout"]


def test_option_suggestion_is_a_sensible_call():
    o = planner.buy_guide("AAPL")["option"]
    assert o["available"] and 21 <= o["days_to_expiry"] <= 90
    assert 0.45 <= o["delta"] <= 0.85
    assert o["cost"] == pytest.approx(o["premium"] * 100) == o["max_loss"]
    assert o["breakeven"] == pytest.approx(o["strike"] + o["premium"])
    assert o["loss_at_stop"] < 0 and o["price_basis"] == "ask"


def test_portfolio_scales_risk_with_target_and_is_honest(monkeypatch):
    # The fake stocks are independent random walks (beta near 0); give them a market-like beta.
    monkeypatch.setattr(planner.risk, "beta", lambda *a: 1.0)
    assert ideas.start_scan(["US"], wait=True)
    low = planner.build_portfolio(50_000, 12, 0.02, "US")       # below the cash rate
    mid = planner.build_portfolio(50_000, 12, 0.07, "US")
    wild = planner.build_portfolio(50_000, 3, 0.60, "US")       # 60% in three months
    assert low["invest_share"] == 0 and low["positions"] == []
    assert 0 < mid["invest_share"] < wild["invest_share"] <= 1
    assert wild["probability_of_target"] < 0.2 and "unlikely" in wild["verdict"]
    for p in (mid, wild):
        total = sum(x["value"] for x in p["positions"]) + p["cash"]
        assert total == pytest.approx(50_000) and p["cash"] >= 0
        assert all(x["shares"] >= 1 and x["shares"] == int(x["shares"]) for x in p["positions"])
        assert p["range_low"] < p["expected_return"] < p["range_high"]
        assert p["positions"][0]["role"] == "Core index fund"
    assert any(x["role"] == "Idea" and x["stop"] for x in wild["positions"])
    tiny = planner.build_portfolio(20, 12, 0.08, "US")
    assert tiny["positions"] == [] and tiny["cash"] == 20 and tiny["notes"]


def test_planning_api():
    g = client.get("/api/buy-guide", params={"symbol": "AAPL", "when": "2026-11-14T17:00:00Z"}).json()
    assert g["session"]["phase"] == "closed"
    assert client.get("/api/buy-guide", params={"symbol": "AAPL", "when": "soon"}).status_code == 400
    p = client.post("/api/portfolio", json={"equity": 10000, "months": 6, "target_return": 0.05, "market": "ASX"}).json()
    assert p["inputs"]["currency"] == "AUD" and p["notes"]  # no scan yet in this test
    assert client.post("/api/portfolio", json={"equity": -5, "months": 6, "target_return": 0.05}).status_code == 422
