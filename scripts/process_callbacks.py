#!/usr/bin/env python3
"""Poll Telegram for button taps and reply-reasons, act on them, log everything."""

import csv
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG = DATA / "log.csv"

LOG_FIELDS = [
    "timestamp", "decision", "title", "link", "temperature",
    "price", "model_reason", "my_reason",
]


def require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(
            "Missing required environment variables:\n"
            + "\n".join(f"  - {n}" for n in missing),
            file=sys.stderr,
        )
        sys.exit(1)


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


def append_log(row):
    DATA.mkdir(exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LOG_FIELDS})


def tg(method, **payload):
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.post(
        f"https://api.telegram.org/bot{bot_token}/{method}",
        json=payload,
        timeout=30,
    )
    if not r.ok:
        print(f"{method} failed: {r.text}", file=sys.stderr)
    return r


def post_to_channel(deal):
    text = f"<b>{html.escape(deal['title'])}</b>\n\n{html.escape(deal['link'])}"
    tg("sendMessage", chat_id=os.environ["TELEGRAM_CHANNEL_ID"], text=text, parse_mode="HTML")


def handle_callback(cb, pending):
    mid = str(cb["message"]["message_id"])
    deal = pending.get(mid)
    action = cb.get("data")

    tg("answerCallbackQuery", callback_query_id=cb["id"])

    if not deal:
        tg("editMessageReplyMarkup", chat_id=cb["message"]["chat"]["id"],
           message_id=int(mid), reply_markup={"inline_keyboard": []})
        return

    if action == "post":
        post_to_channel(deal)
        note = "✅ Posted"
    else:
        note = "❌ Skipped — reply to this message with a reason (optional)"

    tg("editMessageText",
       chat_id=cb["message"]["chat"]["id"],
       message_id=int(mid),
       text=cb["message"].get("text", "") + f"\n\n— {note}",
       link_preview_options={"is_disabled": True})

    append_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": "post" if action == "post" else "skip",
        "title": deal["title"],
        "link": deal["link"],
        "temperature": deal.get("temperature") or "",
        "price": deal.get("price") or "",
        "model_reason": deal.get("reason", ""),
        "my_reason": "",
    })

    if action == "post":
        pending.pop(mid, None)
    else:
        # Keep it around briefly so a reply can still attach a reason.
        deal["awaiting_reason"] = True


def handle_reason(msg, pending):
    """A reply to a reviewed deal is treated as the reason for that decision."""
    parent = str(msg["reply_to_message"]["message_id"])
    deal = pending.get(parent)
    if not deal:
        return

    reason = msg.get("text", "").strip()
    append_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": "reason",
        "title": deal["title"],
        "link": deal["link"],
        "temperature": deal.get("temperature") or "",
        "price": deal.get("price") or "",
        "model_reason": deal.get("reason", ""),
        "my_reason": reason,
    })
    tg("sendMessage", chat_id=msg["chat"]["id"], text="Noted 👍",
       reply_to_message_id=msg["message_id"])
    pending.pop(parent, None)


def main():
    require_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_CHAT_ID", "TELEGRAM_CHANNEL_ID")

    admin_chat_id = str(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
    offset = load("offset.json", {}).get("offset", 0)
    pending = load("pending.json", {})

    r = requests.get(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/getUpdates",
        params={"offset": offset, "timeout": 0, "limit": 100,
                "allowed_updates": json.dumps(["callback_query", "message"])},
        timeout=40,
    )
    r.raise_for_status()
    updates = r.json().get("result", [])
    print(f"{len(updates)} updates")

    for u in updates:
        offset = u["update_id"] + 1
        try:
            if "callback_query" in u:
                cb = u["callback_query"]
                if str(cb["from"]["id"]) == admin_chat_id:
                    handle_callback(cb, pending)
            elif "message" in u:
                msg = u["message"]
                if (str(msg.get("chat", {}).get("id")) == admin_chat_id
                        and "reply_to_message" in msg and msg.get("text")):
                    handle_reason(msg, pending)
        except Exception as exc:  # never let one bad update block the offset
            print(f"error on update {u.get('update_id')}: {exc}", file=sys.stderr)

    save("offset.json", {"offset": offset})
    save("pending.json", pending)


if __name__ == "__main__":
    main()
