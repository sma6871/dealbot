# dealbot

Curates deals from mydealz.de into a small Telegram channel. Low volume by
design: the filter is deliberately restrictive and rejects by default.

Day-to-day operating guide: [USAGE.md](USAGE.md)

## How it works

Two halves, because they have different needs.

**GitHub Actions (scheduled, Python)** — twice a day, reads the mydealz RSS
feeds, scores every new deal against `rules.md` and `brands.md` with an LLM, and
DMs me up to 10 candidates with ✅ Post / ❌ Skip buttons.

**Cloudflare Worker (instant, JS)** — everything interactive. Handles my button
taps immediately, generates a post draft from `post_template.md`, runs the
draft edit loop, handles `/batch`, and records every decision.

State lives in Cloudflare KV, since both halves write to it and a Git repo can't
be a live database. `data/log.csv` is the versioned archive, synced from KV every
6 hours.

```
mydealz RSS ──> fetch_and_score.py ──> my Telegram DM
                        │                    │ tap
                        v                    v
                   KV: seen, pending    Worker ──> draft ──> edit loop ──> channel
                                            │
                                       KV: decisions ──> sync_log.py ──> log.csv
```

## Files

| File | Purpose |
|---|---|
| `rules.md` | Editorial policy. The actual product. Edit this often. |
| `brands.md` | Brand and retailer lists referenced by the policy. |
| `post_template.md` | Post format. Also lives in the Worker's `POST_TEMPLATE` secret. |
| `config.toml` | Feeds, providers, candidate counts, lookback window. |
| `scripts/fetch_and_score.py` | The scheduled job. |
| `scripts/sync_log.py` | Health check + drains KV decisions into `log.csv`. |
| `scripts/dry_run.py` | Score without sending. Use while tuning rules. |
| `worker/index.js` | All interactive handling. |

## Setup

### 1. Telegram

- Create a bot with @BotFather, note the token
- Message the bot, then `curl https://api.telegram.org/bot<TOKEN>/getUpdates`
  to find your chat ID
- Add the bot to the channel as an admin with post permission
- Channel ID: forward a channel post to the bot, check `getUpdates` again for
  `forward_from_chat.id` (starts with `-100`)

### 2. Cloudflare

```bash
cd worker
npx wrangler kv namespace create DEALBOT   # paste the id into wrangler.toml
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_ADMIN_CHAT_ID
npx wrangler secret put TELEGRAM_CHANNEL_ID
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put GROQ_API_KEY
npx wrangler secret put WEBHOOK_SECRET      # any random string
npx wrangler secret put POST_TEMPLATE       # paste post_template.md contents
npx wrangler deploy
```

Register the webhook:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://dealbot.<you>.workers.dev/webhook",
       "secret_token":"<WEBHOOK_SECRET>",
       "allowed_updates":["message","callback_query"]}'
```

Verify: open `/health` in a browser, and send `/status` to the bot.

### 3. GitHub secrets

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`, `GEMINI_API_KEY`,
`GROQ_API_KEY`, `CF_ACCOUNT_ID`, `CF_KV_NAMESPACE_ID`, `CF_API_TOKEN`,
`WORKER_HEALTH_URL`

The Cloudflare API token needs **Workers KV Storage: Edit** on your account.

### 4. Local dry run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...
python scripts/dry_run.py
python scripts/dry_run.py --show-all --provider groq
```

Only needs a provider key. No Telegram or Cloudflare access, sends nothing.

## Daily use

- Twice a day, candidates arrive in the DM
- ✅ Post generates a draft, then ✅ Send / ✏️ Edit / ❌ Discard
- ✏️ Edit: reply in plain language ("shorter", "add the TCO math"), get a new draft
- ❌ Skip: optionally reply with a reason. Reasons on deals you disagreed with are
  the most valuable training data.
- `/batch 20` sends raw unscored deals with 👍 / 👎, for training the rules only.
  Never posts anything.
- `/status` shows worker stats

## Tuning the rules

The point of `log.csv` is that it accumulates the cases where the model's
judgment and mine differ. Every few weeks, read it and update `rules.md` and
`brands.md` from the disagreements. Don't try to get the rules right up front —
they're supposed to be roughly right and improve from evidence.

## Notes

- Webhook and `getUpdates` polling are mutually exclusive. If you ever poll, the
  Worker stops receiving updates.
- KV is eventually consistent. An occasional duplicate deal is not a bug.
- Groq model IDs get retired. Check console.groq.com/docs/models if it fails.
- `/rss/alle` returns zero entries. Use `/rss/hot`, `/rss`, `/rss/deals`.
