"""HTTP API and web app (FastAPI). Run with ``python -m quantlab serve``."""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, backtest, chat, ideas, paper, signals
from .analytics import indicators as ind
from .analytics import options as opt
from .analytics import risk
from .config import settings
from .data import market, news
from .db import connect

WEB_DIR = Path(__file__).parent / "web"
SESSION_COOKIE = "ql_session"
SESSION_SECONDS = 7 * 86400

app = FastAPI(title="QuantLab", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")


def clean(obj):
    """Make numpy/pandas values JSON-safe; NaN and infinity become null."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def line(series: pd.Series) -> list[dict]:
    s = series.dropna()
    return [{"time": d.strftime("%Y-%m-%d"), "value": float(v)} for d, v in s.items() if math.isfinite(v)]


# ---------------------------------------------------------------- auth

def _secret() -> bytes:
    path = settings.data_dir / "secret.key"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


def _sign(expiry: int) -> str:
    return hmac.new(_secret(), f"{expiry}:{settings.password}".encode(), hashlib.sha256).hexdigest()


def _valid_session(token: str | None) -> bool:
    try:
        expiry, sig = (token or "").split(".", 1)
        return int(expiry) > time.time() and hmac.compare_digest(sig, _sign(int(expiry)))
    except ValueError:
        return False


_failed_logins: list[float] = []


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    open_paths = ("/api/login", "/api/session", "/api/health")
    if settings.password and path.startswith("/api/") and path not in open_paths:
        if not _valid_session(request.cookies.get(SESSION_COOKIE)):
            return JSONResponse({"detail": "Login required"}, status_code=401)
    response = await call_next(request)
    # A private tool: ask search engines not to index or follow any of it.
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.exception_handler(market.DataError)
async def data_error(_: Request, exc: market.DataError):
    return JSONResponse({"detail": str(exc)}, status_code=404)


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, request: Request):
    now = time.time()
    _failed_logins[:] = [t for t in _failed_logins if now - t < 300]
    if len(_failed_logins) >= 10:
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    if not settings.password or not hmac.compare_digest(body.password.encode(), settings.password.encode()):
        _failed_logins.append(now)
        raise HTTPException(401, "Wrong password")
    expiry = int(now) + SESSION_SECONDS
    resp = JSONResponse({"ok": True})
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(SESSION_COOKIE, f"{expiry}.{_sign(expiry)}", max_age=SESSION_SECONDS,
                    httponly=True, samesite="strict", secure=https)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/session")
def session(request: Request):
    needed = bool(settings.password)
    return {"login_required": needed,
            "logged_in": not needed or _valid_session(request.cookies.get(SESSION_COOKIE))}


@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------- watchlist

def _watchlist() -> list[str]:
    with connect() as conn:
        if not conn.execute("SELECT value FROM meta WHERE key='watchlist_seeded'").fetchone():
            conn.executemany("INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)",
                             [(s,) for s in settings.default_watchlist])
            conn.execute("INSERT INTO meta (key, value) VALUES ('watchlist_seeded', '1')")
        return [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist ORDER BY added, symbol")]


class SymbolBody(BaseModel):
    symbol: str


@app.get("/api/watchlist")
def watchlist():
    return _watchlist()


@app.post("/api/watchlist")
def watchlist_add(body: SymbolBody):
    symbol = market.normalize_symbol(body.symbol)
    market.get_history(symbol)  # 404s for unknown tickers
    _watchlist()
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)", (symbol,))
    return _watchlist()


@app.delete("/api/watchlist")
def watchlist_remove(symbol: str):
    _watchlist()
    with connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE symbol=?", (market.normalize_symbol(symbol),))
    return _watchlist()


@app.get("/api/overview")
def overview():
    """One row per watchlist symbol: quote, signal and news sentiment."""
    rows = []
    for symbol in _watchlist():
        try:
            q = market.get_quote(symbol)
            s = signals.compute_signal(symbol)
            spark = market.get_history(symbol)["Close"].tail(60)
            rows.append({**q, "score": s["score"], "label": s["label"],
                         "prob_up": s["forecast"]["prob_up"] if s["forecast"] and s["forecast"]["available"] else None,
                         "news_score": s["news"]["score"] if s["news"] else None,
                         "news_count": s["news"]["count"] if s["news"] else 0,
                         "spark": [float(x) for x in spark]})
        except Exception as exc:  # one bad ticker must not blank the dashboard
            rows.append({"symbol": symbol, "error": str(exc)})
    return clean(rows)


# ---------------------------------------------------------------- symbol

@app.get("/api/history")
def history(symbol: str, years: float = 1.0):
    full = market.get_history(symbol)
    df = market.get_history(symbol, years=years)
    c = full["Close"]
    start = df.index[0]
    candles = [{"time": d.strftime("%Y-%m-%d"), "open": r.Open, "high": r.High, "low": r.Low,
                "close": r.Close, "volume": r.Volume} for d, r in df.iterrows()]
    rets = df["Close"].pct_change().dropna()
    bench = market.get_history(market.benchmark_of(symbol), years=2)["Close"].pct_change()
    cur = market.currency_of(symbol)
    return clean({
        "quote": market.get_quote(symbol),
        "candles": candles,
        "ema20": line(ind.ema(c, 20)[start:]),
        "ema50": line(ind.ema(c, 50)[start:]),
        "sma200": line(ind.sma(c, 200)[start:]),
        "rsi": line(ind.rsi(c)[start:]),
        "stats": {
            "period_return": float(df["Close"].iloc[-1] / df["Close"].iloc[0] - 1),
            "volatility": risk.annual_vol(rets),
            "sharpe": risk.sharpe_ratio(rets, settings.risk_free(cur)),
            "max_drawdown": risk.max_drawdown(df["Close"]),
            "beta": risk.beta(full["Close"].pct_change().tail(504), bench),
            "benchmark": market.benchmark_of(symbol),
            "realized_vol_20": float(ind.realized_vol(c).iloc[-1]),
        },
    })


@app.get("/api/signal")
def signal(symbol: str):
    return clean(signals.compute_signal(symbol))


@app.get("/api/news")
def symbol_news(symbol: str):
    items = news.get_news(symbol)
    return clean({"summary": news.summarize(items), "items": items})


# ---------------------------------------------------------------- options

@app.get("/api/options/expiries")
def option_expiries(symbol: str):
    return {"symbol": market.normalize_symbol(symbol), "expiries": market.get_expiries(symbol)}


@app.get("/api/options/chain")
def option_chain(symbol: str, expiry: str):
    chain = market.get_chain(symbol, expiry)
    return clean(opt.analyze_chain(chain, settings.risk_free(market.currency_of(chain.symbol))))


class PriceBody(BaseModel):
    spot: float = Field(gt=0)
    strike: float = Field(gt=0)
    days: float = Field(gt=0, le=3650)
    vol: float = Field(gt=0, le=5, description="Annual volatility as a decimal, e.g. 0.25")
    rate: float = Field(default=0.04, ge=-0.05, le=0.5)
    dividend_yield: float = Field(default=0.0, ge=0, le=0.5)
    kind: str = Field(default="call", pattern="^(call|put)$")


@app.post("/api/options/price")
def option_price(b: PriceBody):
    T = b.days / 365
    args = (b.spot, b.strike, T, b.rate, b.vol, b.kind, b.dividend_yield)
    price = opt.bs_price(*args)
    breakeven = b.strike + price if b.kind == "call" else b.strike - price
    return clean({"price": price, "breakeven": breakeven, **opt.bs_greeks(*args)})


# ---------------------------------------------------------------- backtest

class BacktestBody(BaseModel):
    symbol: str
    strategy: str = "ema_cross"
    params: dict[str, float] = {}
    years: float = Field(default=5, gt=0.5, le=10)
    cost_bps: float = Field(default=5, ge=0, le=200)
    initial: float = Field(default=10000, gt=0)


@app.get("/api/strategies")
def strategies():
    return [{"id": k, "label": v["label"], "params": v["params"], "description": v["fn"].__doc__}
            for k, v in backtest.STRATEGIES.items()]


@app.post("/api/backtest")
def run_backtest(b: BacktestBody):
    df = market.get_history(b.symbol, years=b.years)
    try:
        res = backtest.backtest_strategy(df, b.strategy, b.params, cost_bps=b.cost_bps, initial=b.initial,
                                         risk_free=settings.risk_free(market.currency_of(market.normalize_symbol(b.symbol))))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return clean({"stats": res["stats"], "benchmark": res["benchmark"], "equity": line(res["equity"]),
                  "benchmark_equity": line(res["benchmark_equity"]), "trades": res["trades"][-200:],
                  "from": df.index[0].strftime("%Y-%m-%d"), "to": df.index[-1].strftime("%Y-%m-%d")})


# ---------------------------------------------------------------- paper trading

class OrderBody(BaseModel):
    symbol: str
    side: str
    qty: float
    note: str = ""


@app.get("/api/paper/account")
def paper_account():
    return clean(paper.account())


@app.post("/api/paper/orders")
def paper_order(b: OrderBody):
    try:
        return clean(paper.place_order(b.symbol, b.side, b.qty, b.note))
    except paper.OrderError as exc:
        raise HTTPException(400, str(exc))


class OptionOrderBody(BaseModel):
    symbol: str
    expiry: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    strike: float = Field(gt=0)
    kind: str
    side: str
    contracts: float
    note: str = ""


@app.post("/api/paper/options/orders")
def paper_option_order(b: OptionOrderBody):
    try:
        return clean(paper.place_option_order(b.symbol, b.expiry, b.strike, b.kind, b.side, b.contracts, b.note))
    except paper.OrderError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/paper/trades")
def paper_trades():
    return clean(paper.trades())


@app.post("/api/paper/reset")
def paper_reset():
    paper.reset()
    return {"ok": True}


@app.get("/api/signals/performance")
def signals_performance(horizon: int = 5):
    return clean(signals.signal_performance(max(1, min(horizon, 60))))


# ---------------------------------------------------------------- ideas

@app.get("/api/ideas")
def ideas_status():
    return ideas.status()


@app.post("/api/ideas/scan")
def ideas_scan(market_name: str = "ALL"):
    markets = list(ideas.UNIVERSES) if market_name.upper() == "ALL" else [market_name.upper()]
    if any(m not in ideas.UNIVERSES for m in markets):
        raise HTTPException(400, "Market must be US, ASX or ALL.")
    return {"started": ideas.start_scan(markets)}


# ---------------------------------------------------------------- assistant

class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class ChatBody(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    viewing: str | None = Field(default=None, max_length=15)


@app.get("/api/chat/status")
def chat_status():
    return {"enabled": chat.is_enabled(), "model": settings.chat_model, "web_search": settings.chat_web_search}


@app.post("/api/chat")
def chat_reply(b: ChatBody):
    viewing = None
    if b.viewing:
        try:
            viewing = market.normalize_symbol(b.viewing)
        except market.DataError:
            pass
    try:
        return chat.reply([m.model_dump() for m in b.messages], viewing)
    except chat.ChatError as exc:
        raise HTTPException(503, str(exc))


# ---------------------------------------------------------------- web app

@app.get("/robots.txt", include_in_schema=False)
def robots():
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
