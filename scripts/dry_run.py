#!/usr/bin/env python3
"""Fetch and score deals, print picks to stdout. No Telegram."""

from fetch_and_score import ROOT, fetch_deals, load, require_env, score


def main():
    require_env("GEMINI_API_KEY")

    rules = (ROOT / "rules.md").read_text(encoding="utf-8")
    seen = load("seen.json", [])
    seen_set = set(seen)

    deals = [d for d in fetch_deals() if d["id"] not in seen_set]
    print(f"{len(deals)} new deals in feed")
    if not deals:
        return

    picks = score(deals, rules)
    print(f"model picked {len(picks)}")
    if not picks:
        return

    for i, deal in enumerate(picks, 1):
        print()
        print(f"--- pick {i} ---")
        print(deal["title"])
        print(f"temp: {deal.get('temperature') or 'n/a'} | price: {deal.get('price') or 'n/a'}")
        print(deal.get("reason", ""))
        print(deal.get("link", ""))


if __name__ == "__main__":
    main()
