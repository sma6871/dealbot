"""Loads config.toml. Plain dict access, no framework."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"

DEFAULTS = {
    "max_candidates": 10,
    "max_feed_items": 80,
    "lookback_hours": 14,
    "feeds": ["https://www.mydealz.de/rss/hot"],
}


def _load():
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

_source = _cfg.get("source", {})
USE_GRAPHQL = _source.get("use_graphql", True)
DESCRIPTION_CHARS = _source.get("description_chars", 1500)

# Per-provider settings: {"gemini": {"model": ..., "api_key_env": ...}, ...}
PROVIDER_CONFIG = {name: _cfg.get(name, {}) for name in PROVIDERS}

POSTING = _cfg.get("posting", {})


def read_doc(name):
    """Read a markdown doc from the repo root, empty string if absent."""
    p = ROOT / name
    return p.read_text(encoding="utf-8") if p.exists() else ""
