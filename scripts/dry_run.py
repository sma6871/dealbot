#!/usr/bin/env python3
"""Fetch and score deals, print picks to stdout. No Telegram."""

import argparse

from fetch_and_score import (
    CONFIG,
    ROOT,
    fetch_deals,
    load,
    require_scoring_keys,
    score,
)
from providers import PROVIDERS


def main():
    parser = argparse.ArgumentParser(description="Dry-run deal scoring (no Telegram).")
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        help="Force one provider (skip fallback list). Default: config order.",
    )
    args = parser.parse_args()

    if args.provider:
        require_scoring_keys([args.provider])
    else:
        require_scoring_keys()

    rules = (ROOT / "rules.md").read_text(encoding="utf-8")
    seen = load("seen.json", [])
    seen_set = set(seen)

    deals = [d for d in fetch_deals() if d["id"] not in seen_set]
    print(f"{len(deals)} new deals in feed")
    if not deals:
        return

    if args.provider:
        print(f"provider forced: {args.provider}")
    else:
        print(f"providers: {', '.join(CONFIG['providers'])}")

    picks = score(deals, rules, provider=args.provider)
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
