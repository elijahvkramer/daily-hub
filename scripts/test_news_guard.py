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
        self.assertEqual(len(dropped), 2)                 # the 2025 page, and the story with no source to vet
        self.assertIn("pro day", dropped[0])
        self.assertEqual([i["lead"] for i in ed["sections"][0]["items"]], ["Giants name starter"])

    def test_brief_claimed_date_is_not_trusted_oct12(self):
        """Oct 12, 2026: the brief wrote published=2026-10-10 on a 2023 CBS Sports article."""
        page = b'<meta property="article:published_time" content="2023-10-08T13:00:00Z">'
        ed = {"sections": [{"title": "Sports", "items": [
            {"lead": "No. 12 Oklahoma shocked No. 3 Texas 34-30", "published": "2026-10-10",
             "url": "https://www.cbssports.com/college-football/news/week-6-report-card"}]}]}
        now = datetime.datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
        dropped = g.drop_stale(ed, now, lambda u, t=12, mb=0: (200, "", page, None), {"page": 5})
        self.assertEqual(len(dropped), 1)
        self.assertIn("2023", dropped[0])
        self.assertEqual(ed["sections"][0]["items"], [])

    def test_check_plain_reads_the_page(self):
        page = b'<meta property="article:published_time" content="2023-10-08T13:00:00Z">'
        ed = {"sections": [{"title": "Sports", "items": [{"lead": "Old game", "published": "2026-10-10", "url": "https://x/y"}]}]}
        probs = g.check_plain(ed, "2026-10-12", fetch=lambda u, t=12, mb=0: (200, "", page, None))
        self.assertTrue(any("ARTICLE IS OLD" in p and "2023-10-08" in p for p in probs))

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


class Enforce(unittest.TestCase):
    """Oct 12, 2026: an edition with two stories in total must come out full."""
    def fake_web(self):
        recent = rfc(datetime.datetime.now(UTC) - datetime.timedelta(hours=6))
        def items(prefix, words, n=8):
            import random
            r = random.Random(prefix)
            junk = lambda: " ".join("".join(r.choice("bcdfgklmnprstvz") + r.choice("aeiou") for _ in range(3)) for _ in range(5))
            return [(f"{words[i % len(words)]} {junk()}", f"https://{prefix}/{i}", "", recent, None) for i in range(n)]
        mil = ["Ukraine drone strike", "Pentagon troops deploy", "Israel ceasefire", "Navy warship missile"]
        pol = ["Senate passes bill", "White House policy", "Supreme Court vote", "Governor election"]
        world = ["Japan prime minister resigns", "Earthquake kills dozens in Peru", "Pope meets king", "Floods hit Kenya"]
        feeds = {}
        for url, (outlet, b) in g.FEEDS.items():
            if "world" in b:
                feeds[url] = rss(items(outlet.replace(" ", "") + "W", mil + world))
            elif "us" in b:
                feeds[url] = rss(items(outlet.replace(" ", "") + "w", mil + pol))
            elif "tech" in b:
                feeds[url] = rss(items(outlet.replace(" ", "") + "t", ["AI model", "chip launch"]))
            elif b & {"science", "odd"}:
                feeds[url] = rss(items(outlet.replace(" ", "") + "s", ["comet", "record-breaking pumpkin"]))
        for url, (outlet, sport, team) in g.SPORT_FEEDS.items():
            feeds[url] = rss(items(outlet.replace(" ", "") + "x" + str(sport), ["NFL Week 6 recap", "Kentucky Wildcats basketball practice", "Giants quarterback Jaxson Dart"]))
        paras = ["Officials confirmed on Saturday that the measure passed after a long night of negotiation between both parties in the chamber.",
                 "Analysts said the outcome would reshape spending priorities for the coming fiscal year and could shift several state budgets as well.",
                 "Critics warned that implementation details remain unclear, and several advocacy groups said they would challenge parts of it in court.",
                 "The next step is a review by the committee, which is expected to publish its findings before the end of the month according to aides."]
        page = ("<html><body>" + "".join(f"<p>{x}</p>" for x in paras) + "</body></html>").encode()
        def fetch(u, t=12, mb=0):
            if u.startswith(g.ESPN_API) or "news.google.com" in u:
                return 404, "", b"", None
            if u in feeds:
                return 200, "", feeds[u], None
            return 200, "", page, None
        return fetch

    def test_two_story_edition_is_filled_and_reframed(self):
        today = datetime.datetime.now(UTC).date().isoformat()
        ed = g.skeleton(today)
        ed["subtitle"] = "A quiet weekend on the US political and military front."
        ed["glance"][1]["teaser"] = "Light weekend news cycle"
        ed["sections"][3]["items"] = [{"lead": "Labour held the seat.", "body": "x", "url": "https://es/1", "published": today}]
        ed.pop("auto_edition")
        changed, rep = g.enforce(ed, today, "/nonexistent", "pw", lambda *a: {}, self.fake_web())
        self.assertTrue(changed)
        self.assertEqual(rep["still_thin"], [], rep["section_counts"])
        self.assertNotIn("quiet", ed["subtitle"].lower())
        self.assertNotIn("Light", ed["glance"][1]["teaser"])
        self.assertGreater(rep["details_added"], 0)
        self.assertTrue(any(i.get("detail") for s in ed["sections"] for i in s["items"] if i.get("auto")))

    def test_check_plain_flags_thin_and_quiet(self):
        ed = g.skeleton("2026-10-12")
        ed["glance"][0]["teaser"] = "A quiet weekend"
        probs = g.check_plain(ed, "2026-10-12")
        self.assertEqual(sum("needs at least" in p for p in probs), 6)
        self.assertTrue(any("quiet" in p for p in probs))


class Oct15Audit(unittest.TestCase):
    """Real headlines from the Oct 13-15, 2026 wire-feed editions that Eli flagged."""
    def c(self, title, buckets, desc="", url="https://x.example/2026/10/15/story", **kw):
        return {"title": title, "desc": desc, "url": url, "buckets": set(buckets), **kw}

    def test_junk_never_becomes_a_story(self):
        for t in ["Morning news brief", "MORNING GLORY: The polls got Michigan badly wrong. Now Democrats may face a bigger reckoning",
                  "LIZ PEEK: Two troubled Senate races could spoil Democrats' blue-wave fantasy",
                  "Scammers are stealing checks from the mail. Here’s how to protect yours",
                  "Essential items to stock in your medicine cabinet for cold and flu season"]:
            self.assertIsNone(g.place(self.c(t, {"us"})), t)
        self.assertIsNone(g.place(self.c("The irony of OpenAI crying foul over spying", {"world"},
                                          url="https://www.aljazeera.com/opinions/2026/10/15/the-irony")))

    def test_each_section_gets_the_right_story(self):
        cases = [
            ("Booker ‘wowed’ by GOP challenger’s blunt message to Trump", {"us"}, 1),
            ("Bueckers broadsides to Lady Magic’s spice: the greatest trash talkers in WNBA history", {"us"}, None),
            ("Pakistan school collapse kills almost 30 students and a teacher", {"world"}, 3),
            ("Pope diagnosed with lung cancer but had successful surgery, Vatican says", {"world"}, 3),
            ("Houthis launch new attack on Saudi capital as Trump mulls strikes", {"us"}, 0),
            ("Russian attacks on Ukraine leave 600,000 without power", {"world"}, 0),
            ("Musk says US government may be behind slowdown of Ukraine drone speeds", {"science"}, 0),
            ("Anthropic urges court in Washington, D.C. to rehear appeal of Pentagon blacklisting", {"tech"}, 2),
            ("Nvidia, AMD, Micron fall as AI stock sell-off deepens", {"tech"}, 2),
            ("Key US voters say economy is getting worse ahead of midterms, exclusive poll finds", {"us"}, 1),
            ("Polls are a snapshot, and other helpful things to remember during the midterm elections", {"world"}, None),
            ("Little tern colony has worst breeding season", {"science"}, 4),
        ]
        for t, b, want in cases:
            self.assertEqual(g.place(self.c(t, b)), want, t)

    def test_sports_groups(self):
        S = {"sports"}
        fav = [("Mark Pope pens letter to Kentucky students ahead of basketball season", None, "Kentucky Sports Radio"),
               ("KSR’s top takeaways from Tuesday at SEC Basketball Media Days", None, "Kentucky Sports Radio"),
               ("Bring on the blitz! Changing the identity of the Giants' defense", "nfl", "ESPN"),
               ("NY Giants news: Abdul Carter misses practice (calf)", "nfl", "Big Blue View"),
               ("Midseason report card for USC football receivers after seven games", None, "Trojans Wire")]
        gen = [("Will Stein Show Recap: Kentucky’s offense can get better", None, "Kentucky Sports Radio",
                "Kentucky football coach Will Stein on the offense"),
               ("Bill Belichick noticeably absent from Patriots' celebration of 2001 Super Bowl team", "nfl", "CBS Sports",
                "Belichick recently attended the Giants' Super Bowl reunion"),
               ("Kentucky is a finalist for QB Josiah Boyd, a top 100 recruit in 2028", None, "Kentucky Sports Radio", ""),
               ("Scheffler leads by two after second round at the PGA Tour stop", "golf", "ESPN", "")]
        off = [("PSG’s Achraf Hakimi sends message to Barcelona ahead of highly anticipated clash", None, "Yahoo Sports",
                "The European giants meet on Tuesday"),
               ("Ajša Sivka officially cleared by NCAA to play for Kentucky WBB", None, "Kentucky Sports Radio", ""),
               ("Giants pitcher Logan Webb named NL Pitcher of the Month", None, "ESPN", "San Francisco Giants ace")]
        for t, sp, src in fav:
            self.assertEqual(g.sports_group(self.c(t, S, sport=sp, source=src)), "Favorites", t)
        for t, sp, src, d in gen:
            self.assertEqual(g.sports_group(self.c(t, S, d, sport=sp, source=src)), "General", t)
        for t, sp, src, d in off:
            self.assertIsNone(g.sports_group(self.c(t, S, d, sport=sp, source=src)), t)

    def test_sports_quota_two_and_two_favorites_first(self):
        now = NOW
        w = now - datetime.timedelta(hours=3)
        mk = lambda t, sp, src: {"title": t, "desc": "", "url": "https://s/" + t[:12].replace(" ", ""), "when": w,
                                 "img": None, "source": src, "buckets": {"sports"}, "sport": sp, "team": None, "cats": ""}
        pool = [mk("NFL Week 6 picks: every game", "nfl", "ESPN"),
                mk("Chiefs edge Bills in overtime thriller", "nfl", "ESPN"),
                mk("Lakers sign veteran guard to one-year deal", "nba", "ESPN"),
                mk("Giants activate Malik Nabers from injured reserve", "nfl", "ESPN"),
                mk("Kentucky basketball lands five-star guard commitment", "mcbb", "ESPN"),
                mk("USC football's Lincoln Riley addresses quarterback battle", "cfb", "ESPN")]
        ed = {"sections": [{"title": "Sports", "items": [
            {"lead": "Kentucky basketball opens practice", "body": "Mark Pope", "group": "Favorites", "url": "https://k/1"},
            {"lead": "Giants beat Saints", "body": "NFL", "group": "Favorites", "url": "https://k/2"},
            {"lead": "Giants coordinator speaks", "body": "NFL", "group": "Favorites", "url": "https://k/3"},
            {"lead": "USC football rallies past Penn State", "body": "Trojans", "group": "Favorites", "url": "https://k/4"}]}]}
        g.top_up(ed, now, pool)
        sp = next(s for s in ed["sections"] if s["title"] == "Sports")["items"]
        groups = [i["group"] for i in sp]
        self.assertGreaterEqual(groups.count("General"), 2)     # 4 favorites alone is not enough
        self.assertEqual(groups, sorted(groups, key=lambda x: x != "Favorites"))
        self.assertNotIn("NFL Week 6 picks: every game", [i["lead"] for i in sp])   # betting/picks junk

    def test_framing_uses_the_hero_story_whole(self):
        ed = {"headline": "Morning news brief\nCivil rights advocates plan to send…", "auto_edition": True,
              "glance": [{"label": "US Military & Foreign Conflict", "teaser": "Morning news brief"}],
              "sections": [{"title": "US Military & Foreign Conflict", "items": [
                  {"lead": "Russian attacks on Ukraine leave 600,000 without power", "body": "Kyiv says the grid was hit.", "auto": True}]}]}
        g.refresh_framing(ed, [], force=True)
        self.assertEqual(ed["headline"], "Russian attacks on Ukraine leave 600,000 without power")
        self.assertEqual(ed["subtitle"], "Kyiv says the grid was hit.")
        self.assertNotIn("…", ed["glance"][0]["teaser"])
        self.assertNotIn("Morning", ed["glance"][0]["teaser"])

    def test_edition_window(self):
        self.assertEqual(g.window_days("2026-10-19"), 3)        # Monday
        self.assertEqual(g.window_days("2026-10-15"), 1)
        self.assertEqual(g.edition_cutoff("2026-10-15").isoformat(), "2026-10-14T00:00:00+00:00")
        self.assertEqual(g.edition_cutoff("2026-10-19").isoformat(), "2026-10-16T00:00:00+00:00")

    def test_check_plain_flags_sports_mistakes(self):
        ed = {"headline": "x", "sections": [{"title": "Sports", "items": [
            {"lead": "Will Stein: Kentucky football offense can improve", "group": "Favorites", "url": "https://a/1", "published": "2026-10-15"},
            {"lead": "PSG beats Barcelona in Champions League", "group": "General", "url": "https://a/2", "published": "2026-10-15"}]}]}
        probs = g.check_plain(ed, "2026-10-15")
        self.assertTrue(any("belongs in 'General'" in p for p in probs), probs)
        self.assertTrue(any("not one of Eli's sports" in p for p in probs), probs)
        self.assertTrue(any("'Favorites' stories" in p for p in probs), probs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
