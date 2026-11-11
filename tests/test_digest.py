"""Offline tests: the NestDaddy API and delivery channels are mocked."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import digest  # noqa: E402


def art(n, url=None, source="example.com"):
    return {"title": f"Story {n}", "url": url or f"https://example.com/news/{n}",
            "source_domain": source}


class FakeAPI:
    def __init__(self, headlines=None, search=None):
        self._h = headlines or {}
        self._s = search or {}
        self.quota = {"used": 6, "limit": 5000, "remaining": 4994}

    def headlines(self, section, limit):
        return self._h.get(section, [])[:limit]

    def search(self, q, country, limit):
        return self._s.get(q, [])[:limit]


class ConfigTests(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return Path(f.name)

    def test_default_file_is_valid(self):
        cfg = digest.load_config(Path(digest.HERE / "watchlist.json"))
        self.assertEqual(cfg["sections"], ["global", "tech", "financial"])
        self.assertTrue(cfg["watchlist"])

    def test_rejects_bad_section(self):
        with self.assertRaises(digest.ConfigError):
            digest.load_config(self.write({"sections": ["sports"]}))

    def test_rejects_bad_country(self):
        with self.assertRaises(digest.ConfigError):
            digest.load_config(self.write({"watchlist": [{"q": "ai", "country": "USA"}]}))

    def test_rejects_short_query(self):
        with self.assertRaises(digest.ConfigError):
            digest.load_config(self.write({"watchlist": [{"q": "a"}]}))

    def test_country_upper_cased(self):
        cfg = digest.load_config(self.write({"watchlist": [{"q": "rates", "country": "gb"}]}))
        self.assertEqual(cfg["watchlist"][0]["country"], "GB")


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state" / "seen.json"

    def test_clean_url_strips_tracking(self):
        self.assertEqual(digest.clean_url("https://Example.com/a/?utm_source=x#top"),
                         "https://example.com/a")

    def test_duplicates_within_one_run(self):
        seen = digest.SeenStore(self.path)
        out = seen.only_new([art(1), art(2, url="https://example.com/news/1?ref=feed")])
        self.assertEqual(len(out), 1)

    def test_not_saved_until_commit(self):
        seen = digest.SeenStore(self.path)
        seen.only_new([art(1)])
        self.assertFalse(self.path.exists())
        self.assertEqual(len(digest.SeenStore(self.path).only_new([art(1)])), 1)

    def test_saved_after_commit(self):
        seen = digest.SeenStore(self.path)
        seen.only_new([art(1)])
        seen.commit()
        self.assertEqual(digest.SeenStore(self.path).only_new([art(1)]), [])

    def test_old_entries_expire(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"https://example.com/news/1": 0}), encoding="utf-8")
        self.assertEqual(len(digest.SeenStore(self.path).only_new([art(1)])), 1)

    def test_skips_items_without_url_or_title(self):
        seen = digest.SeenStore(self.path)
        self.assertEqual(seen.only_new([{"title": "x"}, {"url": "https://a.com/b"}]), [])


class DigestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.seen = digest.SeenStore(Path(tmp.name) / "seen.json")
        self.cfg = {"sections": ["global", "tech"], "per_section": 5, "title": "T",
                    "watchlist": [{"q": "OpenAI", "country": None},
                                  {"q": "rates", "country": "GB"}]}

    def test_sections_and_alerts_without_repeats(self):
        api = FakeAPI(headlines={"global": [art(1), art(2)], "tech": [art(2), art(3)]},
                      search={"OpenAI": [art(3), art(4)], "rates": [art(5)]})
        sections = digest.build_digest(api, self.cfg, self.seen)
        names = [n for n, _ in sections]
        self.assertEqual(names, ["World", "Tech", "Alert: OpenAI", "Alert: rates (GB)"])
        urls = [a["url"] for _, items in sections for a in items]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(len(urls), 5)

    def test_empty_sections_are_dropped(self):
        api = FakeAPI(headlines={"global": [art(1)]})
        sections = digest.build_digest(api, self.cfg, self.seen)
        self.assertEqual([n for n, _ in sections], ["World"])

    def test_text_format(self):
        text = digest.as_text("Daily", [("World", [art(1)])])
        self.assertIn("WORLD", text)
        self.assertIn("- Story 1 (example.com)", text)
        self.assertIn("https://example.com/news/1", text)


class ChunkTests(unittest.TestCase):
    def test_short_text_one_chunk(self):
        self.assertEqual(digest.chunks("a\nb"), ["a\nb"])

    def test_long_text_split_on_lines(self):
        line = "x" * 100 + "\n"
        parts = digest.chunks(line * 100, size=1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("".join(parts), line * 100)
        self.assertTrue(all(p.endswith("\n") for p in parts))


class MainTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.seen_path = Path(tmp.name) / "seen.json"
        patcher = mock.patch.object(digest, "SEEN_PATH", self.seen_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # SeenStore's default argument was bound at import time, so patch the class default too
        orig_init = digest.SeenStore.__init__
        path = self.seen_path
        p2 = mock.patch.object(digest.SeenStore, "__init__",
                               lambda s, p=None: orig_init(s, path))
        p2.start()
        self.addCleanup(p2.stop)
        api = FakeAPI(headlines={"global": [art(1)], "tech": [art(2)], "financial": [art(3)]})
        p3 = mock.patch.object(digest, "NestDaddy", lambda key: api)
        p3.start()
        self.addCleanup(p3.stop)

    def env(self, **extra):
        base = {"NESTDADDY_KEY": "test"}
        base.update(extra)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(digest.main([]), 2)

    def test_no_channel(self):
        with self.env():
            self.assertEqual(digest.main([]), 2)

    def test_dry_run_sends_nothing_and_saves_nothing(self):
        with self.env(), mock.patch("sys.stdout"):
            self.assertEqual(digest.main(["--dry-run"]), 0)
        self.assertFalse(self.seen_path.exists())

    def test_slack_success_marks_seen(self):
        with self.env(SLACK_WEBHOOK_URL="https://hooks.example/x"), \
                mock.patch.object(digest, "send_slack") as send, mock.patch("sys.stdout"):
            self.assertEqual(digest.main([]), 0)
        send.assert_called_once()
        self.assertEqual(len(json.loads(self.seen_path.read_text())), 3)

    def test_failed_send_does_not_mark_seen(self):
        with self.env(SLACK_WEBHOOK_URL="https://hooks.example/x"), \
                mock.patch.object(digest, "send_slack", side_effect=RuntimeError("down")), \
                mock.patch("sys.stderr"):
            self.assertEqual(digest.main([]), 1)
        self.assertFalse(self.seen_path.exists())

    def test_one_channel_fails_other_succeeds(self):
        with self.env(SLACK_WEBHOOK_URL="https://hooks.example/x", TELEGRAM_TOKEN="t",
                      TELEGRAM_CHAT_ID="1"), \
                mock.patch.object(digest, "send_slack"), \
                mock.patch.object(digest, "send_telegram", side_effect=RuntimeError("down")), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(digest.main([]), 1)
        self.assertTrue(self.seen_path.exists())


class ApiClientTests(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"articles": [art(1)], "quota": {"remaining": 9}}
        busy = mock.Mock(status_code=429)
        session = mock.Mock(headers={})
        session.get.side_effect = [busy, ok]
        with mock.patch.object(digest.time, "sleep"):
            api = digest.NestDaddy("k", session=session)
            self.assertEqual(len(api.headlines("global", 5)), 1)
        self.assertEqual(api.quota["remaining"], 9)
        self.assertEqual(session.get.call_count, 2)

    def test_drops_none_params(self):
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"results": []}
        session = mock.Mock(headers={})
        session.get.return_value = ok
        digest.NestDaddy("k", session=session).search("ai", None, 5)
        params = session.get.call_args.kwargs["params"]
        self.assertNotIn("country", params)
        self.assertEqual(params["days"], 1)


if __name__ == "__main__":
    unittest.main()
