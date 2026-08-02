# dealbot

Fetches hot deals from mydealz, scores them against `rules.md` with Gemini, and DMs the top candidates to you on Telegram for Post/Skip. Approved deals go to your channel. Decisions land in `data/log.csv`.

## Env vars

Set these as GitHub Actions secrets (and export locally as needed):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_CHAT_ID`
- `TELEGRAM_CHANNEL_ID`
- `GEMINI_API_KEY`
- `GEMINI_MODEL` (optional; defaults to `gemini-flash-latest`)

See `.env.example` for the name list.

## Local dry run

Scores deals and prints picks to stdout. Needs only `GEMINI_API_KEY` (no Telegram).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...
python scripts/dry_run.py
```

Does not send Telegram messages and does not update `data/`.

## Manual workflow runs

In the GitHub repo: **Actions** → pick the workflow → **Run workflow**.

- **Fetch and score deals** — pull feed, score, DM candidates
- **Process my taps** — handle Post/Skip callbacks and optional reply-reasons

## Decision log

`data/log.csv` — one row per post/skip/reason, committed back by the workflows.
