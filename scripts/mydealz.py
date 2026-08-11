"""Fetch deals from mydealz.

Primary path is the site's internal GraphQL API, which returns full descriptions,
the historical comparison price, and the merchant name. The RSS feeds are kept as
an automatic fallback because the GraphQL API is private and undocumented.

Rate limits are aggressive: roughly 5-8 rapid queries trigger an HTTP 418 block.
We make two queries per run with a pause between them, which is well inside that.
"""

import html
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import feedparser
import requests

BASE = "https://www.mydealz.de"
GRAPHQL = f"{BASE}/graphql"
CDN = "https://static.mydealz.de"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Deliberately omits `groups` — the documented sub-selection is wrong and the
# field is not needed.
THREAD_FIELDS = """
    threadId
    title
    url
    price
    nextBestPrice
    temperature
    publishedAt
    description
    voucherCode
    type
    status
    isExpired
    expirable
    commentCount
    shareCount
    linkHost
    mainImage { path name }
    merchant { merchantName }
"""

QUERY_HOTTEST = (
    "query Hottest($filter: ThreadFilter!) { hottestWidget(filter: $filter) { threads { %s } } }"
    % THREAD_FIELDS
)
QUERY_THREADS = (
    "query Threads($filter: ThreadFilter!) { threads(filter: $filter) { %s } }"
    % THREAD_FIELDS
)

RSS_FALLBACK = [
    f"{BASE}/rss/hot",
    f"{BASE}/rss",
    f"{BASE}/rss/deals",
]


# ---------- text helpers ----------

def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def image_url(main_image):
    if not main_image:
        return None
    path, name = main_image.get("path"), main_image.get("name")
    if not (path and name):
        return None
    return f"{CDN}/{path}/{name}/re/600x600/qt/70/{name}.jpg"


# ---------- GraphQL ----------

def _session():
    """A GraphQL POST needs cookies and an XSRF token from the homepage first."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })
    r = s.get(BASE + "/", timeout=20)
    r.raise_for_status()
    token = s.cookies.get("xsrf_t")
    if not token:
        raise RuntimeError("no xsrf_t cookie from homepage")
    return s, token


def _query(session, token, query, variables):
    r = session.post(
        GRAPHQL,
        headers={
            "X-Xsrf-Token": token,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
        json={"query": query, "variables": variables},
        timeout=25,
    )
    if r.status_code == 418:
        raise RuntimeError("rate limited by WAF (418)")
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"graphql errors: {payload['errors'][:1]}")
    return payload.get("data") or {}


def _normalize_graphql(t):
    published = None
    if t.get("publishedAt"):
        published = datetime.fromtimestamp(t["publishedAt"], tz=timezone.utc)

    # The API uses 0 to mean "no comparison price", which would read as free.
    next_best = t.get("nextBestPrice") or None
    price = t.get("price") or None

    return {
        "id": str(t.get("threadId") or t.get("url")),
        "title": strip_html(t.get("title")),
        "link": t.get("url") or "",
        "description": strip_html(t.get("description")),
        "price": price,
        "next_best_price": next_best,
        "temperature": int(t["temperature"]) if t.get("temperature") is not None else None,
        "type": t.get("type"),
        "merchant": (t.get("merchant") or {}).get("merchantName"),
        # The real outgoing URL is never exposed by the API. linkHost is the
        # merchant domain and is populated for every deal.
        "link_host": t.get("linkHost"),
        "voucher_code": t.get("voucherCode"),
        "is_expired": bool(t.get("isExpired")),
        "comment_count": t.get("commentCount"),
        "image_url": image_url(t.get("mainImage")),
        "published": published.isoformat() if published else None,
        "_published_dt": published,
        "source": "graphql",
    }


def fetch_graphql():
    """Both queries, merged and deduped. Raises on failure so RSS can take over."""
    session, token = _session()
    deals, seen = [], set()

    for label, query in (("hottestWidget", QUERY_HOTTEST), ("threads", QUERY_THREADS)):
        data = _query(session, token, query, {"filter": {}})
        threads = (
            data.get("hottestWidget", {}).get("threads")
            if label == "hottestWidget"
            else data.get("threads")
        ) or []
        added = 0
        for t in threads:
            deal = _normalize_graphql(t)
            if not deal["id"] or deal["id"] in seen or not deal["title"]:
                continue
            seen.add(deal["id"])
            deals.append(deal)
            added += 1
        print(f"  graphql {label}: {added}")
        time.sleep(2.5)  # stay well under the WAF rate limit

    if not deals:
        raise RuntimeError("graphql returned no deals")
    return deals


# ---------- RSS fallback ----------

def _normalize_rss(entry):
    published = None
    if getattr(entry, "published_parsed", None):
        published = datetime.fromtimestamp(
            time.mktime(entry.published_parsed), tz=timezone.utc
        )

    title = strip_html(entry.get("title", ""))
    if not title:
        return None

    temp = re.search(r"(\d[\d.]*)\s*°", title)
    price = re.search(r"(\d+[.,]?\d*)\s*€|€\s*(\d+[.,]?\d*)", title)
    price_val = None
    if price:
        raw = (price.group(1) or price.group(2)).replace(",", ".")
        try:
            price_val = float(raw)
        except ValueError:
            pass

    return {
        "id": entry.get("id") or entry.get("link"),
        "title": title,
        "link": entry.get("link", ""),
        "description": strip_html(entry.get("summary", "")),
        "price": price_val,
        "next_best_price": None,
        "temperature": int(temp.group(1).replace(".", "")) if temp else None,
        "type": None,
        "merchant": None,
        "link_host": None,
        "voucher_code": None,
        "is_expired": False,
        "comment_count": None,
        "image_url": None,
        "published": published.isoformat() if published else None,
        "_published_dt": published,
        "source": "rss",
    }


def fetch_rss():
    deals, seen = [], set()
    for url in RSS_FALLBACK:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            print(f"  rss {url}: parse failed", file=sys.stderr)
            continue
        added = 0
        for entry in feed.entries:
            deal = _normalize_rss(entry)
            if not deal or not deal["id"] or deal["id"] in seen:
                continue
            seen.add(deal["id"])
            deals.append(deal)
            added += 1
        print(f"  rss {url}: {added}")
    return deals


# ---------- public ----------

def fetch_deals(lookback_hours=14, max_items=80, use_graphql=True):
    """Return normalized deals, newest first. GraphQL with RSS fallback."""
    deals = []

    if use_graphql:
        try:
            deals = fetch_graphql()
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"  graphql failed ({exc}), falling back to RSS", file=sys.stderr)

    if not deals:
        deals = fetch_rss()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out = []
    for d in deals:
        if d.get("is_expired"):
            continue
        published = d.pop("_published_dt", None)
        if published and published < cutoff:
            continue
        out.append(d)

    out.sort(key=lambda d: d.get("published") or "", reverse=True)
    return out[:max_items]
