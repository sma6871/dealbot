# dealbot

Fetches hot deals from mydealz, scores them against `rules.md`, and DMs the top candidates to you on Telegram for Post/Skip. Approved deals go to your channel. Decisions land in `data/log.csv`.

## Scoring providers

`config.toml` controls which models are tried and in what order:

```toml
[scoring]
providers = ["gemini", "groq"]   # first success wins
```

Each provider has its own section (`model`, `api_key_env`). Put the one you prefer first; if that call fails or returns unusable output, the next is tried. Switch by editing the `providers` list (or use `--provider` in the dry run).

## Env vars

Set these as GitHub Actions secrets (and export locally as needed):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_CHAT_ID`
- `TELEGRAM_CHANNEL_ID`
- `GEMINI_API_KEY` and/or `GROQ_API_KEY` — at least one of the providers listed in `config.toml` must have its key set

See `.env.example` for the name list.

## Local dry run

Scores deals and prints picks to stdout. Needs a scoring API key (no Telegram).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...   # and/or GROQ_API_KEY=...
python scripts/dry_run.py
python scripts/dry_run.py --provider groq    # force one provider
```

Does not send Telegram messages and does not update `data/`.

## GitHub Actions

- **Fetch and score deals** — pull feed, score, DM candidates
- **Sync worker decisions** — health check and drain KV decisions into `data/log.csv`

## Decision log

`data/log.csv` — one row per post/skip/reason, committed back by the workflows.
