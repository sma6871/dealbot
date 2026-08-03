#!/usr/bin/env python3
"""Score today's deals and print the picks. Sends nothing, touches no state.

Use this while tuning rules.md and brands.md.

    export GEMINI_API_KEY=...
    python scripts/dry_run.py
    python scripts/dry_run.py --provider groq
    python scripts/dry_run.py --show-all
"""

import argparse
import os
import sys

import config
from fetch_and_score import available_providers, build_prompt, extract_picks, fetch_deals
from providers import REGISTRY


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", help="force one provider, skipping the fallback list")
    ap.add_argument("--show-all", action="store_true", help="also list every fetched deal")
    args = ap.parse_args()

    print("Fetching feeds:")
    deals = fetch_deals()
    print(f"{len(deals)} deals\n")
    if not deals:
        return

    if args.show_all:
        print("All fetched deals:")
        for i, d in enumerate(deals):
            print(f"  [{i:>3}] {d['temperature'] or '---':>5}° {d['title'][:90]}")
        print()

    providers = available_providers()
    if args.provider:
        providers = [(n, s) for n, s in providers if n == args.provider]
        if not providers:
            raise SystemExit(
                f"Provider '{args.provider}' has no API key set, or isn't in config.toml"
            )

    if not providers:
        raise SystemExit("No provider has its API key set")

    name, settings = providers[0]
    model = settings.get("model", "")
    print(f"Scoring with {name} ({model})...\n")

    raw = REGISTRY[name](build_prompt(deals), model, os.environ[settings["api_key_env"]])
    picks = extract_picks(raw, deals)

    if not picks:
        print("No picks. That's a valid outcome if nothing today fits the policy.")
        return

    print(f"{len(picks)} picks:\n")
    for i, deal in enumerate(picks, 1):
        print(f"{i}. {deal['title']}")
        print(f"   {deal['temperature'] or 'n/a'}° | {deal['reason']}")
        print(f"   {deal['link']}\n")


if __name__ == "__main__":
    main()
