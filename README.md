# news-digest-bot

[![tests](https://github.com/nestdaddy/news-digest-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/nestdaddy/news-digest-bot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A small Python bot that sends you **one clean news digest every morning** to Telegram, Slack or email:

- latest **world, tech and markets** headlines
- **keyword alerts** for the topics you track (a company, a product, a technology), optionally per country
- **no repeats**: stories already sent are skipped for 14 days
- runs **free on GitHub Actions**, no server needed

```
YOUR DAILY NEWS DIGEST

WORLD
- Story title (source.com)
  https://source.com/...

TECH
- ...

ALERT: ELECTRIC VEHICLES (US)
- ...
```

News comes from the [NestDaddy News API](https://nestdaddy.com/apis): headlines and news search across 45+ countries, returned as JSON.

## Quick start (GitHub Actions, about 5 minutes)

1. Click **Use this template** at the top of this page and create your own copy.
2. Get a NestDaddy API key at [nestdaddy.com/mcp](https://nestdaddy.com/mcp) (free 7-day trial). The bot uses about 6 calls a day.
3. In your copy, open **Settings > Secrets and variables > Actions** and add:
   - `NESTDADDY_KEY`
   - and at least one channel:

   | Channel | Secrets |
   |---|---|
   | Telegram | `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` |
   | Slack | `SLACK_WEBHOOK_URL` |
   | Email (SMTP over SSL) | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO`, optional `SMTP_PORT` (default 465) |

4. Edit [`watchlist.json`](watchlist.json) with your own keywords.
5. Open **Actions > daily-digest > Run workflow** to test it. After that it runs every day at 07:00 UTC (change the `cron` line in `.github/workflows/digest.yml`).

### Getting Telegram details

Create a bot with [@BotFather](https://t.me/BotFather) and copy the token. Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`. For a group or channel, add the bot there first.

### Getting a Slack webhook

Create a Slack app, turn on **Incoming Webhooks**, add one for your channel and copy its URL.

## Configure

`watchlist.json`:

```json
{
  "title": "Your daily news digest",
  "sections": ["global", "tech", "financial"],
  "per_section": 5,
  "watchlist": [
    {"q": "OpenAI"},
    {"q": "electric vehicles", "country": "US"},
    {"q": "interest rates", "country": "GB"}
  ]
}
```

| Field | Meaning |
|---|---|
| `sections` | Any of `global` (world), `tech`, `financial` (markets, crypto, economy). Use `[]` for alerts only. |
| `per_section` | Stories per section and per alert, 1 to 50. |
| `watchlist[].q` | Key words to search in the last 24 hours of news (2 to 200 characters). |
| `watchlist[].country` | Optional two-letter country code (US, GB, DE, FR, JP, AU, SG and more). Leave it out to search every country. |

The config is checked on start, and a clear error is printed if something is wrong.

## Run it locally

```bash
pip install -r requirements.txt
export NESTDADDY_KEY="your_key"
python digest.py --dry-run          # print the digest, send nothing
export TELEGRAM_TOKEN="..." TELEGRAM_CHAT_ID="..."
python digest.py                    # send it
```

Or with cron on any Linux server or Mac:

```bash
0 7 * * * cd /path/to/news-digest-bot && NESTDADDY_KEY=... TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... python3 digest.py >> digest.log 2>&1
```

## How it works

- `digest.py` calls `GET /api/v1/news/{global,tech,financial}` for headlines and `GET /api/v1/news/search?q=...&days=1` for each alert.
- Every URL is normalised (no query string or fragment) and checked against `state/seen.json`. On GitHub Actions that file is kept between runs in the Actions cache.
- Stories are marked as sent **only after delivery worked**, so a failed send is retried on the next run. If one channel fails, the others still get the digest.
- Rate limits and server errors (429, 5xx) are retried with back-off. The remaining API quota is printed after each run.

Exit codes: `0` sent or nothing new, `1` API or delivery error, `2` missing key, channel or bad config.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run offline: the API and the channels are mocked.

## Ideas

- **Summaries:** pass each URL to `GET /api/v1/extract?url=...` for the readable article text, then let any language model write a two-line summary.
- **One digest per team:** keep a config per channel with `DIGEST_CONFIG=path/to/team.json`.
- **Ask instead of schedule:** connect the [NestDaddy MCP server](https://nestdaddy.com/mcp) to Claude, Cursor or n8n and ask about today's news directly.

## Learn more

- Step by step guide: [Build a daily news digest bot in Python](https://nestdaddy.com/blog/news-digest-bot-python)
- API reference: [nestdaddy.com/apis](https://nestdaddy.com/apis) and the [OpenAPI spec](https://nestdaddy.com/openapi.json)
- More examples in Python, JavaScript and curl: [nestdaddy/nestdaddy-examples](https://github.com/nestdaddy/nestdaddy-examples)

## License

MIT
