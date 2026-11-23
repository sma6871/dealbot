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

import requests

import config
import kv
import mydealz
from providers import REGISTRY, call_gemini_decision

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

def fetch_deals():
    """Delegates to the mydealz client: GraphQL first, RSS as fallback."""
    return mydealz.fetch_deals(
        lookback_hours=config.LOOKBACK_HOURS,
        max_items=config.MAX_FEED_ITEMS,
        use_graphql=config.USE_GRAPHQL,
    )


# ---------- pre-filter (zero cost) ----------

BLOCKED_MERCHANTS = {
    "aliexpress", "temu", "shein", "wish", "banggood",
    "lightinthebox", "joom", "dhgate", "veepee",
}

BLOCKED_KEYWORDS = [
    # Car maintenance (audience has no car)
    "winterreifen", "sommerreifen", "ganzjahresreifen", "autobatterie",
    "kfz-versicherung", "motoröl",
    # Tobacco & vapes
    "vape", "e-zigarette", "tabak", "liquid",
    # Gambling
    "casino", "spielbank", "sportwetten", "tipico", "bwin",
    # Video games
    "[ps5]", "[ps4]", "[playstation 5]", "[playstation 4]", "[playstation store]",
    "[xbox]", "[nintendo switch]", "steam key", "playstation 5", "playstation 4",
]


def is_hard_reject(deal):
    """Zero-ambiguity reject filter. Preserves all 0€ deals, freebies, and services."""
    shop = ((deal.get("merchant") or "") + " " + (deal.get("link_host") or "")).lower()
    for b in BLOCKED_MERCHANTS:
        if b in shop:
            return True, f"blocked merchant: {b}"

    title = (deal.get("title") or "").lower()
    for kw in BLOCKED_KEYWORDS:
        if kw in title:
            return True, f"hard reject keyword: {kw}"

    return False, ""


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

Each deal gives you facts followed by its full description. Use them:
- "compare-at" is the site's historical comparison price. Judge the discount
  against it, and against any retail price stated in the description. Do not
  rely on your own memory of what things cost.
- "merchant" is authoritative for retailer rules. Do not infer the shop from
  the title when a merchant is given.
- "type Freebie" means it costs nothing. Apply the free-items policy.
- If a deal has no compare-at price and the description states no retail price,
  you cannot judge the discount. Reject it unless the policy clearly allows it
  on other grounds.

Where the policy says to flag something for manual verification, prefix that
deal's reason with "VERIFY: ".

Respond with ONLY this JSON object, no markdown fences, no preamble:
{{"picks": [{{"index": <deal index number>, "reason": "<one short sentence>"}}]}}
"""


def format_deal(i, deal):
    """One deal as facts the model can reason about, plus the description."""
    price = deal.get("price")
    was = deal.get("next_best_price")

    facts = [f"temp {deal.get('temperature') if deal.get('temperature') is not None else 'n/a'}"]
    if price is not None:
        facts.append(f"price EUR {price}")
    if was is not None:
        facts.append(f"compare-at EUR {was}")
        if price:
            try:
                off = round((1 - float(price) / float(was)) * 100)
                facts.append(f"{off}% off")
            except (ZeroDivisionError, ValueError, TypeError):
                pass
    if deal.get("merchant"):
        facts.append(f"merchant {deal['merchant']}")
    elif deal.get("link_host"):
        facts.append(f"shop {deal['link_host']}")
    if deal.get("type"):
        facts.append(f"type {deal['type']}")
    if deal.get("voucher_code"):
        facts.append("has voucher code")

    desc = (deal.get("description") or "")[: config.DESCRIPTION_CHARS]
    return f"[{i}] {deal['title']}\n    {' | '.join(facts)}\n    {desc}"


def build_prompt(deals):
    listing = "\n\n".join(format_deal(i, d) for i, d in enumerate(deals))
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


DECISION_PROMPT = """You are the editorial filter for a small Telegram deals channel in Berlin.
Here is the channel's strict editorial policy and brand guidelines:

<policy>
{rules}
</policy>

<brands>
{brands}
</brands>

Here are candidate deals from mydealz.de:

<deals>
{deals}
</deals>
"""


def build_decision_context(deals):
    listing = "\n\n".join(format_deal(i, d) for i, d in enumerate(deals))
    return DECISION_PROMPT.format(
        rules=config.read_doc("rules.md"),
        brands=config.read_doc("brands.md"),
        deals=listing,
    )


def score_two_stage(deals):
    """Two-stage scoring:
    Stage 1: gemini-decision-1 filters deals with threshold (default 0.5) at $0 output cost.
    Stage 2: Gemini Flash verifies survivors and generates the 1-sentence Telegram reason.
    Falls back gracefully to standard score() if Stage 1 is disabled or fails.
    """
    if not config.DECISION_ENABLED or len(deals) <= 3:
        return score(deals)

    api_key_env = config.DECISION_API_KEY_ENV
    api_key = os.environ.get(api_key_env)
    if not api_key:
        print(
            f"Decision API key ({api_key_env}) unset, falling back to direct scoring",
            file=sys.stderr,
        )
        return score(deals)

    try:
        print(f"Stage 1: evaluating {len(deals)} deals with {config.DECISION_MODEL}...")
        context = build_decision_context(deals)
        scores = call_gemini_decision(context, deals, config.DECISION_MODEL, api_key)

        survivors = []
        for i, d in enumerate(deals):
            score_val = scores.get(i, 0.0)
            if score_val >= config.DECISION_THRESHOLD:
                survivors.append({**d, "_stage1_score": score_val})

        print(
            f"Stage 1: {len(survivors)} / {len(deals)} deals passed threshold "
            f"({config.DECISION_THRESHOLD})"
        )

        # Ensure top candidate deals reach Stage 2: if fewer than 4 passed,
        # include the highest-scoring deals (as long as score >= 0.05) up to 6 deals.
        if len(survivors) < 4:
            ranked = sorted(
                [(i, scores.get(i, 0.0)) for i in range(len(deals))],
                key=lambda x: x[1],
                reverse=True,
            )
            survivor_ids = {d["id"] for d in survivors}
            for idx, s in ranked[:6]:
                if s >= 0.05 and deals[idx]["id"] not in survivor_ids:
                    survivors.append({**deals[idx], "_stage1_score": s})

        if not survivors:
            print("No deals met Stage 1 criteria. Exiting without Stage 2 call.")
            return []

        # Sort survivors by stage 1 score descending if more than max_candidates
        survivors.sort(key=lambda x: x.get("_stage1_score", 0.0), reverse=True)
        survivors = survivors[: config.MAX_CANDIDATES]

        print(f"Stage 2: verifying and generating reasons for {len(survivors)} survivor(s)...")
        picks = score(survivors)
        return picks

    except Exception as exc:
        print(f"Stage 1 failed ({exc}), falling back to direct scoring", file=sys.stderr)
        return score(deals)


# ---------- send ----------

def send_for_review(deal, token, chat_id):
    verify = deal["reason"].startswith("VERIFY:")

    bits = [f"🌡 {deal.get('temperature') if deal.get('temperature') is not None else 'n/a'}"]
    if deal.get("price") is not None:
        line = f"💰 {deal['price']}€"
        if deal.get("next_best_price"):
            line += f" (was {deal['next_best_price']}€)"
        bits.append(line)
    shop = deal.get("merchant") or deal.get("link_host")
    if shop:
        bits.append(f"🏪 {html.escape(shop)}")
    if deal.get("voucher_code"):
        bits.append(f"🎫 <code>{html.escape(deal['voucher_code'])}</code>")

    text = (
        f"{'⚠️ ' if verify else ''}<b>{html.escape(deal['title'])}</b>\n\n"
        f"<i>{html.escape(deal['reason'])}</i>\n\n"
        + "\n".join(bits)
        + f"\n{html.escape(deal['link'])}"
    )
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "🔥 Post", "callback_data": "hot"},
                        {"text": "👍 Good but no", "callback_data": "good"},
                    ],
                    [
                        {"text": "😐 Meh", "callback_data": "meh"},
                        {"text": "❌ Never", "callback_data": "never"},
                    ],
                ]
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

    # Pre-filter obvious hard rejects (zero token cost)
    filtered = []
    dropped = 0
    for d in deals:
        rej, _ = is_hard_reject(d)
        if rej:
            dropped += 1
        else:
            filtered.append(d)
    if dropped:
        print(f"Pre-filter dropped {dropped} obvious junk deals ({len(filtered)} remain)")

    if not filtered:
        print("No deals remaining after pre-filter")
        seen.extend(d["id"] for d in deals)
        kv.put_json("seen", seen[-3000:])
        return

    print("Scoring:")
    picks = score_two_stage(filtered)
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
