# How to use dealbot

A short operating guide. Not setup instructions — see `README.md` for those.

---

## The one-line version

Candidates arrive twice a day. You tap twice to publish. Every tap becomes
training data that sharpens the filter later.

---

## Daily loop

**09:00 and 21:00 (Berlin), automatic**

Candidates land in your Telegram DM. Up to 10, usually far fewer. Some days
zero — that is correct behaviour, not a failure. The policy in `rules.md`
defaults to rejecting.

**For each candidate:**

| Tap | What happens |
|---|---|
| ❌ **Skip** | Logged. Bot asks why. Reply if you have a reason, ignore if not. |
| ✅ **Post** | Does NOT post. Generates an English + Persian draft and sends it back. |

**Then, on the draft:**

| Tap | What happens |
|---|---|
| ✅ **Send** | Posts to the channel. This is the only action that publishes. |
| ✏️ **Edit** | Reply in plain language. Get a new draft. Repeat up to 10 times. |
| ❌ **Discard** | Throws the draft away. |

Edit instructions are just normal sentences:
- "shorter"
- "add the total cost math"
- "fix the Persian numbers"
- "mention it needs a Zalando Plus account"

**Nothing reaches the channel without two deliberate taps.**

---

## Reasons: when they matter

Only write a reason when you **disagree with the model or the temperature**.

- Hot deal you rejected → valuable
- Lukewarm deal you loved → valuable
- Obvious junk you skipped → skip the reason, it teaches nothing

The disagreements are where your editorial judgment actually lives. Everything
else is noise.

---

## Batch mode: training the rules

When you have ten spare minutes:

```
/batch 20
```

- Sends 20 raw, unscored, unfiltered deals
- Two buttons only: 👍 / 👎
- **Posts nothing.** This is purely for training.
- 👎 optionally lets you reply with a reason

This is how `brands.md` gets filled in. The clothing, electronics, and perfume
deals you 👍 *are* your brand list. Don't write that file from imagination —
let batch mode reveal it.

Max is `/batch 50`. Deals sent in batch mode are marked seen and won't come back
as candidates.

---

## Other commands

```
/status
```

Shows whether the worker is alive, how many updates it has handled, how many
decisions haven't been synced to the repo yet, and the last error.

---

## Every few weeks: the feedback loop

1. Open `data/log.csv` in the repo
2. Send it to Claude
3. Claude reads where your decisions diverged from the model's picks and rewrites
   `rules.md` and `brands.md`
4. Commit the new files

**Nothing retrains automatically.** No model is fine-tuned, no weights change.
The "learning" is literally text in `rules.md` getting better. You approve every
change.

---

## Running in the background

| What | When | Does |
|---|---|---|
| `fetch.yml` | 09:00, 21:00 | Fetch, score, DM candidates |
| `sync.yml` | every 6h | Health check + copy decisions into `data/log.csv` |
| `deploy-worker.yml` | on push to `worker/` | Redeploys the worker |

If the worker dies, `sync.yml` will DM you within 6 hours.

---

## Tuning `rules.md` without burning deals

```bash
source .venv/bin/activate
export GEMINI_API_KEY=...
python scripts/dry_run.py
python scripts/dry_run.py --show-all
```

Scores today's deals and prints the picks. Sends nothing, marks nothing as seen,
doesn't touch KV. Use this whenever you edit `rules.md` and want to see the
effect immediately.

`--show-all` lists every fetched deal, not just the picks. Often more useful —
the deals it **rejected** tell you where the rules are too strict.

---

## Judging the system

Ask one question per candidate: **would I have posted this?**

| Answer | Do |
|---|---|
| Yes | Nothing. Leave the rules alone. |
| No | Note which rule it misread. Fix after a few examples, not one. |
| "It missed better ones" | Run `/batch 20` and 👍 what it should have caught. |

**Do not rewrite `rules.md` after one run.** One sample tells you nothing.
Collect three or four runs of evidence first — otherwise you're guessing and
calling it tuning.

Expect the first week to feel slightly wrong. That's the point of the log.

---

## Things that look like bugs but aren't

- **Zero candidates today.** The policy is restrictive by design.
- **One candidate out of 60 deals.** Also normal.
- **A duplicate deal appears.** Cloudflare KV is eventually consistent. Ignore it.
- **A `VERIFY:` prefix on a reason.** The model can't check idealo prices,
  recurring promotions, or live cashback stacks. It's asking you to check
  manually before sending.
- **Button taps feel instant but the log is empty.** Decisions sit in KV until
  `sync.yml` runs, up to 6 hours later.

---

## The one thing that actually matters

`rules.md` is the product. Everything else is plumbing.

The system's job is not to be clever — it's to turn your gut feeling into text
you can read, argue with, and improve. If in three months `rules.md` reads like
something you actually believe, this worked.
