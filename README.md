# JobWatch

Zero-cost job watcher. Runs on GitHub Actions every 4 hours and pushes new
matches straight to Telegram. No LLM is invoked anywhere in this repo.

- `jobwatch.py` — fetch, filter, score, dedupe, notify
- `jobwatch_config.json` — all tuning (keywords, weights, employers)
- `state/` — `seen.json` + `jobs_history.jsonl`, committed back each run

## Secrets required

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID`   | Your numeric Telegram user ID |

## Local use

    python jobwatch.py --test       # preview, saves nothing
    python jobwatch.py --telegram   # run and notify
    python jobwatch.py --stats
