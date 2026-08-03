"""Load config.toml into plain module-level values."""

from __future__ import annotations

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"

DEFAULTS = {
    "max_candidates": 5,
    "max_feed_items": 60,
    "lookback_hours": 26,
    "feeds": ["https://www.mydealz.de/rss/hot"],
}


def _load() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


_cfg = _load()
_scoring = _cfg.get("scoring", {})

PROVIDERS = _scoring.get("providers", ["gemini", "groq"])
MAX_CANDIDATES = _scoring.get("max_candidates", DEFAULTS["max_candidates"])
MAX_FEED_ITEMS = _scoring.get("max_feed_items", DEFAULTS["max_feed_items"])
LOOKBACK_HOURS = _scoring.get("lookback_hours", DEFAULTS["lookback_hours"])
FEED_URLS = _cfg.get("feeds", {}).get("urls", DEFAULTS["feeds"])
PROVIDER_CONFIG = {name: _cfg.get(name, {}) for name in PROVIDERS}
POSTING = _cfg.get("posting", {})


def read_doc(name: str) -> str:
    path = ROOT / name
    return path.read_text(encoding="utf-8") if path.exists() else ""
