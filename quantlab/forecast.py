"""Machine-learning forecast of short-horizon direction.

A gradient-boosted classifier estimates the probability that a stock closes
higher ``horizon`` trading days from now, from price, volume, volatility and
broad-market features.

The honest part is the scoring: the model is graded only on periods it never
trained on (walk-forward validation, with a gap so training labels cannot
overlap the test window). Daily direction is close to a coin flip for most
stocks, so expect accuracy near 50% and treat the reported skill, not the
probability alone, as the thing to trust.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .analytics import indicators as ind

MIN_ROWS = 500
NEWS_DAYS_NEEDED = 60  # days of archived headlines before news sentiment is used as a model input


def build_features(df: pd.DataFrame, market: pd.DataFrame | None = None,
                   news_daily: pd.Series | None = None) -> pd.DataFrame:
    c, v = df["Close"], df["Volume"]
    logc = np.log(c)
    f = pd.DataFrame(index=df.index)
    for n in (1, 5, 10, 20, 60):
        f[f"ret_{n}"] = logc.diff(n)
    f["vol_10"] = logc.diff().rolling(10).std()
    f["vol_20"] = logc.diff().rolling(20).std()
    f["vol_ratio"] = f["vol_10"] / logc.diff().rolling(60).std()
    f["rsi_14"] = ind.rsi(c, 14) / 100
    f["macd_hist"] = ind.macd(c)["hist"] / c
    for n in (20, 50, 200):
        f[f"dist_sma_{n}"] = c / ind.sma(c, n) - 1
    bb = ind.bollinger(c)
    f["bb_pos"] = (c - bb["lower"]) / (bb["upper"] - bb["lower"])
    f["atr_pct"] = ind.atr(df) / c
    f["range_pos"] = (c - df["Low"].rolling(20).min()) / (df["High"].rolling(20).max() - df["Low"].rolling(20).min())
    vol_sd = v.rolling(60).std()
    f["volume_z"] = ((v - v.rolling(60).mean()) / vol_sd.where(vol_sd > 0)).clip(-5, 5)
    if market is not None and len(market):
        m = np.log(market["Close"]).reindex(df.index).ffill()
        f["mkt_ret_5"] = m.diff(5)
        f["mkt_ret_20"] = m.diff(20)
        f["mkt_vol_20"] = m.diff().rolling(20).std()
        f["rel_ret_20"] = f["ret_20"] - f["mkt_ret_20"]
    if news_daily is not None and len(news_daily) >= NEWS_DAYS_NEEDED:
        # Average sentiment of the last 5 days with news. Days before the archive began stay empty,
        # which the model handles natively.
        # Shifted a day so a bar only ever sees headlines from before it.
        lagged = news_daily.copy()
        lagged.index = lagged.index + pd.Timedelta(days=1)
        s = lagged.reindex(lagged.index.union(df.index)).rolling("5D", min_periods=1).mean()
        f["news_5d"] = s.reindex(df.index)
    return f.replace([np.inf, -np.inf], np.nan)


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier

    # Deliberately small and regularised: financial data is mostly noise.
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=120,
                                          l2_regularization=1.0, min_samples_leaf=40, random_state=0)


def _fit(X: pd.DataFrame, y: pd.Series):
    """Fit on the columns that have any data in this training window (a new feature such as news
    sentiment is empty for the early years)."""
    cols = [c for c in X.columns if X[c].notna().any()]
    return _model().fit(X[cols], y), cols


def forecast(df: pd.DataFrame, market: pd.DataFrame | None = None, horizon: int = 5, folds: int = 5,
             news_daily: pd.Series | None = None) -> dict:
    """Train, validate walk-forward and predict the latest bar."""
    from sklearn.metrics import roc_auc_score

    if len(df) < MIN_ROWS:
        return {"available": False, "reason": f"Needs at least {MIN_ROWS} days of history; {len(df)} available."}

    X = build_features(df, market, news_daily)
    news_days = 0 if news_daily is None else len(news_daily)
    fwd = df["Close"].shift(-horizon) / df["Close"] - 1
    y = (fwd > 0).astype(float).where(fwd.notna())
    usable = X.dropna(thresh=int(X.shape[1] * 0.8)).index
    X = X.loc[usable]
    labelled = X.index[y.loc[X.index].notna()]
    Xl, yl, fl = X.loc[labelled], y.loc[labelled], fwd.loc[labelled]
    n = len(Xl)
    if n < MIN_ROWS - 220:
        return {"available": False, "reason": "Not enough history after computing features."}

    # Walk forward: train on everything up to a point, test on the next block, repeat.
    start = n // 2
    edges = np.linspace(start, n, folds + 1).astype(int)
    oos = pd.Series(np.nan, index=Xl.index)
    for a, b in zip(edges[:-1], edges[1:]):
        train_end = a - horizon  # gap: the last training labels must not peek into the test block
        if train_end < 200 or yl.iloc[:train_end].nunique() < 2:
            continue
        m, cols = _fit(Xl.iloc[:train_end], yl.iloc[:train_end])
        oos.iloc[a:b] = m.predict_proba(Xl.iloc[a:b][cols])[:, 1]

    scored = oos.dropna()
    yt, ft = yl.loc[scored.index], fl.loc[scored.index]
    auc = float(roc_auc_score(yt, scored)) if yt.nunique() == 2 else 0.5
    base_rate = float(yt.mean())
    accuracy = float(((scored > 0.5) == (yt > 0.5)).mean())
    confident = scored > 0.55
    # Skill 0 = no better than chance, 1 = AUC of 0.60 or more (strong for daily equity data).
    skill = float(np.clip((auc - 0.5) / 0.10, 0.0, 1.0))

    final, cols = _fit(Xl, yl)
    prob_up = float(final.predict_proba(X.iloc[[-1]][cols])[:, 1][0])
    return {
        "available": True,
        "horizon": horizon,
        "as_of": X.index[-1].strftime("%Y-%m-%d"),
        "prob_up": prob_up,
        "skill": skill,
        "news_feature": {"used": "news_5d" in X.columns, "days_archived": news_days, "days_needed": NEWS_DAYS_NEEDED},
        "validation": {
            "samples": int(len(scored)),
            "from": scored.index[0].strftime("%Y-%m-%d"),
            "to": scored.index[-1].strftime("%Y-%m-%d"),
            "auc": auc,
            "accuracy": accuracy,
            "always_up_accuracy": max(base_rate, 1 - base_rate),
            "base_rate_up": base_rate,
            "avg_return_all": float(ft.mean()),
            "avg_return_when_confident": float(ft[confident].mean()) if confident.any() else None,
            "confident_share": float(confident.mean()),
        },
    }
