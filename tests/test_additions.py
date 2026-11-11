"""Tests for the daily run, resting orders, alerts, fundamentals, scanner backtest, news upgrades,
option strategies, holdings import and portfolio tracking."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantlab import alerts, daily, ideas, paper, planner, signals, strategies, tracking
from quantlab.api import app
from quantlab.config import settings
from quantlab.data import market, news
from quantlab.db import connect
from quantlab.forecast import NEWS_DAYS_NEEDED, build_features, forecast

from conftest import make_prices

client = TestClient(app)


def _backdate_orders(days=30):
    """Pretend open orders were placed in the past so existing price bars can trigger them."""
    day = (market.get_history("AAPL").index[-days]).strftime("%Y-%m-%d")
    with connect() as conn:
        conn.execute("UPDATE paper_orders SET created=? WHERE status='open'", (day,))
    return day


# ---------------------------------------------------------------- resting orders

def test_limit_order_waits_then_fills_at_or_below_its_price():
    bars = market.get_history("AAPL")
    last = float(bars["Close"].iloc[-1])
    far = paper.place_order("AAPL", "buy", 10, order_type="limit", price=last * 0.2)
    near = paper.place_order("AAPL", "buy", 10, order_type="limit", price=float(bars["Low"].iloc[-10:].min()) * 1.001)
    assert far["status"] == near["status"] == "open" and paper.account()["positions"] == []  # nothing fills same day
    _backdate_orders()
    events = paper.process_orders()
    assert [e["order"] for e in events if e["status"] == "filled"] == [near["id"]]
    trade = paper.trades()[0]
    assert trade["price"] <= near["price"] + 1e-9 and trade["qty"] == 10
    assert [o["id"] for o in paper.open_orders()] == [far["id"]]
    assert paper.cancel_order(far["id"]) and not paper.cancel_order(far["id"])
    assert any(a["kind"] == "order" for a in alerts.recent())


def test_bracket_creates_exits_and_one_cancels_the_other():
    last = market.get_quote("AAPL")["price"]
    paper.place_order("AAPL", "buy", 10, stop_loss=last * 0.5, take_profit=last * 3)
    exits = paper.open_orders()
    assert {(o["type"], o["side"]) for o in exits} == {("stop", "sell"), ("limit", "sell")}
    assert exits[0]["oco"] == exits[1]["oco"] is not None
    # Move the stop above every recent low so it must have triggered.
    with connect() as conn:
        conn.execute("UPDATE paper_orders SET price=? WHERE type='stop'", (last * 5,))
    _backdate_orders()
    paper.process_orders()
    assert paper.open_orders() == [] and paper.account()["positions"] == []
    done = {o["type"]: o["status"] for o in paper.open_orders(include_done=True)}
    assert done == {"stop": "filled", "limit": "cancelled"}


def test_order_validation_and_api():
    last = market.get_quote("AAPL")["price"]
    with pytest.raises(paper.OrderError, match="need a price"):
        paper.place_order("AAPL", "buy", 1, order_type="limit")
    with pytest.raises(paper.OrderError, match="below the entry"):
        paper.place_order("AAPL", "buy", 1, stop_loss=last * 2)
    with pytest.raises(paper.OrderError, match="only 0 held"):
        paper.place_order("AAPL", "sell", 1, order_type="stop", price=last * 0.9)
    r = client.post("/api/paper/orders", json={"symbol": "AAPL", "side": "buy", "qty": 5, "order_type": "stop", "price": last * 2})
    assert r.json()["status"] == "open"
    assert len(client.get("/api/paper/orders").json()) == 1
    assert client.delete(f"/api/paper/orders/{r.json()['id']}").status_code == 200
    assert client.delete("/api/paper/orders/999").status_code == 404


# ---------------------------------------------------------------- alerts and the daily run

def test_alerts_are_stored_and_delivery_failures_do_not_raise(monkeypatch):
    monkeypatch.setenv("QUANTLAB_NTFY_TOPIC", "secret-topic")
    sent = []
    monkeypatch.setattr(alerts, "_send_ntfy", lambda title, msg: sent.append((title, msg)))
    alerts.raise_alert("test", "AAPL", "hello")
    assert sent == [("QuantLab: AAPL", "hello")] and alerts.channels() == {"ntfy": True, "email": False}
    monkeypatch.setattr(alerts, "_send_ntfy", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    assert "ntfy delivery failed" in alerts.raise_alert("test", None, "again")["message"]
    body = client.get("/api/alerts").json()
    assert body["unread"] == 2
    client.post("/api/alerts/read")
    assert client.get("/api/alerts").json()["unread"] == 0


def test_daily_run_logs_signals_alerts_changes_and_is_scheduled(monkeypatch):
    with connect() as conn:  # yesterday the app said the opposite of whatever it will say today
        conn.execute("INSERT INTO signal_log VALUES ('AAPL', '2000-01-01', 0.9, 'No Such Label', 1.0)")
    monkeypatch.setattr(market, "get_fundamentals", lambda s: {"days_to_earnings": 2, "next_earnings": "soon"})
    summary = daily.run_daily(scan=True)
    assert summary["signals"] == len(settings.default_watchlist) and summary["errors"] == []
    assert "AAPL" in summary["changes"] and summary["ideas"] >= 0
    kinds = [a["kind"] for a in alerts.recent()]
    assert "signal" in kinds and "earnings" in kinds
    before = len(alerts.recent())
    daily.run_daily(scan=False)
    assert sum(a["kind"] == "earnings" for a in alerts.recent()) == kinds.count("earnings")  # not repeated
    assert len(alerts.recent()) == before

    now = datetime.now()
    monkeypatch.setattr(settings, "daily_time", (now - timedelta(minutes=5)).strftime("%H:%M"))
    assert daily.is_due(now) is False                          # already ran after today's time
    monkeypatch.setattr(daily, "last_run", lambda: {"started": (now - timedelta(days=3)).isoformat()})
    assert daily.is_due(now) is True
    monkeypatch.setattr(settings, "daily_time", "off")
    assert daily.is_due(now) is False


# ---------------------------------------------------------------- fundamentals and earnings

def test_fundamentals_and_earnings_warnings():
    f = market.get_fundamentals("AAPL")
    assert f["days_to_earnings"] == 5 and f["pe"] == 25.0
    g = planner.buy_guide("AAPL")
    assert "Earnings are due" in g["earnings_warning"]
    assert any("Earnings fall inside" in w for w in g["option"]["warnings"])
    assert client.get("/api/fundamentals", params={"symbol": "AAPL"}).json()["sector"] == "Technology"


# ---------------------------------------------------------------- scanner backtest and universe

def test_score_series_matches_the_single_day_score():
    df = make_prices(700, seed=11)
    series = ideas.score_series(df)
    for cut in (300, 500, 700):
        assert series.iloc[cut - 1] == pytest.approx(ideas.quick_score(df.iloc[:cut])["score"], abs=1e-9)


def test_scanner_backtest_runs_without_lookahead():
    res = ideas.backtest_scanner("US")
    assert set(res["stats"]) >= {"strategy", "universe"} and 0 < res["avg_holdings"] <= res["top_n"]
    assert all(np.isfinite(res["stats"][k]["cagr"]) for k in res["stats"])
    # On random walks no selection rule should beat the universe by much.
    assert abs(res["beat_universe"]) < 0.15
    api = client.get("/api/ideas/backtest", params={"market_name": "ASX"}).json()
    assert api["curves"]["strategy"][0]["value"] == pytest.approx(1, abs=0.1)


def test_scan_filters_by_sector_and_group():
    assert ideas.start_scan(["US"], ("large", "mid", "etf"), "Technology", wait=True)
    res = ideas.latest()
    assert res["universe_size"] == len(ideas.universe("US", ("large", "mid"), "Technology"))
    assert {r["sector"] for r in res["table"]} == {"Technology"}
    assert client.post("/api/ideas/scan", params={"sector": "Astrology"}).status_code == 400


# ---------------------------------------------------------------- news upgrades

def test_ai_news_scoring_overrides_word_scores_and_drops_irrelevant(monkeypatch):
    monkeypatch.setattr(settings, "ai_news", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(news, "_ask_claude", lambda symbol, name, rows: [
        SimpleNamespace(sentiment=0.9, relevant=True), SimpleNamespace(sentiment=0.0, relevant=False)])
    items = news.get_news("AAPL")
    assert len(items) == 1 and items[0]["sentiment"] == 0.9 and items[0]["ai"] == 1
    monkeypatch.setattr(news, "_ask_claude", lambda *a: (_ for _ in ()).throw(RuntimeError("api down")))
    news._last_fetch.clear()
    assert len(news.get_news("MSFT")) == 2  # falls back to word scores


def test_news_feature_joins_the_model_only_with_enough_history():
    df = make_prices(900)
    few = pd.Series(0.1, index=df.index[-10:])
    assert "news_5d" not in build_features(df, news_daily=few)
    many = pd.Series(np.linspace(-1, 1, NEWS_DAYS_NEEDED + 5), index=df.index[-(NEWS_DAYS_NEEDED + 5):])
    feats = build_features(df, news_daily=many)
    assert feats["news_5d"].notna().sum() >= NEWS_DAYS_NEEDED and feats["news_5d"].iloc[:100].isna().all()
    # A bar never sees its own day's headlines: the first value appears the day after the first headline.
    assert pd.isna(feats["news_5d"].loc[many.index[0]])
    fc = forecast(df, news_daily=many)
    assert fc["available"] and fc["news_feature"] == {"used": True, "days_archived": len(many), "days_needed": NEWS_DAYS_NEEDED}
    assert forecast(df)["news_feature"]["used"] is False


# ---------------------------------------------------------------- option strategies

def test_option_strategies_have_consistent_payoffs():
    res = strategies.option_strategies("AAPL")
    by = {s["name"]: s for s in res["strategies"]}
    assert set(by) == {"Long call", "Bull call spread", "Long put", "Bear put spread", "Covered call", "Protective put"}
    call = by["Long call"]
    assert call["max_profit"] is None and call["max_loss"] == pytest.approx(call["legs"][0]["premium"] * 100)
    assert call["breakevens"] == [pytest.approx(call["legs"][0]["strike"] + call["legs"][0]["premium"], abs=0.02)]
    spread = by["Bull call spread"]
    lo, hi = spread["legs"]
    assert spread["max_loss"] == pytest.approx(spread["option_cost"]) and spread["max_loss"] < call["max_loss"]
    assert spread["max_profit"] == pytest.approx((hi["strike"] - lo["strike"]) * 100 - spread["option_cost"])
    put = by["Protective put"]
    assert put["shares"] == 100 and put["max_profit"] is None
    assert put["max_loss"] == pytest.approx((res["spot"] - put["legs"][0]["strike"] + put["legs"][0]["premium"]) * 100)
    assert by["Covered call"]["max_profit"] is not None and len(call["payoff"]) == 61
    assert strategies.option_strategies("BHP.AX")["available"] is False
    assert client.get("/api/options/strategies", params={"symbol": "AAPL"}).json()["available"]


# ---------------------------------------------------------------- holdings and tracking

def test_holdings_import_is_forgiving_and_reports_bad_rows():
    csv = "Ticker;Units;Avg Price\nAAPL;10;100\nmsft;5;\nNOPE;3;1\nAAPL;10;200\n;;\nTSLA;0;5\n"
    res = tracking.import_holdings(csv)
    assert res["imported"] == 2 and len(res["skipped"]) == 2
    s = tracking.holdings_summary()
    aapl = next(h for h in s["holdings"] if h["symbol"] == "AAPL")
    assert aapl["qty"] == 20 and aapl["cost"] == 150 and aapl["pnl"] is not None
    assert next(h for h in s["holdings"] if h["symbol"] == "MSFT")["pnl"] is None
    assert s["books"][0]["currency"] == "USD" and sum(h["weight"] for h in s["holdings"]) == pytest.approx(1)
    assert tracking.import_holdings("Code,Shares\nBHP,100\n", "ASX")["imported"] == 1
    assert tracking.held_shares() == {"BHP.AX": 100}
    with pytest.raises(ValueError, match="ticker and quantity columns"):
        tracking.import_holdings("a,b\n1,2\n")
    r = client.post("/api/holdings/import", json={"csv": "Symbol,Quantity\nAAPL,3\n"})
    assert r.json()["imported"] == 1 and client.get("/api/holdings").json()["holdings"][0]["symbol"] == "AAPL"


def test_builder_counts_shares_already_owned(monkeypatch):
    monkeypatch.setattr(planner.risk, "beta", lambda *a: 1.0)
    tracking.import_holdings("Symbol,Quantity\nIVV,2\n")
    p = planner.build_portfolio(50_000, 12, 0.08, "US")
    core = p["positions"][0]
    assert core["symbol"] == "IVV" and core["held"] == 2 and core["to_buy"] == core["shares"] - 2
    assert any("already own" in n for n in p["notes"])


def test_saved_portfolio_is_tracked_against_target_and_index(monkeypatch):
    monkeypatch.setattr(planner.risk, "beta", lambda *a: 1.0)
    body = {"equity": 50_000, "months": 6, "target_return": 0.05, "market": "US", "name": "Test"}
    pid = client.post("/api/portfolios", json=body).json()["id"]
    assert client.get("/api/portfolios").json()[0]["name"] == "Test"
    t = client.get(f"/api/portfolios/{pid}").json()
    assert t["return"] == pytest.approx(0, abs=1e-6) and t["days_elapsed"] == 0 and t["suggestions"]
    # Rewind the save date and mark the position as bought far above today's price.
    with connect() as conn:
        row = conn.execute("SELECT data FROM portfolios WHERE id=?", (pid,)).fetchone()
        data = json.loads(row["data"])
        data["positions"][0]["price"] *= 2
        data["positions"][0]["stop"] = data["positions"][0]["price"]
        conn.execute("UPDATE portfolios SET data=?, created=? WHERE id=?",
                     (json.dumps(data), (datetime.now() - timedelta(days=60)).astimezone().isoformat(), pid))
    t = client.get(f"/api/portfolios/{pid}").json()
    assert t["days_elapsed"] == 60 and len(t["history"]) > 20 and t["index_return"] is not None
    assert t["positions"][0]["stop_hit"] and any(s.startswith("Sell") for s in t["suggestions"])
    assert 0 < t["on_track_return"] < 0.05
    assert client.delete(f"/api/portfolios/{pid}").status_code == 200
    assert client.get(f"/api/portfolios/{pid}").status_code == 404


# ---------------------------------------------------------------- brokers

CHEAP_US = {"base_currency": "AUD", "asx_stocks": False, "us_options": True, "us_fixed": 1.0, "fx_pct": 0.5,
            "option_per_contract": 0.65, "conditional_orders": False}
LOCAL = {"base_currency": "AUD", "us_stocks": True, "asx_fixed": 5.0, "asx_pct": 0.1, "asx_min": 10.0,
         "us_fixed": 10.0, "fx_pct": 0.7}


def test_broker_costs_and_recommendation():
    from quantlab import brokers

    assert brokers.recommend("AAPL")["available"] is False
    brokers.save_broker("GlobalX", CHEAP_US)
    brokers.save_broker("HomeBank", LOCAL)
    with pytest.raises(brokers.BrokerError):
        brokers.save_broker("Bad", {"us_fixed": -1})
    us = brokers.recommend("AAPL", "buy", 10_000)
    costs = {o["broker"]: o["total"] for o in us["options"]}
    assert costs == {"GlobalX": pytest.approx(1 + 50), "HomeBank": pytest.approx(10 + 70)}
    assert us["recommended"] == "GlobalX" and "cheapest" in us["why"]
    asx = brokers.recommend("BHP.AX", "buy", 2_000)       # 5 + 0.1% = 7 -> minimum 10, no conversion
    assert asx["recommended"] == "HomeBank" and asx["options"][0]["total"] == pytest.approx(10)
    assert any("GlobalX does not trade ASX shares" in e for e in asx["excluded"])
    opt = brokers.recommend("AAPL", "buy", 500, "option", 3)
    assert opt["recommended"] == "GlobalX" and opt["options"][0]["commission"] == pytest.approx(1.95)
    assert brokers.recommend("BHP.AX", "buy", 500, "option")["available"] is False
    # A plan that needs stop orders prefers the broker that has them when the cost is close.
    brokers.save_broker("HomeBank", {**LOCAL, "us_fixed": 2.0, "fx_pct": 0.5})
    assert brokers.recommend("AAPL", "buy", 10_000, needs_conditional=True)["recommended"] == "HomeBank"


def test_holdings_split_by_broker_and_selling_where_held():
    from quantlab import brokers

    brokers.save_broker("GlobalX", CHEAP_US)
    brokers.save_broker("HomeBank", LOCAL)
    tracking.import_holdings("Symbol,Quantity,Cost\nAAPL,10,100\nMSFT,5,50\n", broker="GlobalX")
    tracking.import_holdings("Symbol,Quantity\nAAPL,4\n", broker="HomeBank")
    tracking.set_holding("BHP.AX", 100, 40, "HomeBank")
    s = tracking.holdings_summary()
    assert tracking.held_shares()["AAPL"] == 14 and len(s["holdings"]) == 4
    usd = [b for b in s["by_broker"] if b["currency"] == "USD"]
    assert {b["broker"] for b in usd} == {"GlobalX", "HomeBank"} and sum(b["share"] for b in usd) == pytest.approx(1)
    # Re-importing one broker's file leaves the other broker's holdings alone.
    tracking.import_holdings("Symbol,Quantity\nTSLA,1\n", broker="GlobalX")
    assert tracking.held_shares() == {"AAPL": 4, "BHP.AX": 100, "TSLA": 1}
    sell = brokers.recommend("AAPL", "sell", 1_000)
    assert [o["broker"] for o in sell["options"]] == ["HomeBank"] and "hold" in sell["options"][0]["notes"][0]
    tracking.set_holding("TSLA", 0, broker="GlobalX")
    assert "TSLA" not in tracking.held_shares()

    r = client.post("/api/brokers", json={"name": "Third", "data": {"us_fixed": 3}})
    assert r.json()["us_fixed"] == 3 and len(client.get("/api/brokers").json()["brokers"]) == 3
    rec = client.get("/api/brokers/recommend", params={"symbol": "AAPL", "value": 5000}).json()
    assert rec["available"] and len(rec["options"]) == 3
    assert client.delete("/api/brokers", params={"name": "Third"}).status_code == 200
    guide = planner.buy_guide("AAPL")
    assert guide["broker"]["available"] and guide["option_broker"]["recommended"] == "GlobalX"
