"""Settings: loaded from config.yaml plus secrets from the environment (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

PLATFORM_NAMES = ("x", "bluesky", "mastodon", "facebook", "instagram", "youtube")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class PlatformSettings:
    enabled: bool = False
    posts_per_day: int = 1


@dataclass
class TradingSettings:
    enabled: bool = True
    crypto_exchange: str = "kraken"    # any ccxt exchange id (prices only)
    paper_starting_cash: float = 1000.0
    max_usd_per_trade: float = 50.0
    max_daily_loss_usd: float = 100.0
    max_open_positions: int = 5
    max_memecoin_exposure_usd: float = 200.0
    default_stop_loss_pct: float = 15.0
    default_take_profit_pct: float = 40.0
    memecoin_chains: list[str] = field(default_factory=lambda: ["solana", "ethereum", "base"])
    min_liquidity_usd: float = 50000.0
    watchlist: list[str] = field(default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD"])
    alert_move_pct: float = 5.0
    watch_every_minutes: int = 15
    memecoin_scan_every_minutes: int = 60
    auto_paper_trade: bool = True


@dataclass
class Settings:
    owner_name: str = "Boss"
    brand_name: str = "My Brand"
    brand_description: str = ""
    brand_voice: str = "Friendly and concise."
    audience: str = ""
    topics: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    hashtags_style: str = "0-2 relevant hashtags"
    mode: str = "review"
    dry_run: bool = True
    timezone: str = "UTC"
    model: str = "claude-opus-5-5"
    effort: str = "high"
    platforms: dict[str, PlatformSettings] = field(default_factory=dict)
    plan_content_at: str = "07:00"
    posting_times: list[str] = field(default_factory=lambda: ["09:00", "13:00", "18:30"])
    check_mentions_every_minutes: int = 15
    refresh_metrics_every_minutes: int = 60
    daily_briefing_at: str = "21:00"
    auto_reply: bool = True
    max_replies_per_run: int = 10
    db_path: str = "data/jarvis.db"
    youtube_inbox_dir: str = "videos"
    youtube_privacy: str = "public"
    youtube_category_id: str = "22"
    trading: TradingSettings = field(default_factory=TradingSettings)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def autopilot(self) -> bool:
        return self.mode == "autopilot"

    def enabled_platforms(self) -> list[str]:
        return [name for name, p in self.platforms.items() if p.enabled]


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    """Load config.yaml (falling back to config.example.yaml) and the .env file."""
    load_dotenv()
    candidates = [Path(path)] if path else [Path("config.yaml"), Path("config.example.yaml")]
    raw: dict = {}
    for candidate in candidates:
        if candidate.exists():
            raw = yaml.safe_load(candidate.read_text()) or {}
            break

    schedule = raw.pop("schedule", {}) or {}
    engagement = raw.pop("engagement", {}) or {}
    platforms_raw = raw.pop("platforms", {}) or {}
    youtube = {f"youtube_{k}": v for k, v in (raw.pop("youtube", {}) or {}).items()}
    trading_raw = raw.pop("trading", {}) or {}

    known = set(Settings.__dataclass_fields__)
    settings = Settings(**{k: v for k, v in raw.items() if k in known})
    for k, v in {**schedule, **engagement, **youtube}.items():
        if k in known:
            setattr(settings, k, v)

    settings.platforms = {
        name: PlatformSettings(**(platforms_raw.get(name) or {})) for name in PLATFORM_NAMES
    }

    trading_known = set(TradingSettings.__dataclass_fields__)
    unknown = set(trading_raw) - trading_known
    if unknown:
        raise ValueError(f"unknown trading settings: {sorted(unknown)}")
    settings.trading = TradingSettings(**trading_raw)
    if settings.youtube_privacy not in ("public", "unlisted", "private"):
        raise ValueError("youtube.privacy must be public, unlisted or private")

    if settings.mode not in ("review", "autopilot"):
        raise ValueError(f"mode must be 'review' or 'autopilot', got {settings.mode!r}")
    if settings.effort not in EFFORT_LEVELS:
        raise ValueError(f"effort must be one of {EFFORT_LEVELS}, got {settings.effort!r}")
    settings.tz  # validate timezone early
    return settings
