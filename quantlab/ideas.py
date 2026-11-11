"""Trade ideas: scan a universe of large, liquid stocks and shortlist buy candidates.

The scan has two stages so it finishes in about a minute:

1. A fast screen of every stock in the universe on trend and momentum alone
   (one bulk price download).
2. The full signal (trend, momentum, ML forecast, news) for the best few from
   stage 1. Only those the full signal still rates Buy or better become ideas.

The universe is fixed lists of the largest US and ASX companies, so every
result is liquid enough to trade. Ideas are a shortlist to look into, picked
mostly on trend and momentum; they are not a prediction and not advice.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import numpy as np

from . import signals
from .analytics import indicators as ind
from .data import market
from .db import connect

US_UNIVERSE = """AAPL MSFT NVDA GOOGL AMZN META TSLA AVGO BRK-B LLY JPM V UNH XOM MA COST HD PG JNJ WMT NFLX
ABBV BAC CRM ORCL CVX KO MRK AMD PEP ADBE TMO LIN ACN MCD CSCO ABT WFC GE IBM DHR QCOM TXN PM INTU CAT VZ DIS
AMGN NOW ISRG PFE CMCSA NEE GS RTX SPGI UBER T LOW AMAT HON UNP BKNG AXP PGR BLK COP TJX SYK MS ETN VRTX C
BSX LMT ADP MDT PANW BA MU CB ADI GILD DE SBUX PLD AMT BMY SCHW TMUS SO MO NKE INTC UPS CL DUK
PYPL SHW ICE CME EQIX MCK PLTR CRWD COIN SHOP SNOW ABNB F GM RDDT""".split()

ASX_UNIVERSE = [s + ".AX" for s in """BHP CBA CSL NAB WBC ANZ WES MQG GMG FMG WDS TLS RIO WOW TCL ALL QBE STO
COL REA BXB SUN ORG XRO NST JHX IAG SCG RMD CPU S32 COH WTC SHL ASX QAN APA MPL TLC AMC FPH SGP MIN PLS EVN
CAR SEK NXT TWE PME JBH ALD AGL BSL LYC""".split()]

UNIVERSES = {"US": US_UNIVERSE, "ASX": ASX_UNIVERSE}
SHORTLIST = 8          # per market: how many stage-1 leaders get the full signal
MIN_BARS = 220         # need a 200-day average
MIN_DOLLAR_VOLUME = 5e6  # average daily traded value, in the stock's own currency

_state = {"status": "idle", "stage": "", "done": 0, "total": 0, "error": None}
_lock = threading.Lock()


def quick_score(df) -> dict:
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


def run_scan(markets: list[str]) -> dict:
    """Scan the chosen markets and return ranked ideas plus the full stage-1 table."""
    markets = [m for m in markets if m in UNIVERSES] or list(UNIVERSES)
    screened, missing, ideas = [], 0, []
    _state.update(stage="Screening prices", done=0, total=sum(len(UNIVERSES[m]) for m in markets))
    leaders: list[tuple[str, dict]] = []
    for m in markets:
        universe = UNIVERSES[m]
        data = market.get_history_bulk(universe)
        missing += len(universe) - len(data)
        rows = []
        for sym, df in data.items():
            if len(df) < MIN_BARS:
                missing += 1
                continue
            q = quick_score(df)
            if not np.isfinite(q["score"]) or q["dollar_volume"] < MIN_DOLLAR_VOLUME:
                continue
            rows.append({"symbol": sym, "market": m, "currency": market.currency_of(sym),
                         "as_of": df.index[-1].strftime("%Y-%m-%d"), **q})
        rows.sort(key=lambda r: r["score"], reverse=True)
        screened += rows
        leaders += [(r["symbol"], r) for r in rows[:SHORTLIST] if r["score"] >= 0.2]
        _state["done"] += len(universe)

    _state.update(stage="Running the full signal on the leaders", done=0, total=len(leaders))
    for sym, quick in leaders:
        try:
            sig = signals.compute_signal(sym)
        except Exception:  # a bad ticker should not sink the scan
            _state["done"] += 1
            continue
        _state["done"] += 1
        if sig["score"] < 0.2:
            continue  # the full signal no longer rates it a Buy
        fc = sig["forecast"] or {}
        ideas.append({
            "symbol": sym, "name": market.get_name(sym), "market": quick["market"], "currency": sig["currency"],
            "price": sig["price"], "as_of": sig["as_of"], "score": sig["score"], "label": sig["label"],
            "quick_score": quick["score"], "rsi": quick["rsi"], "ret_20": quick["ret_20"], "ret_120": quick["ret_120"],
            "prob_up": fc.get("prob_up") if fc.get("available") else None,
            "ml_skill": fc.get("skill") if fc.get("available") else None,
            "news_score": sig["news"]["score"] if sig["news"] and sig["news"]["count"] else None,
            "plan": sig["plan"], "reasons": _reasons(sig),
        })
    ideas.sort(key=lambda r: r["score"], reverse=True)
    screened.sort(key=lambda r: r["score"], reverse=True)
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "markets": markets, "universe_size": sum(len(UNIVERSES[m]) for m in markets),
        "screened": len(screened), "missing": missing, "shortlisted": len(leaders),
        "ideas": ideas, "table": screened,
    }


def _worker(markets: list[str]) -> None:
    from .api import clean

    try:
        result = clean(run_scan(markets))
        with connect() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ideas', ?)", (json.dumps(result),))
        _state.update(status="idle", stage="", error=None)
    except Exception as exc:
        _state.update(status="idle", stage="", error=f"The scan failed: {exc}")


def start_scan(markets: list[str], wait: bool = False) -> bool:
    """Start a scan in the background. Returns False if one is already running."""
    with _lock:
        if _state["status"] == "running":
            return False
        _state.update(status="running", stage="Starting", done=0, total=0, error=None)
    thread = threading.Thread(target=_worker, args=(markets,), daemon=True)
    thread.start()
    if wait:
        thread.join()
    return True


def latest() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='ideas'").fetchone()
    return json.loads(row["value"]) if row else None


def status() -> dict:
    return {**_state, "result": latest()}
