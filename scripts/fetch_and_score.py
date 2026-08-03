#!/usr/bin/env python3
"""Fetch mydealz deals, score them against rules.md + brands.md, DM candidates.

Everything after my first button tap is handled by the Cloudflare Worker.
This script only produces candidates and writes them to KV.
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import feedparser
import requests

import config
import kv
from providers import REGISTRY

PENDING_TTL = 60 * 60 * 24 * 3  # 3 days, matches the Worker


# ---------- env ----------

def require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))


def available_providers():
    """Providers from config that actually have their API key set."""
    out = []
    for name in config.PROVIDERS:
        settings = config.PROVIDER_CONFIG.get(name, {})
        key_env = settings.get("api_key_env")
        if name in REGISTRY and key_env and os.environ.get(key_env):
            out.append((name, settings))
    return out


# ---------- fetch ----------

def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_entry(entry, cutoff):
    published = None
    if getattr(entry, "published_parsed", None):
        published = datetime.fromtimestamp(
            time.mktime(entry.published_parsed), tz=timezone.utc
        )
    if published and published < cutoff:
        return None

    title = strip_html(entry.get("title", ""))
    if not title:
        return None

    temp_match = re.search(r"(\d[\d.]*)\s*°", title)
    price_match = re.search(r"(\d+[.,]?\d*)\s*€|€\s*(\d+[.,]?\d*)", title)

    return {
        "id": entry.get("id") or entry.get("link"),
        "title": title,
        "link": entry.get("link", ""),
        "summary": strip_html(entry.get("summary", ""))[:400],
        "temperature": temp_match.group(1).replace(".", "") if temp_match else None,
        "price": (price_match.group(1) or price_match.group(2)) if price_match else None,
        "published": published.isoformat() if published else None,
    }


def fetch_deals():
    """Read every configured feed, merge, dedupe by id keeping first occurrence."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.LOOKBACK_HOURS)
    deals, seen_ids = [], set()

    for url in config.FEED_URLS:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            print(f"  {url}: parse failed ({feed.get('bozo_exception')})", file=sys.stderr)
            continue

        added = 0
        for entry in feed.entries:
            deal = parse_entry(entry, cutoff)
            if not deal or not deal["id"] or deal["id"] in seen_ids:
                continue
            seen_ids.add(deal["id"])
            deals.append(deal)
            added += 1
        print(f"  {url}: {added} new")

        if len(deals) >= config.MAX_FEED_ITEMS:
            break

    return deals[: config.MAX_FEED_ITEMS]


# ---------- score ----------

PROMPT = """You are the editorial filter for a small Telegram deals channel in Berlin.

Follow this policy strictly. It is deliberately restrictive.

<policy>
{rules}
</policy>

<brands>
{brands}
</brands>

Here are today's candidate deals from mydealz.de:

<deals>
{deals}
</deals>

Pick at most {n} deals that fit the policy. Fewer is strongly preferred over
more. If only two genuinely fit, return two. If none fit, return an empty list.
Never pad the list to reach {n}.

Where the policy says to flag something for manual verification, prefix that
deal's reason with "VERIFY: ".

Respond with ONLY this JSON object, no markdown fences, no preamble:
{{"picks": [{{"index": <deal index number>, "reason": "<one short sentence>"}}]}}
"""


def build_prompt(deals):
    listing = "\n".join(
        f"[{i}] {d['title']}\n"
        f"    temp: {d['temperature'] or 'n/a'} | price: {d['price'] or 'n/a'}\n"
        f"    {d['summary'][:200]}"
        for i, d in enumerate(deals)
    )
    return PROMPT.format(
        rules=config.read_doc("rules.md"),
        brands=config.read_doc("brands.md"),
        deals=listing,
        n=config.MAX_CANDIDATES,
    )


def extract_picks(raw, deals):
    """Accept either {"picks": [...]} or a bare [...] so both providers work."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    parsed = json.loads(text)

    if isinstance(parsed, dict):
        items = parsed.get("picks", [])
    elif isinstance(parsed, list):
        items = parsed
    else:
        raise ValueError(f"unexpected JSON type: {type(parsed).__name__}")

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= idx < len(deals):
            out.append({**deals[idx], "reason": str(item.get("reason", ""))[:300]})
    return out[: config.MAX_CANDIDATES]


def score(deals):
    """Try providers in config order. First one that returns usable picks wins."""
    providers = available_providers()
    if not providers:
        raise SystemExit(
            "No usable provider. Set an API key for one of: "
            + ", ".join(config.PROVIDERS)
        )

    prompt = build_prompt(deals)
    failures = []

    for name, settings in providers:
        model = settings.get("model", "")
        api_key = os.environ[settings["api_key_env"]]
        try:
            raw = REGISTRY[name](prompt, model, api_key)
            picks = extract_picks(raw, deals)
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            failures.append(f"{name}: {exc}")
            print(f"  {name} failed: {exc}", file=sys.stderr)
            continue

        if not picks:
            # An empty list is a legitimate answer, not a failure.
            print(f"  {name} returned no picks (valid outcome)")
            return []
        print(f"  scored by {name} ({model})")
        return picks

    raise SystemExit("All providers failed:\n  " + "\n  ".join(failures))


# ---------- send ----------

def send_for_review(deal, token, chat_id):
    verify = deal["reason"].startswith("VERIFY:")
    text = (
        f"{'⚠️ ' if verify else ''}<b>{html.escape(deal['title'])}</b>\n\n"
        f"<i>{html.escape(deal['reason'])}</i>\n\n"
        f"🌡 {deal['temperature'] or 'n/a'}\n"
        f"{html.escape(deal['link'])}"
    )
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
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
    if not r.ok:
        print(f"sendMessage failed: {r.text}", file=sys.stderr)
        return None
    return r.json()["result"]["message_id"]


# ---------- main ----------

def main():
    require_env(
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ADMIN_CHAT_ID",
        "CF_ACCOUNT_ID",
        "CF_KV_NAMESPACE_ID",
        "CF_API_TOKEN",
    )
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_ADMIN_CHAT_ID"]

    print("Fetching feeds:")
    seen = kv.get_json("seen", []) or []
    seen_set = set(seen)

    deals = [d for d in fetch_deals() if d["id"] not in seen_set]
    print(f"{len(deals)} unseen deals")
    if not deals:
        return

    print("Scoring:")
    picks = score(deals)
    print(f"{len(picks)} candidates")

    for deal in picks:
        mid = send_for_review(deal, token, chat_id)
        if mid:
            kv.put_json(f"pending:{mid}", deal, expiration_ttl=PENDING_TTL)
            print(f"  sent {mid}: {deal['title'][:60]}")

    # Mark everything evaluated as seen so we never re-score it.
    seen.extend(d["id"] for d in deals)
    kv.put_json("seen", seen[-3000:])


if __name__ == "__main__":
    main()
