"""News headlines and sentiment.

Headlines come from two free RSS feeds: Yahoo Finance (per ticker) and Google
News (search). Each headline is scored from -1 (bearish) to +1 (bullish) with
VADER plus a small finance vocabulary. Headline sentiment is a coarse signal:
it reads tone, not meaning, and it sees only the headline.

Every headline is archived in SQLite so a sentiment history builds up over
time (free feeds only return recent items, so history cannot be backfilled).
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from ..config import settings
from ..db import connect
from .market import get_name, is_asx, normalize_symbol

# Words whose market meaning differs from everyday English, on VADER's -4..4 scale.
FINANCE_LEXICON = {
    "beat": 2.0, "beats": 2.0, "tops": 1.8, "surge": 2.5, "surges": 2.5, "soar": 2.5,
    "soars": 2.5, "rally": 2.0, "rallies": 2.0, "jump": 1.8, "jumps": 1.8, "upgrade": 2.5,
    "upgrades": 2.5, "upgraded": 2.5, "outperform": 2.2, "bullish": 2.5, "buyback": 1.8,
    "record": 1.2, "raises": 1.5, "hikes": 0.8, "rebound": 1.8, "rebounds": 1.8,
    "breakout": 1.5, "overweight": 1.8, "growth": 1.2, "profit": 1.5, "profits": 1.5,
    "miss": -2.0, "misses": -2.0, "plunge": -2.8, "plunges": -2.8, "tumble": -2.5,
    "tumbles": -2.5, "slump": -2.3, "slumps": -2.3, "sink": -2.0, "sinks": -2.0,
    "slide": -1.8, "slides": -1.8, "downgrade": -2.5, "downgrades": -2.5,
    "downgraded": -2.5, "underperform": -2.2, "bearish": -2.5, "lawsuit": -2.0,
    "probe": -1.8, "investigation": -1.8, "recall": -1.8, "layoffs": -1.5, "cuts": -1.2,
    "cut": -1.0, "warns": -2.0, "warning": -1.8, "selloff": -2.5, "sell-off": -2.5,
    "bankruptcy": -3.5, "default": -2.5, "fraud": -3.2, "underweight": -1.8,
    "short": -0.8, "tariff": -1.2, "tariffs": -1.2, "halt": -1.5, "halted": -1.5,
    "share": 0.0, "shares": 0.0, "stock": 0.0, "gross": 0.0, "vice": 0.0,
}

_analyzer = None
_last_fetch: dict[str, float] = {}


def score_headline(text: str) -> float:
    global _analyzer
    if _analyzer is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        _analyzer = SentimentIntensityAnalyzer()
        _analyzer.lexicon.update(FINANCE_LEXICON)
    return float(_analyzer.polarity_scores(text)["compound"])


def _feeds(symbol: str) -> list[tuple[str, str]]:
    base = symbol.split(".")[0].lstrip("^")
    name = get_name(symbol)
    query = f'"{name}" OR "{base}" stock' if name != symbol else f"{base} stock"
    if is_asx(symbol):
        region, gnews = "AU&lang=en-AU", "hl=en-AU&gl=AU&ceid=AU:en"
        query += " ASX"
    else:
        region, gnews = "US&lang=en-US", "hl=en-US&gl=US&ceid=US:en"
    return [
        ("Yahoo Finance", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(symbol)}&region={region}"),
        ("Google News", f"https://news.google.com/rss/search?q={quote_plus(query)}+when:7d&{gnews}"),
    ]


def _download(symbol: str) -> list[dict]:
    import feedparser
    import requests

    items = []
    for feed_name, url in _feeds(symbol):
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (QuantLab)"})
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception:
            continue  # one feed failing should not hide the other
        for e in parsed.entries[:40]:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            source = feed_name
            src = e.get("source")
            if src and src.get("title"):
                source = src["title"]
                if title.endswith(" - " + source):
                    title = title[: -len(source) - 3]
            stamp = e.get("published_parsed") or e.get("updated_parsed")
            published = datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else datetime.now(timezone.utc)
            items.append({"title": title, "link": e.get("link", ""), "source": source,
                          "published": published.isoformat()})
    return items


def refresh_news(symbol: str, force: bool = False) -> None:
    symbol = normalize_symbol(symbol)
    if not force and time.time() - _last_fetch.get(symbol, 0) < settings.news_ttl:
        return
    _last_fetch[symbol] = time.time()
    items = _download(symbol)
    with connect() as conn:
        for it in items:
            conn.execute(
                "INSERT OR IGNORE INTO news (symbol, link, title, source, published, sentiment) VALUES (?,?,?,?,?,?)",
                (symbol, it["link"], it["title"], it["source"], it["published"], score_headline(it["title"])),
            )


def get_news(symbol: str, days: int = 7, limit: int = 40) -> list[dict]:
    """Recent headlines, newest first."""
    symbol = normalize_symbol(symbol)
    refresh_news(symbol)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            "SELECT title, link, source, published, sentiment FROM news "
            "WHERE symbol=? AND published>=? ORDER BY published DESC LIMIT ?",
            (symbol, since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def summarize(items: list[dict], half_life_days: float = 2.0) -> dict:
    """Recency-weighted average sentiment: a headline loses half its weight every two days."""
    if not items:
        return {"score": 0.0, "count": 0, "positive": 0, "negative": 0}
    now = datetime.now(timezone.utc)
    total = weight_sum = 0.0
    for it in items:
        age = max((now - datetime.fromisoformat(it["published"])).total_seconds() / 86400, 0.0)
        w = math.pow(0.5, age / half_life_days)
        total += w * it["sentiment"]
        weight_sum += w
    return {
        "score": total / weight_sum if weight_sum else 0.0,
        "count": len(items),
        "positive": sum(1 for it in items if it["sentiment"] > 0.15),
        "negative": sum(1 for it in items if it["sentiment"] < -0.15),
    }


def news_sentiment(symbol: str) -> dict:
    return summarize(get_news(symbol))
