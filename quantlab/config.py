"""Runtime settings, all overridable with QUANTLAB_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"QUANTLAB_{name}", default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data")))
    # Optional: when set, the web app requires this password. Not needed for
    # local use, where only this computer can reach the app.
    password: str = field(default_factory=lambda: _env("PASSWORD", ""))
    # Annual risk-free rates used for Sharpe ratios and option pricing.
    risk_free_usd: float = field(default_factory=lambda: float(_env("RISK_FREE_USD", "0.04")))
    risk_free_aud: float = field(default_factory=lambda: float(_env("RISK_FREE_AUD", "0.04")))
    # Paper trading.
    paper_start_cash: float = field(default_factory=lambda: float(_env("PAPER_START_CASH", "100000")))
    paper_slippage_bps: float = field(default_factory=lambda: float(_env("PAPER_SLIPPAGE_BPS", "5")))
    paper_commission_bps: float = field(default_factory=lambda: float(_env("PAPER_COMMISSION_BPS", "5")))
    paper_option_fee: float = field(default_factory=lambda: float(_env("PAPER_OPTION_FEE", "0.65")))  # per contract
    # Local time (HH:MM) of the automatic daily run while the app is open; "off" disables it.
    daily_time: str = field(default_factory=lambda: _env("DAILY_TIME", "18:00"))
    # Let Claude read and score news headlines instead of the built-in word scoring (uses the API key).
    ai_news: bool = field(default_factory=lambda: _env("AI_NEWS", "") in ("1", "true", "yes"))
    news_model: str = field(default_factory=lambda: _env("NEWS_MODEL", "claude-opus-5-5"))
    # Assistant (chat). Needs an Anthropic API key in ANTHROPIC_API_KEY.
    chat_model: str = field(default_factory=lambda: _env("CHAT_MODEL", "claude-opus-5-5"))
    chat_web_search: bool = field(default_factory=lambda: _env("CHAT_WEB_SEARCH", "") in ("1", "true", "yes"))
    # How long fetched prices / news are reused before refetching (seconds).
    price_ttl: int = field(default_factory=lambda: int(_env("PRICE_TTL", "600")))
    news_ttl: int = field(default_factory=lambda: int(_env("NEWS_TTL", "1800")))
    default_watchlist: tuple[str, ...] = ("AAPL", "TSLA", "RDDT", "IVV", "BHP.AX", "CBA.AX")

    def risk_free(self, currency: str) -> float:
        return self.risk_free_aud if currency == "AUD" else self.risk_free_usd

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
