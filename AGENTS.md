# AGENTS.md

Entry point for any AI agent working on this repo. Read this first.

`README.md` is setup. `USAGE.md` is how to operate the bot day to day. This file
is *why things are the way they are* and *what's currently true*.

**Keep this file updated.** This file is the memory; chat history is not.

Before you end a session, check each of these and say what you updated:

- [ ] Something now works that didn't → "Current state"
- [ ] A bug was found or fixed → "Open items"
- [ ] A decision was made → "Decisions", with the reasoning
- [ ] Something cost time to figure out → "Gotchas"
- [ ] An idea was postponed → "Parked", with why

---

## What this is

A deal curation pipeline for a small Telegram channel, `@berliner_deals`,
~30 subscribers in Berlin, mostly immigrants working in tech.

The channel's value is that it posts **rarely**. Roughly 1-3 posts a week. A
mediocre post is worse than no post. Everything in the design serves that.

**The actual product is `rules.md`** — a prose editorial policy that an LLM
applies. Everything else is plumbing. The system exists to turn the owner's gut
feeling into text he can read, argue with, and improve. Judge changes by whether
they make `rules.md` better, not by whether they add capability.

---

## Architecture

Two halves, split because they have different needs.

```
mydealz GraphQL ──> fetch_and_score.py ──> owner's Telegram DM
   (GitHub Actions, 2x/day)                      │ tap
                     │                           v
                     v                    Cloudflare Worker
              KV: seen, pending:*         (instant webhook)
                                                 │
                                          draft ──> edit loop ──> channel
                                                 │
                                      KV: decisions ──> sync_log.py ──> log.csv
```

- **GitHub Actions (Python)** — scheduled work. Fetch, score, DM candidates.
- **Cloudflare Worker (JS)** — everything interactive. Button taps, draft
  generation, edit loop, `/batch`, `/status`.
- **Cloudflare KV** — shared live state. Both halves write to it.
- **`data/log.csv`** — the versioned archive, drained from KV every 6h.

### Why state is in KV and not the repo

Both halves write to it, and Git cannot act as a live database for two writers.
This was learned the hard way — see "Gotchas".

---

## Files

| File | Role |
|---|---|
| `rules.md` | The editorial policy. **The product.** Edit often. |
| `brands.md` | Brand/retailer lists referenced by the policy. Mostly empty on purpose. |
| `post_template.md` | Post format, EN + FA. Also duplicated into the Worker's `POST_TEMPLATE` secret. |
| `config.toml` | Feeds, providers, candidate counts, description truncation. |
| `scripts/mydealz.py` | GraphQL client with automatic RSS fallback. |
| `scripts/fetch_and_score.py` | The scheduled job. One LLM call per run. |
| `scripts/providers.py` | Gemini + Groq. Dict dispatch, no abstraction. |
| `scripts/config.py` | Loads `config.toml` via stdlib `tomllib`. |
| `scripts/kv.py` | Cloudflare KV REST wrapper. |
| `scripts/sync_log.py` | Worker health check + drains KV decisions to `log.csv`. |
| `scripts/dry_run.py` | Score without sending. Use when tuning `rules.md`. |
| `worker/index.js` | All interactive handling. 496 lines. |
| `.github/workflows/fetch.yml` | 07:00 and 19:00 UTC. |
| `.github/workflows/sync.yml` | Every 6h. |
| `.github/workflows/deploy-worker.yml` | On push to `worker/`. |

---

## Decisions, and why

- **GraphQL over RSS.** RSS truncates descriptions to ~157 chars. GraphQL gives
  full descriptions (median ~1000, max 10k), plus `nextBestPrice` (the compare-at
  price), `merchantName`, and `type` (Deal/Voucher/Freebie). This fixed the
  model judging discounts against its own stale price knowledge.
  RSS is kept as automatic fallback because the API is private and undocumented.

- **Two taps to publish.** ✅ Post generates a draft; ✅ Send publishes. Nothing
  reaches the channel accidentally. The private DM *is* the staging environment.

- **`/batch` never posts.** It exists purely to generate training data for
  `rules.md` and `brands.md`. Keep it that way.

- **Reasons only when disagreeing.** Requiring a reason on every skip kills the
  habit. Reasons matter when the owner and the model disagree — hot deal
  rejected, lukewarm deal loved. The easy calls teach nothing.

- **`brands.md` stays mostly empty.** It gets filled from actual 👍 decisions in
  batch mode, not from imagination. A list of brands the owner *thinks* he likes
  is worse than no list.

- **Nothing retrains automatically.** No fine-tuning, no weight updates. The
  "learning" is `rules.md` getting better prose, reviewed and committed by hand.

- **Cloudflare Worker, not GitHub Actions polling.** The original design polled
  Telegram every 10 min via `review.yml`. Deleted. The webhook is instant and
  free.

- **Worker deploys via GitHub Actions**, not Cloudflare's Build integration.
  Don't enable both — double deploys.

- **Posts must not leak mydealz as the source.** Use `linkHost` (the merchant
  domain, populated for every deal) to name the shop. The real outgoing URL is
  NOT obtainable — see Gotchas.

---

## Current state

**Working end to end.** Verified: GraphQL fetch (16 hottest + 30 newest, ~45
after dedupe/filtering), Gemini scoring, candidates arriving in DM, buttons
responding instantly, draft generation, edit loop.

**`✅ Send` root cause found:** `TELEGRAM_CHANNEL_ID` was never set in the
Cloudflare secrets. The handler's bugs (below) hid this by reporting success.

**Feedback loop status:** instrumented but never run. `log.csv` is accumulating
decisions. The manual "read the log, rewrite the rules" step has happened **zero
times**. That step is the whole point; everything else is ready for it.

**Cost:** ~€2.40/month on Gemini 3.6 Flash, now reduced. One LLM call per fetch
run, plus one per draft and per edit round. €5/month spend cap set.
`thinkingLevel: "low"` cut output tokens from ~4,000 to ~2,000 per scoring call.
Current per-call usage: ~13k input, ~2k output.

---

## Open items

Ordered by value, highest first.

1. **Run the feedback loop once.** Send `log.csv` to Claude, get rewritten
   `rules.md` and `brands.md`. This has never been done and is the reason the
   system exists.

2. **`✅ Send` handler bugs** in `worker/index.js`. The missing
   `TELEGRAM_CHANNEL_ID` is fixed, but the handler still:
   - Says "Posted ✅" without checking whether `sendMessage` succeeded
   - Deletes `draft:<mid>` from KV even on failure, so the draft is unrecoverable
   - Never reports the actual Telegram error to the owner

   A draft was permanently lost to this. Fix before relying on Send.

2b. **`thinkingConfig` is misplaced in `worker/index.js`** — it sits as a sibling
   of `generationConfig` instead of inside it, so it is silently ignored and
   drafts still use default (high) reasoning.

3. **Error reporting to chat generally.** Failures currently go to
   `console.error`, visible only in Cloudflare's log stream. They should reach
   the owner's DM.

4. **`silent return` on missing pending deal.** In `handleCallback`, buttons are
   stripped *before* the deal lookup, and a missing deal returns silently. The
   owner sees buttons vanish with no explanation. Should say "this deal expired
   or wasn't found".

5. **New bot commands.** Owner has a list, not yet provided.

6. **`thinkingBudget`.** Gemini 3.x emits ~4,000 reasoning tokens per call
   against ~100 tokens of actual JSON output. Setting
   `"thinkingConfig": {"thinkingBudget": 0}` in `providers.py` would cut cost
   ~80-90%. **Test with `dry_run.py` first** — compare picks before and after.
   Suspicion: reject decisions won't change (rule matching), but TCO math on
   contract deals might degrade. Contract deals are the highest-value category,
   so check those specifically. `512` is the middle-ground option.

### Parked, deliberately

Do not build these without the owner explicitly asking. Each was considered and
postponed for a stated reason.

- **Image posts.** Every deal already carries `image_url` from GraphQL. Blocked
  on: captions cap at 1024 chars and the bilingual posts often exceed that, so it
  needs photo + separate text logic. Also unclear how often it's actually wanted.
- **Expired post auto-marking.** `sync.yml` could check `isExpired` and edit
  posted messages to prepend ⛔️. Needs the Worker to store `posted:<msg_id>`
  first. Not worth it below a few posts a week. Cheaper fix: always include
  `⏰ Valid until:` in posts so expiry is self-documenting.
- **OpenRouter as a provider.** ~15 lines, saves €1-2/month. Not worth the extra
  hop and the quality gamble.
- **Hermes Agent as project management platform.** Interesting but premature —
  would be automating a task performed zero times so far.

---

## Gotchas learned the hard way

Each of these cost real time. Don't rediscover them.

- **Telegram webhooks and `getUpdates` polling are mutually exclusive.** If
  anything polls, the Worker silently stops receiving updates.
- **Two KV namespaces will silently desync the two halves.** `wrangler.toml`'s
  `[[kv_namespaces]].id` and the `CF_KV_NAMESPACE_ID` GitHub secret must be the
  same namespace. Symptom: buttons vanish, nothing logged, KV looks empty.
- **`wrangler deploy` overwrites dashboard bindings and `[vars]`** from
  `wrangler.toml`. Secrets survive. Since deploys are via git, `wrangler.toml` is
  the source of truth for bindings — not the dashboard.
- **GitHub *Environment* secrets are not *Repository* secrets.** Environment
  secrets only reach jobs that declare that environment. These workflows don't.
  Symptom: secrets resolve to empty strings and the script reports them missing.
- **The Cloudflare API token needs both** `Workers Scripts: Edit` (deploy) and
  `Workers KV Storage: Edit` (the fetch job). Missing the first gives
  `Authentication error [code: 10000]` while still listing the account.
- **GitHub's web upload picker skips dotfiles and `.github/`.** Files uploaded
  that way silently don't land. Type the path in "Create new file" instead.
- **`nextBestPrice` returns `0` to mean "absent"**, which reads as free.
  Normalized to `None` in `mydealz.py`.
- **The `groups { groupsPath }` field in ha-pepper's API docs is wrong.** No
  sub-selection works. Dropped; categories aren't needed.
- **`/rss/alle` returns zero entries.** Working RSS feeds: `/rss/hot`, `/rss`,
  `/rss/deals`. `/rss/deals` overlaps `/rss/hot` almost entirely.
- **mydealz GraphQL rate-limits aggressively** — ~5-8 rapid queries triggers
  HTTP 418. Two queries per run with a 2.5s sleep is well inside it. A homepage
  GET for cookies + `xsrf_t` is required first; sessions don't persist.
- **The real merchant "Zum Deal" URL cannot be retrieved.** Verified by probing:
  `link` and `cpcLink` are null for all threads in the GraphQL feed, and the deal
  page's server HTML doesn't contain it either — the button is client-rendered.
  This is deliberate; the outgoing link is mydealz's affiliate revenue. Links
  found inside `description` (~13/30 deals) are price-comparison and review links
  (keepa, idealo, geizhals, YouTube), not the deal. Only `linkHost` is available.
  Don't re-investigate this.
- **`shareableLink` errors server-side** in GraphQL (`Internal server error`).
  Don't request it.
- **The Gemini model is configured in TWO places:** root `config.toml` `[gemini]`
  for scoring, and `worker/wrangler.toml` `GEMINI_MODEL` for drafts. Changing one
  does not change the other.
- **Gemini 3.x uses `thinkingLevel`, not `thinkingBudget`.** Sending
  `thinkingBudget` to a 3.x model, or both parameters together, returns 400.
  Valid levels for 3.6 Flash: `minimal`, `low`, `medium`, `high`. Full
  thinking-off is not supported. `thinkingConfig` must be nested INSIDE
  `generationConfig`.
- **Don't pin Gemini to `gemini-flash-latest`.** It's an alias that resolves
  differently across endpoints, and on the legacy `generateContent` endpoint it
  can land on a 2.5-series model that rejects `thinkingLevel`. Pin an explicit
  version.
- **Re-running a GitHub Actions workflow replays that run's commit**, not current
  `main`. Use the "Run workflow" button to pick up new code. This caused three
  rounds of chasing an error that had already been fixed.
- **Always surface API error bodies.** `raise_for_status()` throws away the
  message. Google's 400 body names the exact rejected field. Use
  `if not r.ok: raise ValueError(f"gemini {r.status_code}: {r.text[:500]}")`.
- **Groq's free tier cannot score.** One scoring prompt is ~13-16k tokens, over
  the 8k tokens/minute free limit — it returns 413. The fallback only works for
  drafting (~3-4k tokens). Groq is not a real safety net for scoring.
- **KV is eventually consistent.** An occasional duplicate deal is not a bug.
- **`git add data/log.csv` fails when the file doesn't exist**, which is the
  normal case before the first sync. `sync.yml` guards for this.

---

## Conventions

- **Boring, obvious code.** One user, personal tool. No abstraction layers, no
  registries, no plugin systems, no dependency injection.
- **No tests, no linters, no Docker, no type stubs.** Deliberate.
- **Python: stdlib + `requests` + `feedparser`.** Nothing else. `tomllib` for
  config, not pyyaml or pydantic.
- **Fail loudly on missing config**, listing every missing variable at once.
- **Catch specific exceptions**, not bare `except`.
- **The owner fights a documented tendency to over-engineer.** When proposing
  changes, prefer the smallest version that produces evidence. Say plainly when
  something isn't worth building yet, and why.
- **Don't change `rules.md` content without being asked.** It's the owner's
  editorial judgment, not the agent's.
- **`post_template.md` is duplicated** into the Worker's `POST_TEMPLATE` secret.
  Editing the file alone changes nothing — the secret must be re-set.

---

## Environment

**GitHub secrets** (Repository, not Environment): `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ADMIN_CHAT_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `CF_ACCOUNT_ID`,
`CF_KV_NAMESPACE_ID`, `CF_API_TOKEN`, `WORKER_HEALTH_URL`

**Cloudflare Worker secrets**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`,
`TELEGRAM_CHANNEL_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `WEBHOOK_SECRET`,
`POST_TEMPLATE`

`TELEGRAM_CHANNEL_ID` lives **only** in Cloudflare — only the Worker posts.

**KV keys**: `seen` (list), `pending:<msg_id>`, `draft:<msg_id>`,
`awaiting:<chat_id>`, `decisions` (drained by sync), `stats`

**Worker URL**: `https://dealbot.masouda65.workers.dev` — `/health` and
`/webhook`
