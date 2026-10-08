#!/usr/bin/env python3
"""Offline tests for news_guard.py:  python3 scripts/test_news_guard.py

Covers the Oct 8, 2026 failures: a 2025 Kentucky pro-day article published under a 2026
dateline (stale), and sections with 0-1 stories (thin)."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_guard as g  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 8, 14, 0, tzinfo=UTC)


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><description>{d}</description>"
        f"<pubDate>{w}</pubDate>"
        + (f'<media:content xmlns:media="http://search.yahoo.com/mrss/" url="{i}" width="800"/>' if i else "")
        + "</item>" for t, u, d, w, i in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


def rfc(dt):
    import email.utils
    return email.utils.format_datetime(dt)


class Dates(unittest.TestCase):
    def test_url_date(self):
        self.assertEqual(g.url_date("https://x.com/2025/10/08/kentucky-pro-day").year, 2025)
        self.assertEqual(g.url_date("https://x.com/news/2026-10-07-story").month, 10)
        self.assertIsNone(g.url_date("https://x.com/story-about-things"))

    def test_page_date_meta_and_jsonld(self):
        a = '<meta property="article:published_time" content="2025-10-09T12:00:00Z">'
        b = '<script type="application/ld+json">{"datePublished":"2025-10-09T12:00:00-04:00"}</script>'
        self.assertEqual(g.page_date(a).year, 2025)
        self.assertEqual(g.page_date(b).year, 2025)

    def test_stale_by_url_year_only(self):
        it = {"url": "https://x.com/kentucky-pro-day-2025"}
        v, _ = g.check_item(it, NOW, lambda *a: (404, "", b"", None), {"page": 0})
        self.assertEqual(v, "stale")


class Guard(unittest.TestCase):
    def test_kentucky_pro_day_2025_is_dropped_via_page_metadata(self):
        stale_page = b'<meta property="article:published_time" content="2025-10-09T15:00:00Z">'
        fresh_page = b'<meta property="article:published_time" content="2026-10-08T09:00:00Z">'
        pages = {"https://ksr.example/pro-day": stale_page, "https://ksr.example/fresh": fresh_page}
        ed = {"sections": [{"title": "Sports", "items": [
            {"lead": "Jalen Low and Otega Oweh shine at Kentucky's pro day", "url": "https://ksr.example/pro-day"},
            {"lead": "Giants name starter", "url": "https://ksr.example/fresh"},
            {"lead": "No link at all"},
        ]}]}
        dropped = g.drop_stale(ed, NOW, lambda u, t=12, mb=0: (200, "", pages[u], None), {"page": 5})
        self.assertEqual(len(dropped), 1)
        self.assertIn("pro day", dropped[0])
        self.assertEqual([i["lead"] for i in ed["sections"][0]["items"]], ["Giants name starter", "No link at all"])

    def test_page_checked_once(self):
        calls = []
        it = {"url": "https://x.com/a"}
        f = lambda u, t=12, mb=0: (calls.append(u) or (200, "", b'<meta name="date" content="2026-10-08">', None))
        g.check_item(it, NOW, f, {"page": 5})
        g.check_item(it, NOW, f, {"page": 5})
        self.assertEqual(len(calls), 1)      # second run uses the cached pubchk


class TopUp(unittest.TestCase):
    def pool(self):
        recent = rfc(NOW - datetime.timedelta(hours=5))
        old = rfc(NOW - datetime.timedelta(days=400))
        feeds = {
            "BBC": (rss([
                ("Ukraine drone strike hits Russian refinery", "https://b/1", "Military strike overnight", recent, "https://i/1.jpg"),
                ("Ceasefire talks resume in Gaza as troops pull back", "https://b/2", "talks", recent, None),
                ("Old war story from last year", "https://b/old", "x", old, None),
                ("Japan elects new prime minister", "https://b/3", "politics abroad", recent, None),
            ]), {"world"}),
            "Verge": (rss([("New AI chip unveiled", "https://v/1", "chip", recent, None),
                           ("Apple ships software update", "https://v/2", "iOS", recent, None),
                           ("Startup raises money for robots", "https://v/3", "robots", recent, None),
                           ("Google rolls out Gemini feature", "https://v/4", "ai", recent, None)]), {"tech"}),
            "ESPN": (rss([("Kentucky Wildcats open practice", "https://e/1", "basketball", recent, None),
                          ("NFL Week 6 injury report", "https://e/2", "nfl", recent, None),
                          ("UFC card set", "https://e/3", "ufc", recent, None),
                          ("PGA leader after round two", "https://e/4", "golf", recent, None),
                          ("Giants QB practices", "https://e/5", "giants", recent, None)]), {"sports"}),
        }
        pool = []
        for name, (xml, b) in feeds.items():
            pool += g.parse_feed(xml, name, b)
        return pool

    def test_thin_sections_filled_old_items_ignored_favorites_first(self):
        ed = {"sections": [{"title": "Tech & AI", "items": [{"lead": "Only one", "body": "x", "url": "https://t/0"}]},
                           {"title": "Sports", "items": []}]}
        added = g.top_up(ed, NOW, self.pool())
        by = {s["title"]: s["items"] for s in ed["sections"]}
        self.assertGreaterEqual(len(by["Tech & AI"]), 4)
        self.assertEqual(len(by["Sports"]), 4)
        self.assertTrue(by["Sports"][0]["group"] == "Favorites")          # Kentucky/Giants lead
        self.assertEqual(len(by["Sports"]) and sum(1 for i in by["Sports"] if i["group"] == "Favorites"), 2)
        self.assertNotIn("https://b/old", [i["url"] for s in ed["sections"] for i in s["items"]])
        self.assertTrue(all(i.get("published") for s in ed["sections"] for i in s["items"] if i.get("auto")))
        self.assertIn("Tech & AI", added)

    def test_missing_section_is_recreated_in_order(self):
        ed = {"sections": [{"title": "Sports", "items": [{"lead": "x", "body": "y", "url": "https://s/0"}]}]}
        g.top_up(ed, NOW, self.pool())
        titles = [s["title"] for s in ed["sections"] if s["items"]]
        self.assertEqual(titles[0], "US Military & Foreign Conflict")
        self.assertEqual(titles[-1], "Sports")

    def test_no_repeat_of_prior_edition_urls_or_same_edition(self):
        ed = {"sections": [{"title": "Tech & AI", "items": [
            {"lead": "New AI chip unveiled", "body": "chip", "url": "https://other/chip"}]}]}
        g.top_up(ed, NOW, self.pool(), prior_urls={"https://v/2"})
        tech = next(s for s in ed["sections"] if s["title"] == "Tech & AI")["items"]
        self.assertNotIn("https://v/2", [i["url"] for i in tech])      # published in a prior edition
        self.assertEqual(sum("chip" in i["lead"].lower() for i in tech), 1)

    def test_idempotent(self):
        ed = {"sections": []}
        g.top_up(ed, NOW, self.pool())
        before = [len(s["items"]) for s in ed["sections"]]
        g.top_up(ed, NOW, self.pool())
        self.assertEqual(before, [len(s["items"]) for s in ed["sections"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
