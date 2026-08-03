#!/usr/bin/env python3
"""Check the Worker is alive, and drain its decision log into data/log.csv.

The Worker records my taps into a KV array because it can't commit to Git.
This job moves them into the repo so the history is versioned and greppable.
"""

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

import kv

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "log.csv"

FIELDS = [
    "timestamp", "decision", "title", "link", "temperature",
    "model_reason", "my_reason", "sent_text", "edit_rounds",
]

STALE_HOURS = 48


def require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))


def notify(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    if not (token and chat_id):
        print("Cannot notify, Telegram vars unset", file=sys.stderr)
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )


def check_health():
    url = os.environ.get("WORKER_HEALTH_URL")
    if not url:
        print("WORKER_HEALTH_URL unset, skipping health check")
        return

    try:
        r = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        notify(f"⚠️ dealbot worker unreachable: {exc}")
        print(f"Health check failed: {exc}", file=sys.stderr)
        return

    if not r.ok:
        notify(f"⚠️ dealbot worker returned HTTP {r.status_code}")
        print(f"Health check HTTP {r.status_code}", file=sys.stderr)
        return

    data = r.json()
    print(f"Worker OK: {data}")

    last = data.get("lastUpdate")
    if last:
        try:
            seen_at = datetime.fromisoformat(last.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - seen_at).total_seconds() / 3600
            if age > STALE_HOURS:
                notify(
                    f"⚠️ dealbot worker responds but hasn't handled an update in "
                    f"{age:.0f}h. Check the Telegram webhook registration."
                )
        except ValueError:
            pass


def drain_decisions():
    decisions = kv.get_json("decisions", []) or []
    if not decisions:
        print("No new decisions")
        return 0

    LOG.parent.mkdir(parents=True, exist_ok=True)
    is_new = not LOG.exists()

    with LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        for row in decisions:
            writer.writerow({k: row.get(k, "") for k in FIELDS})

    # Only clear after the write succeeded.
    kv.put_json("decisions", [])
    print(f"Appended {len(decisions)} decisions to {LOG.name}")
    return len(decisions)


def main():
    require_env("CF_ACCOUNT_ID", "CF_KV_NAMESPACE_ID", "CF_API_TOKEN")
    check_health()
    drain_decisions()


if __name__ == "__main__":
    main()
