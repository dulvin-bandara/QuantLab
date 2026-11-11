"""Trade ideas: scan a universe of liquid stocks and funds and shortlist candidates.

The scan has two stages so it finishes in a minute or two:

1. A fast screen of every name in the chosen universe on trend and momentum
   alone (one bulk price download).
2. The full signal (trend, momentum, ML forecast, news) for the best few from
   stage 1. Only those the full signal still rates Buy or better become ideas.
   The weakest US names get the same treatment to produce bearish ideas, which
   can be acted on with put options.

The universe is fixed lists of large and mid-sized US and ASX companies and
popular index funds, grouped by sector. Ideas are a shortlist to look into,
picked mostly on trend and momentum; they are not a prediction and not advice.
``backtest_scanner`` replays the stage-1 screen over past years to show how
that selection rule would have done.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import signals
from .analytics import indicators as ind
from .analytics import risk
from .data import market
from .db import connect

# market -> size group -> sector -> tickers
UNIVERSE = {
    "US": {
        "large": {
            "Technology": "AAPL MSFT NVDA AVGO ORCL CRM AMD ADBE ACN CSCO IBM QCOM TXN INTU NOW AMAT MU ADI PANW INTC PLTR CRWD SNOW SHOP",
            "Communication": "GOOGL META NFLX DIS CMCSA VZ T TMUS RDDT",
            "Consumer": "AMZN TSLA HD MCD LOW BKNG TJX SBUX NKE ABNB F GM UBER COST PG WMT KO PEP PM MO CL",
            "Health Care": "LLY UNH JNJ ABBV MRK TMO ABT DHR AMGN ISRG PFE SYK VRTX BSX MDT GILD BMY MCK",
            "Financials": "BRK-B JPM V MA BAC WFC GS SPGI AXP PGR BLK MS C CB SCHW ICE CME PYPL COIN",
            "Industrials": "GE CAT RTX HON UNP ETN LMT ADP BA DE UPS",
            "Energy & Utilities": "XOM CVX COP NEE SO DUK",
            "Materials": "LIN SHW",
            "Real Estate": "PLD AMT EQIX",
        },
        "mid": {
            "Technology": "DDOG NET ZS OKTA TWLO HUBS MDB DOCU ZM U PATH",
            "Communication": "ROKU PINS SNAP",
            "Consumer": "ETSY DKNG CHWY W RIVN LCID DASH LULU DECK CROX WSM CELH ELF",
            "Health Care": "EXEL HALO INCY NBIX",
            "Financials": "SOFI HOOD AFRM ALLY",
            "Industrials": "AXON SAIA GNRC",
            "Energy & Utilities": "FSLR ENPH DVN FANG OVV",
            "Materials": "CLF AA",
        },
        "etf": {"Funds": "SPY QQQ IWM DIA IVV VTI VOO XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC SMH GLD SLV TLT HYG EEM EFA ARKK"},
    },
    "ASX": {
        "large": {
            "Financials": "CBA NAB WBC ANZ MQG QBE SUN IAG ASX MPL CPU",
            "Materials": "BHP RIO FMG S32 MIN PLS EVN NST JHX AMC BSL LYC",
            "Health Care": "CSL RMD COH SHL FPH PME",
            "Energy & Utilities": "WDS STO ORG APA AGL ALD",
            "Consumer": "WES WOW COL ALL TLC JBH TWE QAN",
            "Real Estate": "GMG SCG SGP",
            "Technology": "XRO WTC NXT CAR SEK REA",
            "Industrials": "TCL BXB",
            "Communication": "TLS",
        },
        "mid": {
            "Financials": "BEN BOQ NWL HUB PNI",
            "Materials": "IGO ILU SFR ORI",
            "Health Care": "ANN",
            "Energy & Utilities": "BPT NHC WHC WOR",
            "Consumer": "A2M ARB DMP FLT HVN LOV APE IEL",
            "Real Estate": "CHC DXS GPT MGR LLC",
            "Technology": "TNE",
            "Industrials": "ALX CWY ALQ",
        },
        "etf": {"Funds": "VAS A200 IOZ VGS NDQ IVV VHY STW VTS ETHI"},
    },
}
GROUP_LABELS = {"large": "Large companies", "mid": "Mid-sized and growth companies", "etf": "Index and sector funds"}
SHORTLIST = 8            # per market: how many stage-1 leaders get the full signal
BEAR_SHORTLIST = 4       # US only: how many of the weakest get the full signal
MIN_BARS = 220           # need a 200-day average
MIN_DOLLAR_VOLUME = 5e6  # average daily traded value, in the stock's own currency

_state = {"status": "idle", "stage": "", "done": 0, "total": 0, "error": None}
_lock = threading.Lock()


def universe(market_name: str, groups=("large",), sector: str | None = None) -> dict[str, dict]:
    """Tickers for a market as {symbol: {"sector", "group"}}."""
    out = {}
    for group in groups:
        for sec, tickers in UNIVERSE[market_name].get(group, {}).items():
            if sector and sec != sector:
                continue
            for t in tickers.split():
                out[t + ".AX" if market_name == "ASX" else t] = {"sector": sec, "group": group}
    return out


def sectors() -> list[str]:
    return sorted({sec for m in UNIVERSE.values() for g in m.values() for sec in g})


def score_series(df: pd.DataFrame) -> pd.Series:
    """The stage-1 score for every day in ``df`` at once (same rule as ``quick_score``)."""
    c = df["Close"]
    sma50, sma200 = ind.sma(c, 50), ind.sma(c, 200)
    ema20, ema50 = ind.ema(c, 20), ind.ema(c, 50)
    sign = lambda s: np.sign(s).fillna(0.0)
    two = (sign(c - sma50) + sign(ema20 - ema50)) / 2
    four = (sign(c - sma50) + sign(ema20 - ema50) + sign(c - sma200) + sign(sma50 - sma200)) / 4
    trend = four.where(sma200.notna(), two)

    rsi = ind.rsi(c)
    hist = ind.macd(c)["hist"]
    vol = np.log(c).diff().rolling(60).std()
    z = ((c / c.shift(20) - 1) / (vol * np.sqrt(20))).where(vol > 0).fillna(0.0)
    momentum = (((rsi - 50) / 25).clip(-1, 1).fillna(0.0) + sign(hist) + (z / 2).clip(-1, 1)) / 3
    return 0.6 * trend + 0.4 * momentum


def quick_score(df: pd.DataFrame) -> dict:
    """Stage-1 score from price alone: 60% trend, 40% momentum, each from -1 to +1."""
    trend, _ = signals.trend_component(df)
    momentum, _ = signals.momentum_component(df)
    close = df["Close"]
    return {
        "score": 0.6 * trend + 0.4 * momentum,
        "trend": trend,
        "momentum": momentum,
        "price": float(close.iloc[-1]),
        "rsi": float(ind.rsi(close).iloc[-1]),
        "ret_20": float(close.iloc[-1] / close.iloc[-21] - 1),
        "ret_120": float(close.iloc[-1] / close.iloc[-121] - 1),
        "dollar_volume": float((close * df["Volume"]).tail(20).mean()),
    }


def _reasons(sig: dict) -> list[str]:
    out = [sig["components"]["trend"]["note"], sig["components"]["momentum"]["note"]]
    ml = sig["components"].get("ml")
    if ml and abs(ml["score"]) > 0.01:
        out.append(ml["note"])
    nw = sig["components"].get("news")
    if nw:
        out.append(nw["note"])
    return out


def _idea(sym: str, quick: dict, sig: dict) -> dict:
    fc = sig["forecast"] or {}
    fund = market.get_fundamentals(sym)
    reasons = _reasons(sig)
    days = fund.get("days_to_earnings")
    if days is not None and days <= 14:
        reasons.append(f"Earnings are due in {days} day{'s' if days != 1 else ''} ({fund['next_earnings']}): results can "
                       "move the price sharply either way, whatever the trend says.")
    return {
        "symbol": sym, "name": market.get_name(sym), "market": quick["market"], "sector": quick["sector"],
        "group": quick["group"], "currency": sig["currency"], "price": sig["price"], "as_of": sig["as_of"],
        "score": sig["score"], "label": sig["label"], "quick_score": quick["score"], "rsi": quick["rsi"],
        "ret_20": quick["ret_20"], "ret_120": quick["ret_120"],
        "prob_up": fc.get("prob_up") if fc.get("available") else None,
        "news_score": sig["news"]["score"] if sig["news"] and sig["news"]["count"] else None,
        "pe": fund.get("pe"), "next_earnings": fund.get("next_earnings"), "days_to_earnings": days,
        "plan": sig["plan"], "reasons": reasons,
    }


def run_scan(markets: list[str], groups=("large",), sector: str | None = None) -> dict:
    """Scan the chosen markets and return ranked ideas plus the full stage-1 table."""
    markets = [m for m in markets if m in UNIVERSE] or list(UNIVERSE)
    groups = [g for g in groups if g in GROUP_LABELS] or ["large"]
    members = {m: universe(m, groups, sector) for m in markets}
    size = sum(len(v) for v in members.values())
    screened, missing = [], 0
    leaders: list[dict] = []
    laggards: list[dict] = []
    _state.update(stage="Screening prices", done=0, total=size)
    for m in markets:
        data = market.get_history_bulk(list(members[m]))
        missing += len(members[m]) - len(data)
        rows = []
        for sym, df in data.items():
            if len(df) < MIN_BARS:
                missing += 1
                continue
            q = quick_score(df)
            if not np.isfinite(q["score"]) or q["dollar_volume"] < MIN_DOLLAR_VOLUME:
                continue
            rows.append({"symbol": sym, "market": m, "currency": market.currency_of(sym),
                         "as_of": df.index[-1].strftime("%Y-%m-%d"), **members[m][sym], **q})
        rows.sort(key=lambda r: r["score"], reverse=True)
        screened += rows
        leaders += [r for r in rows[:SHORTLIST] if r["score"] >= 0.2]
        if m == "US":  # bearish ideas need put options, which only US names have here
            laggards += [r for r in rows[::-1][:BEAR_SHORTLIST] if r["score"] <= -0.2 and r["group"] != "etf"]
        _state["done"] += len(members[m])

    ideas, bearish = [], []
    _state.update(stage="Running the full signal on the leaders", done=0, total=len(leaders) + len(laggards))
    for quick, bullish in [(r, True) for r in leaders] + [(r, False) for r in laggards]:
        try:
            sig = signals.compute_signal(quick["symbol"])
        except Exception:  # a bad ticker should not sink the scan
            continue
        finally:
            _state["done"] += 1
        if bullish and sig["score"] >= 0.2:
            ideas.append(_idea(quick["symbol"], quick, sig))
        elif not bullish and sig["score"] <= -0.2:
            bearish.append(_idea(quick["symbol"], quick, sig))
    ideas.sort(key=lambda r: r["score"], reverse=True)
    bearish.sort(key=lambda r: r["score"])
    screened.sort(key=lambda r: r["score"], reverse=True)
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "markets": markets, "groups": groups, "sector": sector, "universe_size": size,
        "screened": len(screened), "missing": missing, "shortlisted": len(leaders) + len(laggards),
        "ideas": ideas, "bearish": bearish, "table": screened,
    }


def _worker(markets, groups, sector) -> None:
    from .api import clean

    try:
        result = clean(run_scan(markets, groups, sector))
        previous = latest()
        with connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ideas', ?)", (json.dumps(result),))
        _state.update(status="idle", stage="", error=None)
        if previous:
            from . import alerts

            seen = {i["symbol"] for i in previous["ideas"]}
            for i in result["ideas"]:
                if i["symbol"] not in seen:
                    alerts.raise_alert("idea", i["symbol"], f"New idea: {i['symbol']} ({i['name']}) is rated {i['label']} "
                                       f"at {i['price']:.2f} {i['currency']}.")
    except Exception as exc:
        _state.update(status="idle", stage="", error=f"The scan failed: {exc}")


def start_scan(markets: list[str], groups=("large",), sector: str | None = None, wait: bool = False) -> bool:
    """Start a scan in the background. Returns False if one is already running."""
    with _lock:
        if _state["status"] == "running":
            return False
        _state.update(status="running", stage="Starting", done=0, total=0, error=None)
    thread = threading.Thread(target=_worker, args=(markets, groups, sector), daemon=True)
    thread.start()
    if wait:
        thread.join()
    return True


def latest() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='ideas'").fetchone()
    res = json.loads(row["value"]) if row else None
    if res:
        res.setdefault("bearish", [])  # scans saved by older versions
    return res


def status() -> dict:
    return {**_state, "result": latest()}


# ---------------------------------------------------------------- does the scanner work?

def backtest_scanner(market_name: str = "US", top_n: int = SHORTLIST, hold_days: int = 5, cost_bps: float = 10.0) -> dict:
    """Replay the stage-1 screen over about four years.

    Every ``hold_days`` trading days it buys the ``top_n`` highest-scoring large companies (score
    of 0.2 or more) in equal amounts, using only prices known at that time, and holds them until
    the next review. It is compared with holding every company in the universe equally, and with
    the market index.

    Two honest limits: only the price screen is replayed (the forecast and news parts of the
    full signal cannot be reconstructed for the past), and the universe is *today's* biggest
    companies, which flatters all three lines; the fair comparison is against the universe.
    """
    if market_name not in UNIVERSE:
        raise ValueError("Market must be US or ASX.")
    data = market.get_history_bulk(list(universe(market_name)), period="5y")
    data = {s: df for s, df in data.items() if len(df) >= MIN_BARS + 60}
    if len(data) < top_n * 2:
        raise ValueError("Not enough price history could be loaded to run the test.")
    closes = pd.DataFrame({s: df["Close"] for s, df in data.items()}).sort_index().ffill(limit=3)
    scores = pd.DataFrame({s: score_series(df) for s, df in data.items()}).reindex(closes.index).ffill(limit=3)
    rets = closes.pct_change().fillna(0.0)
    dates = closes.index[MIN_BARS:]

    weights = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    current = pd.Series(0.0, index=closes.columns)
    held_counts, turnover = [], pd.Series(0.0, index=closes.index)
    for i, day in enumerate(dates):
        if i % hold_days == 0:  # review at this day's close
            row = scores.loc[day].dropna()
            picks = row[row >= 0.2].nlargest(top_n).index
            new = pd.Series(0.0, index=closes.columns)
            if len(picks):
                new[picks] = 1 / len(picks)
            turnover[day] = (new - current).abs().sum()
            current = new
            held_counts.append(len(picks))
        weights.loc[day] = current
    held = weights.shift(1).fillna(0.0)             # yesterday's decision earns today's return
    strat = (held * rets).sum(axis=1) - turnover.shift(1).fillna(0.0) * cost_bps / 1e4
    strat, uni = strat.loc[dates[1]:], rets.loc[dates[1]:].mean(axis=1)
    out = {"strategy": (1 + strat).cumprod(), "universe": (1 + uni).cumprod()}
    try:
        idx = market.get_history(market.benchmark_of(next(iter(data))))["Close"]
        idx = idx.reindex(strat.index).ffill().dropna()
        out["index"] = idx / idx.iloc[0]
    except market.DataError:
        pass
    stats = {k: risk.summary(v) for k, v in out.items()}
    return {
        "market": market_name, "from": strat.index[0].strftime("%Y-%m-%d"), "to": strat.index[-1].strftime("%Y-%m-%d"),
        "stocks": len(data), "top_n": top_n, "hold_days": hold_days, "cost_bps": cost_bps,
        "avg_holdings": float(np.mean(held_counts)), "avg_turnover_per_review": float(turnover[turnover > 0].mean() or 0),
        "stats": stats, "curves": out,
        "beat_universe": stats["strategy"]["cagr"] - stats["universe"]["cagr"],
    }
