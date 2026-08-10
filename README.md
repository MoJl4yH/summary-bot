# TG Summary Bot

**English** | [Русский](README.ru.md)

A Telegram bot that quietly logs messages in your group chat and posts a daily digest of what was discussed — with clickable links to the start of each discussion topic.

Example digest:

> 📅 **Digest for 10.08.2026**
>
> 🔹 [Landing page redesign](https://t.me/c/xxx/201): Oleg, Maria and Ivan decided to simplify the header — drop the carousel, keep a single offer.
> 🔹 [SBP payments](https://t.me/c/xxx/205): Anya raised the payments question, Maria suggested YooKassa with lower fees.
>
> **Day's mood:** Productive day — clear next steps agreed on every topic

## How it works

1. The bot saves all text messages and media captions from the configured groups into a local SQLite database (`data/messages.db`), including reply chains.
2. Every day at the configured time (`SUMMARY_TIME`, default 22:00) the last 24 hours of messages are formatted as a log (`[HH:MM] Name (ID: 123): text`) and sent to an LLM via [OpenRouter](https://openrouter.ai/).
3. The LLM returns structured JSON: discussion topics, a one-line summary with participant names, and the `start_message_id` of each topic.
4. The bot builds a digest where every topic links to the first message of that discussion (`https://t.me/c/<chat_id>/<message_id>`) and posts it to the chat.
5. After a successful scheduled run the processed messages are deleted; records older than 48 hours are cleaned up automatically.

## Setup

```bash
git clone <repo-url> && cd tg-summary-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_GROUP_CHAT_IDS` | Group chat ID(s), comma-separated, e.g. `-1001234567890` |
| `SUMMARY_TIME` | Daily posting time, `HH:MM` (default `22:00`) |
| `TIMEZONE` | Timezone for the schedule and message timestamps (default `Europe/Moscow`) |
| `OPENROUTER_API_KEY` | [OpenRouter](https://openrouter.ai/) API key for summary generation |
| `LLM_MODEL` | Model name (default `google/gemini-3-flash-preview`) |
| `DATABASE_PATH` | SQLite path (optional, default `./data/messages.db`) |

## Telegram configuration

1. Create a bot with @BotFather and **disable privacy mode** (`/setprivacy` → Disable) — otherwise the bot won't see regular group messages.
2. Add the bot to your group.
3. Send `/chatid` in the group — the bot replies with the chat ID; put it into `TELEGRAM_GROUP_CHAT_IDS`.
4. Restart the bot.

## Run

```bash
python bot.py
```

## Commands

- `/summary` (or `/daily_summary`) — generate a digest for the last 24 hours on demand (does not delete messages from the database).
- `/chatid` — show the current chat ID.

## Notes

- `t.me/c/...` links work for supergroups (IDs starting with `-100`) and open only for chat members.
- The system prompt for topic extraction lives in [prompts/daily_summary.txt](prompts/daily_summary.txt) — tune it to your chat's style.
- No external database required: everything is stored in a single SQLite file.
