"""Daily news digest bot.

Collects world, tech and markets headlines plus keyword alerts from the
NestDaddy News API, drops stories it already sent, and delivers one digest to
Telegram, Slack and/or email.

Configuration lives in watchlist.json; secrets come from environment variables.
Docs: https://nestdaddy.com/blog/news-digest-bot-python
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

API = os.environ.get("NESTDADDY_API_URL", "https://nestdaddy.com/api/v1")
HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("DIGEST_CONFIG", HERE / "watchlist.json"))
SEEN_PATH = Path(os.environ.get("DIGEST_SEEN_FILE", HERE / "state" / "seen.json"))
SEEN_DAYS = 14          # forget sent URLs after two weeks
TELEGRAM_CHUNK = 4000   # Telegram's hard limit is 4096 characters
TIMEOUT = 30

SECTION_TITLES = {"global": "World", "tech": "Tech", "financial": "Markets"}


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- config

def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")

    sections = cfg.get("sections", ["global", "tech", "financial"])
    bad = [s for s in sections if s not in SECTION_TITLES]
    if bad:
        raise ConfigError(f"Unknown section(s) {bad}; use {sorted(SECTION_TITLES)}")

    per_section = int(cfg.get("per_section", 5))
    if not 1 <= per_section <= 50:
        raise ConfigError("per_section must be between 1 and 50")

    watchlist = []
    for w in cfg.get("watchlist", []):
        q = str(w.get("q", "")).strip()
        if not 2 <= len(q) <= 200:
            raise ConfigError(f"Watchlist query must be 2-200 characters: {w!r}")
        country = w.get("country")
        if country and not (len(country) == 2 and country.isalpha()):
            raise ConfigError(f"Country must be a two-letter code: {w!r}")
        watchlist.append({"q": q, "country": country.upper() if country else None})

    return {"sections": sections, "per_section": per_section, "watchlist": watchlist,
            "title": cfg.get("title", "Your daily news digest")}


# ---------------------------------------------------------------- API

class NestDaddy:
    def __init__(self, key: str, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers.update({"X-API-Key": key, "User-Agent": "news-digest-bot/1.0"})
        self.quota = None

    def get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(3):
            r = self.s.get(f"{API}{path}", params=params, timeout=TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
            data = r.json()
            self.quota = data.get("quota", self.quota)
            return data
        return {}

    def headlines(self, section: str, limit: int) -> list[dict]:
        return self.get(f"/news/{section}", limit=limit).get("articles", [])

    def search(self, q: str, country: str | None, limit: int) -> list[dict]:
        return self.get("/news/search", q=q, country=country, days=1, limit=limit).get("results", [])


# ---------------------------------------------------------------- dedupe

def clean_url(url: str) -> str:
    """Drop query strings and fragments so tracking tags do not create duplicates."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))


class SeenStore:
    def __init__(self, path: Path = SEEN_PATH):
        self.path = path
        try:
            self.data: dict[str, float] = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        cutoff = time.time() - SEEN_DAYS * 86400
        self.data = {u: t for u, t in self.data.items() if t >= cutoff}
        self.pending: dict[str, float] = {}

    def only_new(self, articles: list[dict]) -> list[dict]:
        fresh = []
        for a in articles:
            url = a.get("url")
            if not url or not a.get("title"):
                continue
            key = clean_url(url)
            if key in self.data or key in self.pending:
                continue
            self.pending[key] = time.time()
            fresh.append(a)
        return fresh

    def commit(self) -> None:
        """Save only after delivery worked, so a failed send is retried next run."""
        self.data.update(self.pending)
        self.pending = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------- digest

def build_digest(api: NestDaddy, cfg: dict, seen: SeenStore) -> list[tuple[str, list[dict]]]:
    sections = []
    for name in cfg["sections"]:
        items = seen.only_new(api.headlines(name, cfg["per_section"]))
        if items:
            sections.append((SECTION_TITLES[name], items))
    for w in cfg["watchlist"]:
        items = seen.only_new(api.search(w["q"], w["country"], cfg["per_section"]))
        if items:
            label = w["q"] + (f" ({w['country']})" if w["country"] else "")
            sections.append((f"Alert: {label}", items))
    return sections


def as_text(title: str, sections: list[tuple[str, list[dict]]]) -> str:
    lines = [title]
    for name, items in sections:
        lines.append(f"\n{name.upper()}")
        for a in items:
            source = a.get("source_domain") or ""
            lines.append(f"- {a['title'].strip()}" + (f" ({source})" if source else ""))
            lines.append(f"  {a['url']}")
    lines.append("\nNews by NestDaddy: https://nestdaddy.com")
    return "\n".join(lines)


# ---------------------------------------------------------------- delivery

def chunks(text: str, size: int = TELEGRAM_CHUNK) -> list[str]:
    """Split on line breaks so links are never cut in half."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


def send_telegram(text: str) -> None:
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    for part in chunks(text):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": part, "disable_web_page_preview": True},
                          timeout=TIMEOUT)
        r.raise_for_status()


def send_slack(text: str) -> None:
    r = requests.post(os.environ["SLACK_WEBHOOK_URL"], json={"text": text}, timeout=TIMEOUT)
    r.raise_for_status()


def send_email(text: str, subject: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = os.environ["SMTP_TO"]
    msg.set_content(text)
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, timeout=TIMEOUT) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def channels() -> list[str]:
    found = []
    if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        found.append("telegram")
    if os.environ.get("SLACK_WEBHOOK_URL"):
        found.append("slack")
    if os.environ.get("SMTP_HOST"):
        found.append("email")
    return found


# ---------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    key = os.environ.get("NESTDADDY_KEY")
    if not key:
        print("NESTDADDY_KEY is not set. Get a free key (1,000 calls a month) at https://nestdaddy.com/mcp",
              file=sys.stderr)
        return 2
    targets = channels()
    if not targets and not dry_run:
        print("No delivery channel set. Add TELEGRAM_TOKEN + TELEGRAM_CHAT_ID, SLACK_WEBHOOK_URL "
              "or SMTP_* variables, or run with --dry-run.", file=sys.stderr)
        return 2

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    api = NestDaddy(key)
    seen = SeenStore()
    try:
        sections = build_digest(api, cfg, seen)
    except requests.HTTPError as e:
        print(f"NestDaddy API error {e.response.status_code}: {e.response.text[:200]}", file=sys.stderr)
        return 1
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 1

    if not sections:
        print("Nothing new since the last digest.")
        return 0

    text = as_text(cfg["title"], sections)
    count = sum(len(items) for _, items in sections)
    if dry_run:
        print(text)
        print(f"\n[dry run] {count} stories, nothing sent, nothing marked as seen")
        return 0

    senders = {"telegram": lambda: send_telegram(text), "slack": lambda: send_slack(text),
               "email": lambda: send_email(text, cfg["title"])}
    failed = []
    for name in targets:
        try:
            senders[name]()
        except Exception as e:  # one broken channel must not block the others
            failed.append(name)
            print(f"Sending to {name} failed: {e}", file=sys.stderr)

    if len(failed) == len(targets):
        return 1
    seen.commit()
    print(f"Sent {count} stories to {', '.join(t for t in targets if t not in failed)}")
    if api.quota and api.quota.get("remaining") is not None:
        print(f"API quota: {api.quota['remaining']} calls left this period")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
