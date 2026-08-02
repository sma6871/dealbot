#!/usr/bin/env python3
"""Fetch mydealz deals, score them against rules.md, DM the top N for approval."""

import html
import json
import os
import re
import sys
import time
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests

from providers import PROVIDERS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

FEED_URL = "https://www.mydealz.de/rss/hot"


def load_config():
    path = ROOT / "config.toml"
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    scoring = cfg.get("scoring", {})
    return {
        "providers": scoring.get("providers", ["gemini", "groq"]),
        "max_candidates": scoring.get("max_candidates", 5),
        "max_feed_items": scoring.get("max_feed_items", 60),
        "lookback_hours": scoring.get("lookback_hours", 26),
        "gemini": cfg.get("gemini", {}),
        "groq": cfg.get("groq", {}),
    }


CONFIG = load_config()
MAX_CANDIDATES = CONFIG["max_candidates"]
MAX_FEED_ITEMS = CONFIG["max_feed_items"]
LOOKBACK_HOURS = CONFIG["lookback_hours"]


def require_env(*names):
    """Require every listed var (used for Telegram secrets)."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(
            "Missing required environment variables:\n"
            + "\n".join(f"  - {n}" for n in missing),
            file=sys.stderr,
        )
        sys.exit(1)


def require_scoring_keys(providers=None):
    """Fail if none of the given providers has its api key set."""
    names = providers if providers is not None else CONFIG["providers"]
    available = []
    checked = []
    for name in names:
        section = CONFIG.get(name, {})
        env_name = section.get("api_key_env")
        if not env_name:
            continue
        checked.append(env_name)
        if os.environ.get(env_name):
            available.append(name)
    if available:
        return
    listed = checked or ["(no api_key_env configured)"]
    print(
        "Missing scoring API keys — need at least one of:\n"
        + "\n".join(f"  - {n}" for n in listed),
        file=sys.stderr,
    )
    sys.exit(1)


# ---------- tiny json helpers ----------

def load(name, default):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save(name, obj):
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- fetch ----------

def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fetch_deals():
    feed = feedparser.parse(FEED_URL)
    if feed.bozo and not feed.entries:
        print(f"Feed parse failed: {feed.get('bozo_exception')}", file=sys.stderr)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    deals = []

    for e in feed.entries:
        published = None
        if getattr(e, "published_parsed", None):
            published = datetime.fromtimestamp(
                time.mktime(e.published_parsed), tz=timezone.utc
            )
        if published and published < cutoff:
            continue

        title = strip_html(e.get("title", ""))
        if not title:
            continue

        # Temperature is usually embedded in the title of /rss/hot, e.g. "1234°"
        temp_match = re.search(r"(\d[\d.]*)\s*°", title)
        temperature = temp_match.group(1).replace(".", "") if temp_match else None

        # Price often appears as "12,99€" or "€12.99"
        price_match = re.search(r"(\d+[.,]?\d*)\s*€|€\s*(\d+[.,]?\d*)", title)
        price = (price_match.group(1) or price_match.group(2)) if price_match else None

        deals.append({
            "id": e.get("id") or e.get("link"),
            "title": title,
            "link": e.get("link", ""),
            "summary": strip_html(e.get("summary", ""))[:400],
            "temperature": temperature,
            "price": price,
            "published": published.isoformat() if published else None,
        })

    return deals[:MAX_FEED_ITEMS]


# ---------- score ----------

PROMPT = """You are the editorial filter for a small Telegram deals channel.

Here is the channel's selection policy. Follow it strictly.

<policy>
{rules}
</policy>

Here are today's candidate deals from mydealz.de:

<deals>
{deals}
</deals>

Pick the {n} best deals according to the policy. Fewer is fine — if only two
deals genuinely fit, return two. If none fit, return an empty picks list. Do not
pad the list to reach {n}.

Respond with ONLY a JSON object, no markdown fences, no preamble:
{{"picks": [{{"index": <the deal's index number>, "reason": "<one short sentence on why this fits the policy>"}}]}}
"""


def _parse_picks(text):
    """Parse model text into a list of pick dicts. Accepts array or {"picks": [...]}."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    parsed = json.loads(text)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        picks = parsed.get("picks")
        if isinstance(picks, list):
            return picks
        raise ValueError("JSON object missing a 'picks' array")
    raise ValueError(f"expected list or object, got {type(parsed).__name__}")


def _picks_to_deals(picks, deals):
    out = []
    for p in picks:
        try:
            idx = int(p["index"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= idx < len(deals):
            out.append({**deals[idx], "reason": str(p.get("reason", ""))[:300]})
    return out[:MAX_CANDIDATES]


def score(deals, rules, provider=None):
    listing = "\n".join(
        f"[{i}] {d['title']}\n"
        f"    temp: {d['temperature'] or 'n/a'} | price: {d['price'] or 'n/a'}\n"
        f"    {d['summary'][:200]}"
        for i, d in enumerate(deals)
    )

    prompt = PROMPT.format(rules=rules, deals=listing, n=MAX_CANDIDATES)
    names = [provider] if provider else list(CONFIG["providers"])

    for name in names:
        call_fn = PROVIDERS.get(name)
        if call_fn is None:
            print(f"{name} failed: unknown provider", file=sys.stderr)
            continue

        section = CONFIG.get(name, {})
        env_name = section.get("api_key_env")
        if not env_name or not os.environ.get(env_name):
            print(f"{name}: skipped ({env_name or 'api_key_env'} not set)", file=sys.stderr)
            continue

        api_key = os.environ[env_name]
        model = section.get("model", "")
        if not model:
            print(f"{name} failed: no model in config", file=sys.stderr)
            continue

        try:
            text = call_fn(prompt, model, api_key)
        except (requests.RequestException, ValueError) as exc:
            print(f"{name} failed: {exc}", file=sys.stderr)
            continue

        try:
            raw_picks = _parse_picks(text)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"{name} failed: could not parse output ({exc}): {text[:500]}", file=sys.stderr)
            continue

        out = _picks_to_deals(raw_picks, deals)
        if not out:
            print(f"{name} failed: no valid picks in output", file=sys.stderr)
            continue

        print(f"scored with {name}")
        return out

    print("All scoring providers failed", file=sys.stderr)
    sys.exit(1)


# ---------- send ----------

def send_for_review(deal):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    admin_chat_id = os.environ["TELEGRAM_ADMIN_CHAT_ID"]
    tg = f"https://api.telegram.org/bot{bot_token}"

    text = (
        f"<b>{html.escape(deal['title'])}</b>\n\n"
        f"<i>{html.escape(deal['reason'])}</i>\n\n"
        f"🌡 {deal['temperature'] or 'n/a'}\n"
        f"{html.escape(deal['link'])}"
    )
    resp = requests.post(
        f"{tg}/sendMessage",
        json={
            "chat_id": admin_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ Post", "callback_data": "post"},
                    {"text": "❌ Skip", "callback_data": "skip"},
                ]]
            },
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"sendMessage failed: {resp.text}", file=sys.stderr)
        return None
    return resp.json()["result"]["message_id"]


# ---------- main ----------

def main():
    require_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_CHAT_ID")
    require_scoring_keys()

    rules = (ROOT / "rules.md").read_text(encoding="utf-8")
    seen = load("seen.json", [])
    seen_set = set(seen)

    deals = [d for d in fetch_deals() if d["id"] not in seen_set]
    print(f"{len(deals)} new deals in feed")
    if not deals:
        return

    picks = score(deals, rules)
    print(f"model picked {len(picks)}")

    pending = load("pending.json", {})
    for deal in picks:
        mid = send_for_review(deal)
        if mid:
            pending[str(mid)] = deal

    # Mark everything we looked at as seen, so we never re-evaluate it.
    seen.extend(d["id"] for d in deals)
    save("seen.json", seen[-3000:])
    save("pending.json", pending)


if __name__ == "__main__":
    main()
