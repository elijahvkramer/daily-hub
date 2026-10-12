#!/usr/bin/env python3
"""Freshness guard + minimum-depth top-up for the published news edition.

Why this exists (Oct 8, 2026): two failures in the same edition.
  1. STALE: a story about Kentucky's pro day (Jalen Low / Otega Oweh) ran under the date
     "Thursday, October 8, 2026" but cited an article from 2025. Nothing in the pipeline ever
     read an article's real publication date -- the edition's dateline is stamped from the
     clock, and the brief's "must be inside the window" rule was an instruction to a model,
     not a check. An annual event (a pro day) is the classic way an old article slips through.
  2. THIN: tabs shipped with one story, or none (Tech for days), because the brief was told to
     drop anything repeated or out of window and to skip a section rather than pad it.

This module is called from enrich_news.py on every Calendar Refresh run (~every 30 min), so
it also repairs an edition after it is published. It is deterministic Python -- no model:

  * drop_stale(): every item's article date is read (item.published, else a date in the URL,
    else the page's own metadata) and anything older than STALE_DAYS is removed. The verdict
    is cached on the item (`pubchk`) so a page is read at most once.
  * top_up(): any section with fewer than MIN_ITEMS stories is filled from publisher RSS
    feeds, newest first, only from items whose feed timestamp is inside FRESH_HOURS, never
    repeating a story/URL from the last three editions or from the same edition.

Both are additive/subtractive on the in-memory edition; the caller re-encrypts and writes it.
"""
import datetime
import email.utils
import html
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

STALE_DAYS = 4        # Monday editions cover 72h; one day of slack for time zones / late posts
FRESH_HOURS = 60      # feed fillers must be newer than this (covers a Monday morning's weekend)
MIN_ITEMS = 4         # every section shows at least this many stories

SECTION_ORDER = [
    "US Military & Foreign Conflict",
    "Politics & Policy",
    "Tech & AI",
    "Major International Headlines",
    "Random",
    "Sports",
]

SECTION_KEYS = ["military", "politic", "tech", "international", "random", "sport"]


def sec_index(title):
    t = (title or "").lower()
    for i, k in enumerate(SECTION_KEYS):
        if k in t:
            return i
    return None


# feed url -> (outlet name, buckets it may fill). Buckets: world, us, tech, science, sports.
FEEDS = {
    "https://feeds.bbci.co.uk/news/world/rss.xml": ("BBC News", {"world"}),
    "https://feeds.bbci.co.uk/news/technology/rss.xml": ("BBC News", {"tech"}),
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml": ("BBC News", {"science"}),
    "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml": ("BBC News", {"us"}),
    "https://www.theguardian.com/world/rss": ("The Guardian", {"world"}),
    "https://www.theguardian.com/us-news/rss": ("The Guardian", {"us"}),
    "https://www.theguardian.com/technology/rss": ("The Guardian", {"tech"}),
    "https://www.theguardian.com/science/rss": ("The Guardian", {"science"}),
    "https://www.aljazeera.com/xml/rss/all.xml": ("Al Jazeera", {"world"}),
    "https://moxie.foxnews.com/google-publisher/latest.xml": ("Fox News", {"us", "world"}),
    "https://www.cbsnews.com/latest/rss/main": ("CBS News", {"us", "world"}),
    "https://www.cbsnews.com/latest/rss/politics": ("CBS News", {"us"}),
    "https://www.cbsnews.com/latest/rss/world": ("CBS News", {"world"}),
    "https://www.cbsnews.com/latest/rss/science": ("CBS News", {"science"}),
    "https://www.cbsnews.com/latest/rss/technology": ("CBS News", {"tech"}),
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910": ("CNBC", {"tech"}),
    "https://feeds.arstechnica.com/arstechnica/index": ("Ars Technica", {"tech", "science"}),
    "https://www.theverge.com/rss/index.xml": ("The Verge", {"tech"}),
    "https://sports.yahoo.com/rss/": ("Yahoo Sports", {"sports"}),
    "https://www.cbssports.com/rss/headlines/": ("CBS Sports", {"sports"}),
    "https://www.cbssports.com/rss/headlines/college-basketball/": ("CBS Sports", {"sports"}),
    "https://www.cbssports.com/rss/headlines/nfl/": ("CBS Sports", {"sports"}),
    "https://www.cbssports.com/rss/headlines/college-football/": ("CBS Sports", {"sports"}),
    "https://www.espn.com/espn/rss/news": ("ESPN", {"sports"}),
    "https://www.espn.com/espn/rss/ncb/news": ("ESPN", {"sports"}),
    "https://www.espn.com/espn/rss/nfl/news": ("ESPN", {"sports"}),
    # Oct 12, 2026: a deeper pool so a thin edition can always be filled -- Random in particular
    # had only four science feeds behind it.
    "https://www.espn.com/espn/rss/ncf/news": ("ESPN", {"sports"}),
    "https://www.espn.com/espn/rss/nba/news": ("ESPN", {"sports"}),
    "https://www.espn.com/espn/rss/golf/news": ("ESPN", {"sports"}),
    "https://kentuckysportsradio.com/feed/": ("Kentucky Sports Radio", {"sports"}),
    "https://feeds.npr.org/1001/rss.xml": ("NPR", {"us", "world"}),
    "https://feeds.npr.org/1014/rss.xml": ("NPR", {"us"}),
    "https://feeds.npr.org/1004/rss.xml": ("NPR", {"world"}),
    "https://feeds.npr.org/1019/rss.xml": ("NPR", {"tech"}),
    "https://feeds.npr.org/1007/rss.xml": ("NPR", {"science"}),
    "https://rss.upi.com/news/odd_news.rss": ("UPI", {"odd"}),
    "https://rss.upi.com/news/science_news.rss": ("UPI", {"science"}),
    "https://www.theguardian.com/environment/rss": ("The Guardian", {"science"}),
    "https://feeds.bbci.co.uk/news/business/rss.xml": ("BBC News", {"world"}),
    "https://techcrunch.com/feed/": ("TechCrunch", {"tech"}),
}

MILITARY_WORDS = re.compile(
    r"\b(military|troops?|pentagon|army|navy|air force|marines?|war|strikes?|missiles?|drones?|"
    r"ceasefire|hamas|hezbollah|houthis?|ukraine|russia|iran|israel|gaza|nato|defen[sc]e|"
    r"warship|airstrike|combat|taiwan|north korea|sanctions?|veterans?|hegseth)\b", re.I)
POLITICS_WORDS = re.compile(
    r"\b(congress|senate|senators?|house|white house|trump|president|election|supreme court|"
    r"bill|governor|democrats?|republicans?|tariffs?|shutdown|policy|federal|justice department|"
    r"campaign|vote|voters|court|attorney general|biden|vance|speaker)\b", re.I)
FAVORITES = re.compile(r"\b(kentucky|wildcats|calipari|mark pope|giants|usc|trojans|lincoln riley)\b", re.I)


# ---------------------------------------------------------------- dates

def parse_dt(s):
    """RFC-822 or ISO-8601 -> aware UTC datetime, or None."""
    if not s:
        return None
    s = str(s).strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime.datetime(int(m[1]), int(m[2]), int(m[3]), 12, tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
    return None


URL_DATE = re.compile(r"/((?:19|20)\d\d)[/-](\d{1,2})[/-](\d{1,2})(?:[/-]|$|\?)")
URL_YM = re.compile(r"/((?:19|20)\d\d)/(\d{1,2})/")
URL_YEAR_SLUG = re.compile(r"[-_/]((?:19|20)\d\d)(?:[-_/.]|$)")


def url_date(url):
    """A date spelled out in the article URL, or None. Year-only slugs (-2025-) count too."""
    if not url:
        return None
    m = URL_DATE.search(url)
    if m:
        try:
            return datetime.datetime(int(m[1]), int(m[2]), int(m[3]), 12, tzinfo=datetime.timezone.utc)
        except ValueError:
            pass
    m = URL_YM.search(url)
    if m:
        try:
            return datetime.datetime(int(m[1]), int(m[2]), 28, 12, tzinfo=datetime.timezone.utc)
        except ValueError:
            pass
    return None


def url_year(url):
    m = URL_YEAR_SLUG.search(urllib.parse.urlparse(url or "").path)
    return int(m[1]) if m else None


_META_DATE = [
    re.compile(r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|og:article:published_time|'
               r'datePublished|pubdate|publishdate|publish[-_]date|parsely-pub-date|date|dc\.date(?:\.issued)?|'
               r'sailthru\.date|article\.published)["\'][^>]*content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name|itemprop)=["\']'
               r'(?:article:published_time|og:article:published_time|datePublished|pubdate|publishdate|'
               r'parsely-pub-date|date|dc\.date(?:\.issued)?)["\']', re.I),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
    re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.I),
]


def page_date(body_text):
    """The article's own publication date from page metadata, or None."""
    for rx in _META_DATE:
        for m in rx.finditer(body_text or ""):
            d = parse_dt(html.unescape(m.group(1)))
            if d:
                return d
    return None


def edition_now(edition_date):
    """Anchor 'now' to the edition's own date when it is in the past (backfills/tests)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    d = parse_dt(edition_date)
    return min(now, d + datetime.timedelta(days=1)) if d else now


def check_item(item, now, fetch, budget):
    """-> (verdict, datetime|None). verdict in 'fresh' | 'stale' | 'unknown'.

    Oct 12, 2026 rewrite. The old order trusted the date the brief WROTE (`published`) before
    anything else -- and that morning the brief stamped "2026-10-10" on a CBS Sports article from
    2023 (Oklahoma 34, Texas 30 was the 2023 Red River game), so the check passed a three-year-old
    story. A date a model types is a claim, not evidence. Now:
      1. a feed filler's timestamp (the publisher's own RSS pubDate) is trusted -- `pubsrc: feed`
      2. otherwise the ARTICLE PAGE ITSELF is read and its published-time metadata decides
      3. if the page can't be read: a full date spelled out in the URL, or an older year in the slug
      4. still nothing -> 'unknown'; drop_stale() treats unverifiable stories as not publishable
    The verdict is cached (`pubchk` + `pubsrc`) so each page is read once; a cached date whose
    source was only the brief's own claim is re-verified."""
    cutoff = now - datetime.timedelta(days=STALE_DAYS)
    d, src = None, None
    if item.get("pubsrc") in ("feed", "page", "url") and parse_dt(item.get("pubchk")):
        d, src = parse_dt(item["pubchk"]), item["pubsrc"]
    elif item.get("auto") and parse_dt(item.get("pubchk")):
        d, src = parse_dt(item["pubchk"]), "feed"
    if d is None and item.get("url") and budget.get("page", 0) > 0:
        budget["page"] -= 1
        try:
            status, _, body, _ = fetch(item["url"], 12, 900_000)
            if status == 200:
                d = page_date(body.decode("utf-8", "ignore") if isinstance(body, bytes) else body)
                src = "page" if d else None
        except Exception:  # noqa: BLE001
            d = None
    if d is None:
        d = url_date(item.get("url"))
        src = "url" if d else None
    if d is not None:
        claimed = parse_dt(item.get("published"))
        if claimed and abs((claimed - d).days) > 2:
            item["pubclaim"] = item.get("published")          # what the brief said, kept for the audit
        item["pubchk"], item["pubsrc"] = d.isoformat(timespec="seconds"), src
        return ("fresh" if d >= cutoff else "stale"), d
    y = url_year(item.get("url"))
    if y is not None and y < now.year:       # no date anywhere, but the URL says an older year
        item["pubchk"], item["pubsrc"] = f"{y}-01-01T00:00:00+00:00", "url"
        return "stale", parse_dt(item["pubchk"])
    return "unknown", None


def drop_stale(edition, now, fetch, budget=None):
    """Remove items whose article is older than the window. Returns list of dropped leads."""
    budget = budget if budget is not None else {"page": 60}
    dropped = []
    for sec in edition.get("sections", []):
        keep = []
        for it in sec.get("items", []):
            verdict, d = check_item(it, now, fetch, budget)
            if verdict == "stale":
                dropped.append(f"{(it.get('lead') or '')[:70]} [{d.date() if d else '?'}] {it.get('url') or ''}")
            elif verdict == "unknown" and (not it.get("url") or it.get("unverified_runs", 0) >= 1):
                # Eli, Oct 12: vet the date before posting ANY story. One retry (a page can time out
                # once); a story whose date still cannot be established comes down.
                dropped.append(f"{(it.get('lead') or '')[:70]} [date unverifiable] {it.get('url') or ''}")
            else:
                if verdict == "unknown":
                    it["unverified_runs"] = it.get("unverified_runs", 0) + 1
                keep.append(it)
        sec["items"] = keep
    return dropped


# ---------------------------------------------------------------- feeds

MEDIA_NS = "{http://search.yahoo.com/mrss/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
STOPWORDS = set("the a an and or but of in on at to for with from by as is are was were be been this that "
                "it its his her their our your new more most over under after before said says will would "
                "could should can may might us".split())


def _words(text):
    return {w.lower() for w in re.findall(r"[A-Za-z]{3,}", text or "")} - STOPWORDS


def similar(a, b):
    ta, tb = _words(a), _words(b)
    return len(ta & tb) / max(3, min(len(ta), len(tb))) if ta and tb else 0.0


def _clean(text, limit=None):
    t = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:limit].rsplit(" ", 1)[0] + "…") if limit and len(t) > limit else t


def parse_feed(xml_bytes, outlet, buckets):
    """RSS 2.0 / Atom -> list of dicts with a verified timestamp. Items without one are skipped."""
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:  # noqa: BLE001
        return out
    nodes = list(root.iter("item")) + list(root.iter(ATOM_NS + "entry"))
    for it in nodes:
        title = _clean(it.findtext("title") or it.findtext(ATOM_NS + "title"))
        link = (it.findtext("link") or "").strip()
        if not link:
            el = it.find(ATOM_NS + "link")
            link = (el.get("href") if el is not None else "") or ""
        when = parse_dt(it.findtext("pubDate") or it.findtext(DC_NS + "date")
                        or it.findtext(ATOM_NS + "published") or it.findtext(ATOM_NS + "updated"))
        if not (title and link and when):
            continue
        desc = _clean(it.findtext("description") or it.findtext(ATOM_NS + "summary")
                      or it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded"), 260)
        img = None
        for tag in (MEDIA_NS + "content", MEDIA_NS + "thumbnail"):
            for el in it.findall(tag):
                u = el.get("url")
                w = el.get("width")
                if u and not re.search(r"logo|favicon|sprite|placeholder|\.svg", u, re.I) \
                        and not (w and w.isdigit() and int(w) < 300):
                    img = u
                    break
            if img:
                break
        if not img:
            for el in it.findall("enclosure"):
                if (el.get("type") or "").startswith("image") and el.get("url"):
                    img = el.get("url")
                    break
        title = re.sub(r"\s+[|\-–—]\s+[^|\-–—]{2,30}$", "", title)    # "headline - Outlet"
        out.append({"title": title, "desc": desc if desc and similar(desc, title) < 0.9 else "",
                    "url": link, "when": when, "img": img, "source": outlet, "buckets": buckets})
    return out


def classify(cand, section_title):
    """Does this fresh feed story belong in this section?"""
    t = section_title.lower()
    text = f"{cand['title']} {cand['desc']}"
    b = cand["buckets"]
    if "military" in t:
        return bool(MILITARY_WORDS.search(text)) and bool(b & {"world", "us"})
    if "politic" in t:
        return "us" in b and bool(POLITICS_WORDS.search(text)) and not MILITARY_WORDS.search(cand["title"])
    if "tech" in t:
        return "tech" in b
    if "international" in t:
        return "world" in b and not re.search(r"\b(trump|congress|senate|white house)\b", cand["title"], re.I)
    if "random" in t:
        return bool(b & {"science", "odd"})
    if "sport" in t:
        return "sports" in b
    return False


def _slug(s):
    return "-".join(re.findall(r"[a-z0-9]+", (s or "").lower())[:6]) or "story"


def prior_stories(repo, decrypt, passphrase, date, n=3):
    """(urls, leads) of the last n editions before `date`, for repeat avoidance."""
    urls, leads = set(), []
    try:
        dates = sorted(json.load(open(os.path.join(repo, "data", "manifest.json"))).get("news", []), reverse=True)
    except Exception:  # noqa: BLE001
        return urls, leads
    for d in [x for x in dates if x < date][:n]:
        try:
            ed = decrypt(json.load(open(os.path.join(repo, "data", "news", f"{d}.json.enc"))), passphrase)
        except Exception:  # noqa: BLE001
            continue
        for sec in ed.get("sections", []):
            for it in sec.get("items", []):
                if it.get("url"):
                    urls.add(it["url"].split("?")[0])
                leads.append(f"{it.get('lead', '')} {it.get('body', '')}")
    return urls, leads


def top_up(edition, now, pool, prior_urls=(), prior_leads=(), min_items=MIN_ITEMS,
           fresh_hours=FRESH_HOURS, prior_sim=0.6):
    """Fill thin sections from `pool` (parse_feed output). Returns {section title: n_added}."""
    cutoff = now - datetime.timedelta(hours=fresh_hours)
    fresh = sorted((c for c in pool if c["when"] >= cutoff), key=lambda c: c["when"], reverse=True)
    secs = edition.setdefault("sections", [])
    present = {sec_index(s.get("title")) for s in secs}
    for i, title in enumerate(SECTION_ORDER):       # a section the brief skipped entirely comes back
        if i not in present:
            secs.append({"title": title, "items": []})
    secs.sort(key=lambda s: sec_index(s.get("title")) if sec_index(s.get("title")) is not None else 99)

    used_urls = {it.get("url", "").split("?")[0] for s in secs for it in s.get("items", []) if it.get("url")}
    used_text = [f"{it.get('lead', '')} {it.get('body', '')}" for s in secs for it in s.get("items", [])]
    added = {}
    for sec in secs:
        items = sec.setdefault("items", [])
        title = sec.get("title") or ""
        is_sports = "sport" in title.lower()
        if is_sports:
            # favorites first: if a favorite-team story is in the pool it goes in before anything else
            fresh_for = sorted(fresh, key=lambda c: (not FAVORITES.search(f"{c['title']} {c['desc']}"), -c["when"].timestamp()))
        else:
            fresh_for = fresh
        for cand in fresh_for:
            if len(items) >= min_items:
                break
            u = cand["url"].split("?")[0]
            if u in used_urls or u in prior_urls or not classify(cand, title):
                continue
            blob = f"{cand['title']} {cand['desc']}"
            if any(similar(blob, t) >= 0.5 for t in used_text) or any(similar(blob, t) >= prior_sim for t in prior_leads):
                continue
            item = {
                "lead": cand["title"],
                "body": cand["desc"] or f"{cand['source']} is reporting this story as it develops.",
                "url": cand["url"],
                "source": cand["source"],
                "published": cand["when"].isoformat(timespec="seconds"),
                "pubchk": cand["when"].isoformat(timespec="seconds"),
                "pubsrc": "feed",
                "storyKey": _slug(cand["title"]),
                "auto": True,                       # wire-feed top-up, not written by the brief
            }
            if cand["img"]:
                item["img"] = cand["img"]
                item["imgCaption"] = cand["source"]
            if is_sports:
                item["group"] = "Favorites" if FAVORITES.search(blob) else "General"
            items.append(item)
            used_urls.add(u)
            used_text.append(blob)
            added[title] = added.get(title, 0) + 1
    if any(s.get("items") for s in secs):
        edition["sections"] = [s for s in secs if s.get("items")] + [s for s in secs if not s.get("items")]
    return added


def build_pool(fetch, feeds=None):
    pool, ok = [], 0
    for url, (outlet, buckets) in (feeds or FEEDS).items():
        try:
            status, _, body, _ = fetch(url, 12, 900_000)
            if status != 200:
                continue
            got = parse_feed(body, outlet, buckets)
            if got:
                ok += 1
                pool.extend(got)
        except Exception:  # noqa: BLE001
            continue
    return pool, ok


def run(edition, date, repo, passphrase, decrypt, fetch, dry=False):
    """Guard + top-up on an in-memory edition. Returns (changed, report dict)."""
    now = edition_now(date)
    before = json.dumps(edition, sort_keys=True)
    dropped = drop_stale(edition, now, fetch)
    thin = [s.get("title") for s in edition.get("sections", []) if len(s.get("items", [])) < MIN_ITEMS]
    present = {sec_index(s.get("title")) for s in edition.get("sections", []) if s.get("items")}
    missing = [t for i, t in enumerate(SECTION_ORDER) if i not in present]
    added, pool_n, feeds_ok = {}, 0, 0
    if thin or missing:
        pool, feeds_ok = build_pool(fetch)
        pool_n = len(pool)
        urls, leads = prior_stories(repo, decrypt, passphrase, date)
        added = top_up(edition, now, pool, urls, leads)
    changed = json.dumps(edition, sort_keys=True) != before
    counts = {s.get("title"): len(s.get("items", [])) for s in edition.get("sections", [])}
    return changed, {"dropped_stale": dropped, "added": added, "section_counts": counts,
                     "feeds_ok": feeds_ok, "pool": pool_n, "thin_before": thin, "missing_before": missing}


# ---------------------------------------------------------------- enforcement (Oct 12, 2026)
#
# Why: the Oct 12 edition shipped with TWO stories in total. The brief (a model) decided it was
# "a quiet weekend", left four sections empty, and the workflow called that a success because its
# only check was "is today's date in the manifest". The repair above (run) existed, but it only
# ran inside Calendar Refresh -- and GitHub's scheduler then skipped Calendar Refresh for nine
# straight hours, so nothing repaired the edition all day.
#
# enforce() is the single, deterministic floor every path now goes through: the brief workflow
# runs it right after the model publishes (and creates an edition from scratch if the model
# published nothing), and every refresh run repeats it. It escalates instead of giving up:
#   1. drop stale stories, top up thin sections from fresh feeds (FRESH_HOURS)
#   2. still thin -> widen to FRESH_HOURS_WIDE and loosen the repeat filter (never repeat a URL)
#   3. give every feed filler a real 2-3 paragraph `detail` from its own article page
#   4. rewrite glance chips / subtitle that still claim a "quiet" day the edition no longer has

FRESH_HOURS_WIDE = 96
QUIET_WORDS = re.compile(r"\b(quiet|light|slow|no major|nothing (new|major)|no dated|thin)\b", re.I)
SKIP_P = re.compile(r"(cookie|subscribe|sign up|newsletter|all rights reserved|©|copyright|advertis|"
                    r"javascript|your browser|click here|read more|follow us|download the app|getty images|"
                    r"photo:|image:|credit:|\bad\b feedback)", re.I)
_P_TAG = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)


def article_detail(url, fetch, min_words=70, max_words=230):
    """2-3 paragraphs of the article's own text, or None. Plain extraction, no model."""
    try:
        status, _, body, _ = fetch(url, 12, 900_000)
    except Exception:  # noqa: BLE001
        return None
    if status != 200 or not body:
        return None
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
    text = re.sub(r"<(script|style|noscript|figure|figcaption|aside|nav|footer|header)\b.*?</\1>", " ", text, flags=re.I | re.S)
    paras, words = [], 0
    for m in _P_TAG.finditer(text):
        t = _clean(m.group(1))
        if len(t) < 70 or SKIP_P.search(t) or t.count(" ") < 10:
            continue
        if any(similar(t, p) > 0.8 for p in paras):
            continue
        w = t.split()
        if words + len(w) > max_words and paras:
            break
        paras.append(" ".join(w[:max_words]))
        words += len(w)
        if len(paras) >= 4 or (len(paras) >= 3 and words >= 150):
            break
    if words < min_words:
        return None
    if len(paras) == 4:                      # four short grafs read better as three
        paras = paras[:2] + [paras[2] + " " + paras[3]]
    return "\n\n".join(paras)


def _short(lead, n=6):
    w = re.sub(r"[.!?]+$", "", (lead or "").strip()).split()
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def refresh_framing(edition, thin_before):
    """Glance chips and subtitle written when a section was empty must not survive its repair."""
    changed = False
    secs = {sec_index(s.get("title")): s for s in edition.get("sections", [])}
    for g in edition.get("glance", []) or []:
        sec = secs.get(sec_index(g.get("label")))
        if not sec or not sec.get("items"):
            continue
        if QUIET_WORDS.search(g.get("teaser", "")) or sec.get("title") in thin_before and any(i.get("auto") for i in sec["items"]):
            first = next((i for i in sec["items"] if not i.get("auto")), sec["items"][0])
            nt = _short(first.get("lead"))
            if nt and nt != g.get("teaser"):
                g["teaser"] = nt
                changed = True
    if QUIET_WORDS.search(edition.get("subtitle", "") or ""):
        tops = [s["items"][0].get("lead", "") for s in edition.get("sections", []) if s.get("items")][:3]
        edition["subtitle"] = " ".join(re.sub(r"\s+", " ", t).strip().rstrip(".") + "." for t in tops if t)
        changed = True
    return changed


def section_counts(edition):
    counts = {t: 0 for t in SECTION_ORDER}
    for s in edition.get("sections", []):
        i = sec_index(s.get("title"))
        if i is not None:
            counts[SECTION_ORDER[i]] += len(s.get("items", []))
    return counts


def thin_sections(edition, min_items=MIN_ITEMS):
    return [t for t, n in section_counts(edition).items() if n < min_items]


def skeleton(date):
    d = parse_dt(date) or datetime.datetime.now(datetime.timezone.utc)
    dl = d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")
    return {"date": date, "dateline": dl, "eyebrow": ("Weekend Roundup" if d.weekday() == 0 else "Weekday Roundup"),
            "headline": "Today's briefing", "subtitle": "", "auto_edition": True,
            "glance": [{"label": t, "teaser": ""} for t in SECTION_ORDER],
            "sections": [{"title": t, "items": []} for t in SECTION_ORDER], "sources": ""}


def enforce(edition, date, repo, passphrase, decrypt, fetch, detail_budget=14):
    """The floor. Mutates `edition`; returns (changed, report). Never raises on network trouble."""
    before = json.dumps(edition, sort_keys=True)
    thin_before = thin_sections(edition)
    now = edition_now(date)
    _, rep = run(edition, date, repo, passphrase, decrypt, fetch)
    widened = {}
    if thin_sections(edition):
        pool, ok = build_pool(fetch)
        rep["feeds_ok"], rep["pool"] = max(rep.get("feeds_ok", 0), ok), max(rep.get("pool", 0), len(pool))
        urls, leads = prior_stories(repo, decrypt, passphrase, date)
        widened = top_up(edition, now, pool, urls, leads, fresh_hours=FRESH_HOURS_WIDE, prior_sim=0.85)
    # feed fillers get their own article's text so the article page is not a stub
    details = 0
    for s in edition.get("sections", []):
        for it in s.get("items", []):
            if detail_budget <= 0:
                break
            if it.get("detail") or not it.get("url") or it.get("dchk"):
                continue
            detail_budget -= 1
            it["dchk"] = 1                          # tried once; never re-fetch every run
            d = article_detail(it["url"], fetch)
            if d:
                it["detail"] = d
                details += 1
    refresh_framing(edition, thin_before)
    if edition.get("auto_edition"):
        lead = next((s["items"][0] for s in edition.get("sections", []) if s.get("items")), None)
        if lead and edition.get("headline") == "Today's briefing":
            edition["headline"] = _short(lead.get("lead"), 12)
        edition["sources"] = ", ".join(sorted({i.get("source") for s in edition["sections"] for i in s.get("items", []) if i.get("source")}))
    rep.update({"widened": widened, "details_added": details, "still_thin": thin_sections(edition),
                "section_counts": section_counts(edition), "thin_before": thin_before})
    return json.dumps(edition, sort_keys=True) != before, rep


def check_plain(edition, date, min_items=MIN_ITEMS, fetch=None):
    """For the brief to run on /tmp/news.json BEFORE publishing: every problem, as text.
    With `fetch`, every article page is opened and its OWN publication date is compared with the
    window -- the brief's `published` field is never taken on faith (Oct 12: a 2023 article)."""
    probs = []
    if fetch is not None:
        now = edition_now(date)
        budget = {"page": 80}
        for s in edition.get("sections", []):
            for it in s.get("items", []):
                probe = {k: it.get(k) for k in ("url", "published")}
                verdict, d = check_item(probe, now, fetch, budget)
                lead = (it.get("lead") or "")[:70]
                if verdict == "stale":
                    probs.append(f"ARTICLE IS OLD -- the page itself says {d.date()} (you wrote {it.get('published')}): {lead}  <{it.get('url')}>  Replace this story.")
                elif verdict == "unknown":
                    probs.append(f"could not confirm a publication date on the article page: {lead}  <{it.get('url')}>  Use an article whose page shows its date.")
    for t, n in section_counts(edition).items():
        if n < min_items:
            probs.append(f"section '{t}' has {n} stories -- needs at least {min_items}. Search again with different outlets.")
    now = edition_now(date)
    cutoff = now - datetime.timedelta(days=STALE_DAYS)
    for s in edition.get("sections", []):
        for it in s.get("items", []):
            d = parse_dt(it.get("published"))
            if not d:
                probs.append(f"no `published` date on: {(it.get('lead') or '')[:70]}")
            elif d < cutoff:
                probs.append(f"too old ({d.date()}): {(it.get('lead') or '')[:70]}")
            if not it.get("url"):
                probs.append(f"no `url` on: {(it.get('lead') or '')[:70]}")
    for g in edition.get("glance", []) or []:
        if QUIET_WORDS.search(g.get("teaser", "")):
            probs.append(f"glance chip '{g.get('teaser')}' calls a section quiet -- every section must carry real stories")
    return probs
