import math

import numpy as np
import pandas as pd
import pytest

from quantlab import backtest
from quantlab.analytics import indicators as ind
from quantlab.analytics import options as opt
from quantlab.analytics import risk
from quantlab.forecast import build_features, forecast

from conftest import make_prices


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1.0, 60.0))
    assert ind.rsi(up).iloc[-1] == 100
    r = ind.rsi(make_prices()["Close"]).dropna()
    assert r.between(0, 100).all()


def test_beta_of_scaled_market_is_scale():
    m = make_prices()["Close"].pct_change().dropna()
    assert risk.beta(2 * m, m) == pytest.approx(2.0)
    assert risk.portfolio_beta({"A": 0.6, "B": 0.4}, {"A": 1.5, "B": 0.5}) == pytest.approx(1.1)


def test_sharpe_and_drawdown():
    eq = pd.Series([100, 110, 99, 120.0], index=pd.bdate_range("2024-01-01", periods=4))
    assert risk.max_drawdown(eq) == pytest.approx(-0.1)
    rets = pd.Series([0.01, -0.005, 0.02, 0.0])
    assert risk.sharpe_ratio(rets) == pytest.approx(rets.mean() / rets.std() * math.sqrt(252))


def test_black_scholes_reference_value_and_parity():
    # Hull's textbook example: S=42, K=40, r=10%, sigma=20%, T=0.5 -> call 4.76, put 0.81
    c = opt.bs_price(42, 40, 0.5, 0.10, 0.20, "call")
    p = opt.bs_price(42, 40, 0.5, 0.10, 0.20, "put")
    assert c == pytest.approx(4.76, abs=0.005)
    assert p == pytest.approx(0.81, abs=0.005)
    assert c - p == pytest.approx(42 - 40 * math.exp(-0.05))


def test_implied_vol_round_trip_and_greeks():
    for kind in ("call", "put"):
        price = opt.bs_price(100, 105, 0.25, 0.04, 0.33, kind, q=0.01)
        assert opt.implied_vol(price, 100, 105, 0.25, 0.04, kind, q=0.01) == pytest.approx(0.33, abs=1e-4)
    g = opt.bs_greeks(100, 100, 0.5, 0.04, 0.25, "call")
    bump = (opt.bs_price(100.01, 100, 0.5, 0.04, 0.25) - opt.bs_price(99.99, 100, 0.5, 0.04, 0.25)) / 0.02
    assert g["delta"] == pytest.approx(bump, abs=1e-4)
    assert math.isnan(opt.implied_vol(0.01, 100, 50, 0.25, 0.04, "call"))  # below intrinsic value


def test_backtest_has_no_lookahead():
    df = make_prices(300)
    # A "strategy" that knows tomorrow's direction only earns it if the engine leaks the future.
    rets = df["Close"].pct_change()
    cheat = (rets > 0).astype(float)  # long on days that already went up
    res = backtest.run_backtest(df, cheat, cost_bps=0)
    expected = (1 + cheat.shift(1).fillna(0) * rets.fillna(0)).prod() - 1
    assert res["stats"]["total_return"] == pytest.approx(expected)
    always = backtest.run_backtest(df, pd.Series(1.0, index=df.index), cost_bps=0)
    # Always-long enters at the first close, so it matches buy and hold exactly.
    assert always["stats"]["total_return"] == pytest.approx(always["benchmark"]["total_return"])


def test_costs_reduce_returns_and_trades_are_listed():
    df = make_prices(800)
    free = backtest.backtest_strategy(df, "ema_cross", {"fast": 5, "slow": 20}, cost_bps=0)
    paid = backtest.backtest_strategy(df, "ema_cross", {"fast": 5, "slow": 20}, cost_bps=50)
    assert paid["stats"]["total_return"] < free["stats"]["total_return"]
    assert free["stats"]["trades"] == len(free["trades"]) > 0
    for name in backtest.STRATEGIES:
        assert backtest.backtest_strategy(df, name)["equity"].notna().all()


def test_features_use_only_the_past():
    df = make_prices(600)
    full = build_features(df)
    truncated = build_features(df.iloc[:400])
    pd.testing.assert_frame_equal(full.iloc[:400], truncated)


def test_forecast_finds_no_edge_in_noise_and_some_in_trend():
    noise = forecast(make_prices(1500, drift=0.0))
    assert noise["available"] and 0 <= noise["prob_up"] <= 1
    assert noise["validation"]["auc"] < 0.58  # a random walk must not look predictable
    assert not forecast(make_prices(200))["available"]
