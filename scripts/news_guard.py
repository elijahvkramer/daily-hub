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

    Order: a date the brief recorded, then a date in the URL, then the page itself. A cached
    verdict (`pubchk`) from an earlier run is reused so each page is fetched at most once."""
    cutoff = now - datetime.timedelta(days=STALE_DAYS)
    d = parse_dt(item.get("published")) or parse_dt(item.get("pubchk"))
    if d is None:
        d = url_date(item.get("url"))
    if d is None and item.get("url") and budget.get("page", 0) > 0:
        budget["page"] -= 1
        try:
            status, _, body, _ = fetch(item["url"], 12, 700_000)
            if status == 200:
                d = page_date(body.decode("utf-8", "ignore") if isinstance(body, bytes) else body)
        except Exception:  # noqa: BLE001
            d = None
    if d is not None:
        item["pubchk"] = d.isoformat(timespec="seconds")
        return ("fresh" if d >= cutoff else "stale"), d
    y = url_year(item.get("url"))
    if y is not None and y < now.year:       # no date anywhere, but the URL says an older year
        item["pubchk"] = f"{y}-01-01T00:00:00+00:00"
        return "stale", parse_dt(item["pubchk"])
    return "unknown", None


def drop_stale(edition, now, fetch, budget=None):
    """Remove items whose article is older than the window. Returns list of dropped leads."""
    budget = budget if budget is not None else {"page": 40}
    dropped = []
    for sec in edition.get("sections", []):
        keep = []
        for it in sec.get("items", []):
            verdict, d = check_item(it, now, fetch, budget)
            if verdict == "stale":
                dropped.append(f"{(it.get('lead') or '')[:70]} [{d.date() if d else '?'}] {it.get('url') or ''}")
            else:
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
        return "science" in b
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


def top_up(edition, now, pool, prior_urls=(), prior_leads=(), min_items=MIN_ITEMS):
    """Fill thin sections from `pool` (parse_feed output). Returns {section title: n_added}."""
    cutoff = now - datetime.timedelta(hours=FRESH_HOURS)
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
            if any(similar(blob, t) >= 0.5 for t in used_text) or any(similar(blob, t) >= 0.6 for t in prior_leads):
                continue
            item = {
                "lead": cand["title"],
                "body": cand["desc"] or f"{cand['source']} is reporting this story as it develops.",
                "url": cand["url"],
                "source": cand["source"],
                "published": cand["when"].isoformat(timespec="seconds"),
                "pubchk": cand["when"].isoformat(timespec="seconds"),
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
