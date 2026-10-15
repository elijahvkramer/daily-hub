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


# feed url -> (outlet name, buckets it may fill). Buckets: world, us, tech, science, odd.
#
# Oct 15, 2026 audit: Fox / CBS main / NPR top-stories used to be tagged {"us","world"}, so any
# US domestic story they carried (a check-fraud explainer, a midterm poll, a medicine-cabinet
# list) was eligible for Major International Headlines. A feed now gets "world" only when it is
# genuinely a world desk. BBC Business was dropped (it fed company news into International).
FEEDS = {
    "https://feeds.bbci.co.uk/news/world/rss.xml": ("BBC News", {"world"}),
    "https://feeds.bbci.co.uk/news/technology/rss.xml": ("BBC News", {"tech"}),
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml": ("BBC News", {"science"}),
    "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml": ("BBC News", {"us"}),
    "https://www.theguardian.com/world/rss": ("The Guardian", {"world"}),
    "https://www.theguardian.com/us-news/rss": ("The Guardian", {"us"}),
    "https://www.theguardian.com/technology/rss": ("The Guardian", {"tech"}),
    "https://www.theguardian.com/science/rss": ("The Guardian", {"science"}),
    "https://www.theguardian.com/environment/rss": ("The Guardian", {"science"}),
    "https://www.aljazeera.com/xml/rss/all.xml": ("Al Jazeera", {"world"}),
    "https://moxie.foxnews.com/google-publisher/latest.xml": ("Fox News", {"us"}),
    "https://www.cbsnews.com/latest/rss/main": ("CBS News", {"us"}),
    "https://www.cbsnews.com/latest/rss/politics": ("CBS News", {"us"}),
    "https://www.cbsnews.com/latest/rss/world": ("CBS News", {"world"}),
    "https://www.cbsnews.com/latest/rss/science": ("CBS News", {"science"}),
    "https://www.cbsnews.com/latest/rss/technology": ("CBS News", {"tech"}),
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910": ("CNBC", {"tech"}),
    "https://feeds.arstechnica.com/arstechnica/index": ("Ars Technica", {"tech"}),
    "https://www.theverge.com/rss/index.xml": ("The Verge", {"tech"}),
    "https://techcrunch.com/feed/": ("TechCrunch", {"tech"}),
    "https://feeds.npr.org/1001/rss.xml": ("NPR", {"us"}),
    "https://feeds.npr.org/1014/rss.xml": ("NPR", {"us"}),
    "https://feeds.npr.org/1004/rss.xml": ("NPR", {"world"}),
    "https://feeds.npr.org/1019/rss.xml": ("NPR", {"tech"}),
    "https://feeds.npr.org/1007/rss.xml": ("NPR", {"science"}),
    "https://rss.upi.com/news/odd_news.rss": ("UPI", {"odd"}),
    "https://rss.upi.com/news/science_news.rss": ("UPI", {"science"}),
}

# Sports come only from single-sport feeds now. The multi-sport ones (Yahoo Sports, CBS Sports
# top headlines, ESPN top news) are where PSG soccer, WNBA and MLB stories came from.
# url -> (outlet, sport it covers or None to detect from the text, team it may cover or None)
SPORT_FEEDS = {
    "https://www.cbssports.com/rss/headlines/nfl/": ("CBS Sports", "nfl", None),
    "https://www.cbssports.com/rss/headlines/college-football/": ("CBS Sports", "cfb", None),
    "https://www.cbssports.com/rss/headlines/college-basketball/": ("CBS Sports", "mcbb", None),
    "https://www.espn.com/espn/rss/nfl/news": ("ESPN", "nfl", None),
    "https://www.espn.com/espn/rss/ncf/news": ("ESPN", "cfb", None),
    "https://www.espn.com/espn/rss/ncb/news": ("ESPN", "mcbb", None),
    "https://www.espn.com/espn/rss/nba/news": ("ESPN", "nba", None),
    "https://www.espn.com/espn/rss/golf/news": ("ESPN", "golf", None),
    # favorite-team beat feeds (verified live Oct 15, 2026). KSR / A Sea of Blue cover every
    # Kentucky sport, so their items only count as Favorites with a men's-basketball signal.
    "https://kentuckysportsradio.com/feed/": ("Kentucky Sports Radio", None, "uk"),
    "https://www.aseaofblue.com/rss/index.xml": ("A Sea of Blue", None, "uk"),
    "https://www.bigblueview.com/rss/index.xml": ("Big Blue View", "nfl", "nyg"),
    "https://www.latimes.com/sports/usc/rss2.0.xml": ("Los Angeles Times", None, "usc"),
}
ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/"
ESPN_LEAGUES = {      # ESPN's own news API: exact sport, real photos, publish timestamps
    "football/nfl/news": "nfl", "football/college-football/news": "cfb",
    "basketball/mens-college-basketball/news": "mcbb", "basketball/nba/news": "nba",
    "golf/pga/news": "golf", "mma/ufc/news": "ufc", "tennis/atp/news": "tennis",
}
ESPN_TEAMS = {        # team endpoints; league-wide stories tag every team, so those are filtered
    "football/nfl/news?team=19": ("nfl", "nyg"),
    "football/college-football/news?team=30": ("cfb", "usc"),
    "basketball/mens-college-basketball/news?team=96": ("mcbb", "uk"),
}
GNEWS_TEAMS = {       # last resort for Favorites: Google News search, last 2 days
    "uk": ('"Kentucky" Wildcats men\'s basketball', "mcbb"),
    "nyg": ('"New York Giants"', "nfl"),
    "usc": ('"USC" Trojans football', "cfb"),
}
TEAM_NAMES = {"uk": "Kentucky basketball", "nyg": "Giants", "usc": "USC football"}

_T = lambda p: re.compile(p, re.I)
MILITARY_WORDS = _T(
    r"\b(military|troops?|soldiers?|pentagon|hegseth|army|navy|naval|air force|marines?|missiles?|drones?|"
    r"air ?strikes?|ceasefire|cease-fire|hamas|hezbollah|houthis?|ukrain\w*|russian? (attack|strike|forces|invasion)|"
    r"gaza|israel\w* (army|forces|strike|military)|iran\w* (strike|attack|nuclear|missile)|nato|defen[sc]e secretary|"
    r"warships?|combat|invasion|shelling|artillery|nuclear (weapons?|test)|sanctions?|veterans?|"
    r"north korea\w*|taiwan strait|(?<!trade )(?<!price )(?<!culture )(?<!bidding )\bwar\b|weapons?|insurgents?|militants?|"
    r"rebels?|coup|hostages?|frigate|submarine|fighter jets?)\b")
POLITICS_WORDS = _T(
    r"\b(congress\w*|senate|senators?|white house|trump|biden|vance|harris|election\w*|midterms?|ballots?|"
    r"voters?|polls?|polling|supreme court|governors?|gop|democrats?|democratic|republicans?|tariffs?|shutdown|"
    r"lawmakers|legislat\w+|impeach\w*|attorney general|justice department|doj|fbi|campaign\w*|"
    r"house (speaker|republicans|democrats|vote|passes|committee)|speaker johnson|cabinet|executive order|"
    r"newsom|rubio|desantis|filibuster|primary elections?|primaries|administration|federal (judge|court|agency|workers)|"
    r"medicaid|medicare|social security|immigration|ice agents?|deport\w*|white house)\b")
TECH_WORDS = _T(
    r"(\bAI\b|artificial intelligence|openai|anthropic|chatgpt|claude|gemini|nvidia|\bchips?\b|semiconductor|"
    r"\bapple\b|google|microsoft|\bmeta\b|amazon|tesla|spacex|software|\bapps?\b|iphone|android|cyber\w*|"
    r"\bhack\w*|robot\w*|start-?ups?|data cent(er|re)s?|smartphones?|quantum|tsmc|asml|crypto\w*|bitcoin|"
    r"\btech\b|technology|internet|social media|tiktok|youtube|x\.com|musk|zuckerberg|altman|deepfakes?|"
    r"algorithm|computing|silicon|satellites?|\bEVs?\b|electric vehicles?|streaming)")
COMBAT = _T(r"\b(troops?|soldiers?|missiles?|air ?strikes?|war|killed|ceasefire|invasion|shelling|warships?|"
             r"combat|attacks?|bombing|hostages?)\b")
US_DOMESTIC = _T(r"\b(midterms?|congress|senate|governor|state legislature|u\.?s\.? voters|american voters|"
                 r"white house|supreme court|democrats?|republicans?|gop)\b")
HARD_NEWS = _T(r"\b(killed|kills|dead|deaths?|dies|president|prime minister|minister|government|parliament|"
               r"election|protests?|earthquake|floods?|storm|hurricane|typhoon|wildfire|summit|talks|court|"
               r"crisis|attack|collapse\w*|economy|pope|king|queen|arrested|charged|rescue|evacuat\w+|"
               r"record|first|discover\w*|scientists?|study|researchers?|launch\w*|unveil\w*|ban\w*)\b")

# Not news stories at all: newsletters, columnist banners, live blogs, service journalism,
# shopping and betting. (Oct 15, 2026: "Morning news brief" -- NPR's newsletter -- ran as the
# lead military story and as the edition headline; "MORNING GLORY:" / "LIZ PEEK:" columns ran.)
JUNK_TITLE = _T(
    r"^(the\s+)?(morning|evening|weekend|daily|midday|nightly)\s+(news\s+)?(brief|briefing|edition|digest|"
    r"roundup|report|rundown|wrap)\b|"
    r"^(watch|video|listen|opinion|analysis|review|letters?|explainer|quiz)\b\s*[:|\-–—]|"
    r"\b(podcast|newsletter|as it happened|crossword|wordle|connections hint|"
    r"horoscopes?|tickets?|best prices|promo codes?|deals? (of|on)|coupons?|odds,? lines|betting|best bets?|"
    r"picks? against the spread|week \d+ picks|expert picks|picks and predictions|props?\b|fantasy football|recipes?|photos? of the (day|week)|in pictures|"
    r"week in pictures|what to watch|how to watch|tv channel|live stream)\b|"
    r"\b(how to|here'?s how|what to know|things to know|what you need to know|essential items|should you)\b")
# "LIZ PEEK: ...", "MORNING GLORY: ..." -- case-SENSITIVE on purpose: compiled with re.I this also
# matched ordinary "NY Giants news: ..." and "Will Stein Show Recap: ..." headlines.
CAPS_BANNER = re.compile(r"^[A-Z][A-Z'’.&-]+(\s+[A-Z][A-Z'’.&-]+)+:\s")
JUNK_URL = _T(r"/(opinion|opinions|commentisfree|podcasts?|videos?|gallery|galleries|"
              r"pictures|newsletters?|sponsored|shopping|deals|coupons|betting|tickets|picks|fantasy)(/|-|$)")
OFF_SPORT = _T(r"\b(soccer|premier league|la liga|serie a|bundesliga|champions league|mls|fifa|uefa|psg|"
               r"barcelona|real madrid|man(chester)? (united|city)|arsenal|liverpool|chelsea|wnba|women'?s|wbb|"
               r"lady|mlb|baseball|world series|alcs|nlcs|al wild card|nl wild card|nhl|hockey|stanley cup|"
               r"f1|formula 1|formula one|nascar|indycar|volleyball|softball|lacrosse|cricket|rugby|wrestling|"
               r"boxing|lpga|liv golf|wta|olympic\w*)\b")
PREP = _T(r"\b(high school|prep (star|school|football|basketball)|recruiting class rankings?|aau)\b")
SPORT_TEXT = {
    "nfl": _T(r"\b(nfl|quarterbacks?|qb|touchdowns?|super bowl|wide receivers?|running backs?|linebackers?|"
              r"patriots|cowboys|eagles|chiefs|49ers|packers|steelers|ravens|bills|jets|dolphins|bengals|browns|"
              r"broncos|raiders|chargers|seahawks|rams|cardinals|falcons|panthers|saints|buccaneers|bears|lions|"
              r"vikings|commanders|texans|colts|jaguars|titans)\b"),
    "cfb": _T(r"\b(college football|cfp|playoff committee|heisman|bowl game|sec football|big ten football|"
              r"ap top 25|coaches poll)\b"),
    "mcbb": _T(r"\b(college basketball|men'?s basketball|march madness|ncaa tournament|final four|"
               r"big blue madness|hoops|rupp arena|sec media days)\b"),
    "nba": _T(r"\b(nba|lakers|celtics|knicks|warriors|bucks|nuggets|thunder|76ers|sixers|heat|cavaliers|"
              r"mavericks|suns|clippers|timberwolves|grizzlies|pelicans|kings|spurs|rockets|hawks|hornets|"
              r"pistons|pacers|magic|nets|raptors|bulls|wizards|jazz|blazers)\b"),
    "golf": _T(r"\b(pga|pga tour|masters|ryder cup|u\.?s\.? open golf|scheffler|mcilroy|birdie|bogey)\b"),
    "ufc": _T(r"\b(ufc|octagon|mma)\b"),
    "tennis": _T(r"\b(atp|wimbledon|us open tennis|australian open|french open|roland garros|djokovic|"
                 r"alcaraz|sinner)\b"),
}
BASKETBALL = _T(r"\b(basketball|hoops|mark pope|pope|rupp|big blue madness|guard|forward|center|point guard|"
                r"wing|sec media days|nba draft|ncaa tournament|exhibition)\b")
FOOTBALL = _T(r"\b(football|quarterback|qb|touchdown|will stein|mark stoops|gridiron|kroger field|"
              r"wide receiver|running back|linebacker|defensive coordinator|offensive coordinator|"
              r"bowl|depth chart|kickoff)\b")
TEAM_RX = {
    "uk": _T(r"\b(kentucky|wildcats|big blue nation|bbn|mark pope)\b"),
    "nyg": _T(r"\b((new york|ny|n\.y\.)\s+giants|giants|brian daboll|daboll|jaxson dart|metlife)\b"),
    "usc": _T(r"\b(usc|trojans|lincoln riley|coliseum)\b"),
}
NOT_TEAM = {
    "uk": _T(r"\b(arizona wildcats|villanova|kansas state|k-state|northwestern|new hampshire|weber state|"
             r"bethune|wbb|women'?s|lady)\b"),
    "nyg": _T(r"\b(san francisco giants|sf giants|giants (pitcher|outfielder|infielder|manager)|baseball|mlb|"
              r"oracle park|yomiuri|kentucky|wildcats)\b"),
    "usc": _T(r"\b(gamecocks|south carolina|usc upstate|usc aiken|usc beaufort|women'?s|basketball)\b"),
}
# kept for older callers/tests: any of the three favorite teams mentioned by name
FAVORITES = _T(r"\b(kentucky|wildcats|mark pope|giants|usc|trojans|lincoln riley)\b")


def sport_of(text, hint=None):
    """Which of Eli's sports a story is about, or None (an off-list sport, or unknown)."""
    if OFF_SPORT.search(text or ""):
        return None
    if hint:
        return hint
    for sp, rx in SPORT_TEXT.items():
        if rx.search(text or ""):
            return sp
    if FOOTBALL.search(text or "") or re.search(r"\b(availability report|injury report|bowl eligib\w+)\b", text or "", re.I):
        return "cfb"                 # college beat feeds (KSR, A Sea of Blue, LA Times USC)
    if BASKETBALL.search(text or ""):
        return "mcbb"
    return None


def fav_team(text, sport=None, lenient=False, title=None, hint=None):
    """'uk' | 'nyg' | 'usc' when the story is about one of Eli's three teams, else None.

    Favorites are exactly: Kentucky MEN'S BASKETBALL, the New York Giants (NFL), USC FOOTBALL.
    Kentucky football, Kentucky women's basketball, the SF Giants, USC basketball and every other
    "Wildcats" are not. For feed stories (strict) the team must be named in the HEADLINE -- on
    Oct 13 a Belichick/Patriots story became a "Giants" favorite because its summary mentioned a
    Giants Super Bowl -- unless the story came from that team's own beat feed (`hint`). The brief's
    own items (`lenient`) may name the team anywhere and need no sport word, as long as nothing
    points to a different sport. Sport is read from the headline first, then the summary."""
    t = text or ""
    head = title if title is not None else t
    if OFF_SPORT.search(t) and not lenient:
        return None
    if re.search(r"\b(women'?s|wbb|lady|volleyball|softball|baseball|soccer)\b", t, re.I):
        return None

    def named(team):
        if NOT_TEAM[team].search(t):
            return False
        if hint == team:                    # the team's own beat feed: every story is about the team
            return True
        return bool(TEAM_RX[team].search(t if (lenient or title is None) else head))

    def says(rx):
        return "head" if rx.search(head) else ("body" if rx.search(t) else None)

    if named("uk"):
        bb, fb = says(BASKETBALL), says(FOOTBALL)
        if sport == "mcbb" or bb == "head" or (bb and fb != "head" and not (fb and not bb)) \
                or (bb == "body" and fb == "body" and hint == "uk") \
                or (lenient and not fb and sport in (None, "mcbb")):
            if not (fb == "head" and bb != "head"):
                return "uk"
    if named("nyg"):
        if sport == "nfl" or hint == "nyg" or re.search(r"\b(new york|ny)\s+giants\b", t, re.I) \
                or SPORT_TEXT["nfl"].search(t) or (lenient and sport in (None, "nfl")):
            return "nyg"
    if named("usc"):
        if sport == "cfb" or FOOTBALL.search(t) or (lenient and sport in (None, "cfb")):
            return "usc"
    return None


TEAM_OF_SOURCE = {"Kentucky Sports Radio": "uk", "A Sea of Blue": "uk", "Big Blue View": "nyg"}


def is_junk(title, url=""):
    t = (title or "").strip()
    return bool(JUNK_TITLE.search(t) or CAPS_BANNER.search(t) or JUNK_URL.search(urllib.parse.urlparse(url or "").path)
                or len(re.findall(r"[A-Za-z0-9]+", t)) < 3)


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


def window_days(edition_date):
    """Eli's edition window: Monday covers Fri-Sun (3 days), every other day the past day."""
    d = parse_dt(edition_date)
    return 3 if d and d.weekday() == 0 else 1


def edition_cutoff(edition_date, now=None):
    """Oldest publication time an edition may carry: midnight UTC at the start of its window.
    (Oct 15, 2026: this was a flat 4 days for the brief and 60-96 HOURS for feed fillers, which let
    two-day-old stories into weekday editions -- Eli's rule is the past 24h, Monday the past 72h.)"""
    d = parse_dt(edition_date)
    if not d:
        return (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=STALE_DAYS)
    start = datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
    return start - datetime.timedelta(days=window_days(edition_date))


def check_item(item, now, fetch, budget, cutoff=None):
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
    cutoff = cutoff or (now - datetime.timedelta(days=STALE_DAYS))
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


def drop_stale(edition, now, fetch, budget=None, cutoff=None):
    """Remove items whose article is older than the window. Returns list of dropped leads."""
    budget = budget if budget is not None else {"page": 60}
    dropped = []
    for sec in edition.get("sections", []):
        keep = []
        for it in sec.get("items", []):
            verdict, d = check_item(it, now, fetch, budget, cutoff)
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


def parse_feed(xml_bytes, outlet, buckets, sport=None, team=None):
    """RSS 2.0 / Atom -> list of candidate dicts with a verified timestamp (undated items skipped).
    `sport` / `team` come from SPORT_FEEDS; a feed's own <category> tags are kept in `cats`
    because KSR and A Sea of Blue only say which Kentucky sport a story is about there."""
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
        cats = " ".join(filter(None, [(c.text or c.get("term") or "") for c in
                                      it.findall("category") + it.findall(ATOM_NS + "category")]))
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
        if not img:     # Atom feeds (SB Nation) inline the lead photo in the summary/content HTML
            raw = html.unescape(it.findtext(ATOM_NS + "content") or it.findtext(ATOM_NS + "summary") or "")
            m = re.search(r'<img[^>]+src="([^"]+)"', raw)
            if m and not re.search(r"logo|favicon|sprite|placeholder|\.svg|1x1|pixel", m.group(1), re.I):
                img = m.group(1)
        title = re.sub(r"\s+[|\-–—]\s+[^|\-–—]{2,30}$", "", title)    # "headline - Outlet"
        out.append({"title": title, "desc": desc if desc and similar(desc, title) < 0.9 else "",
                    "cats": cats, "url": link, "when": when, "img": img, "source": outlet,
                    "buckets": set(buckets), "sport": sport, "team": team})
    return out


def parse_espn(body, sport, team=None):
    """ESPN's news API -> candidates. Team endpoints tag league-wide stories (power rankings,
    uniforms, betting lines) with all 32 teams; those are dropped unless the team is named."""
    out = []
    try:
        j = json.loads(body)
    except Exception:  # noqa: BLE001
        return out
    for a in j.get("articles", []) or []:
        if (a.get("type") or "") not in ("Story", "HeadlineNews", "Recap", "Preview", "Blog"):
            continue
        title = _clean(a.get("headline"))
        url = ((a.get("links") or {}).get("web") or {}).get("href") or ""
        when = parse_dt(a.get("published") or a.get("lastModified"))
        if not (title and url and when):
            continue
        desc = _clean(a.get("description"), 260)
        teams = [c.get("description") or "" for c in a.get("categories") or [] if c.get("type") == "team"]
        if team and (len(teams) > 3 or not TEAM_RX[team].search(f"{title} {desc}")):
            team = None                        # a league-wide piece, or the opponent's story
        imgs = [i for i in a.get("images") or [] if i.get("url") and (i.get("width") or 999) >= 300]
        out.append({"title": title, "desc": desc if similar(desc, title) < 0.9 else "", "cats": " ".join(teams[:3]),
                    "url": url, "when": when, "img": imgs[0]["url"] if imgs else None, "source": "ESPN",
                    "buckets": {"sports"}, "sport": sport, "team": team})
    return out


def parse_gnews(body, sport, team):
    """Google News search RSS (favorites fallback). Links are Google redirects that open the
    publisher in a browser; the outlet is the title's trailing ' - Outlet'."""
    out = []
    for c in parse_feed(body, "Google News", {"sports"}, sport, team):
        m = re.search(r"\s+-\s+([^-]{2,40})$", c["title"])
        if m:
            c["source"], c["title"] = m.group(1).strip(), c["title"][:m.start()].strip()
        c["gnews"] = True
        out.append(c)
    return out


def _txt(c):
    return f"{c.get('title', '')} {c.get('desc', '')} {c.get('cats', '')}"


def place(cand):
    """The ONE section a feed story belongs in, or None. Decided on the HEADLINE first -- the old
    classifier matched keywords anywhere in the summary, and checked sections in a fixed order
    (Oct 15, 2026: a Booker/GOP-primary story landed in Military; a WNBA trash-talk feature in
    Politics; a medicine-cabinet checklist in Major International Headlines)."""
    title, url, b = cand.get("title", ""), cand.get("url", ""), cand.get("buckets") or set()
    if is_junk(title, url):
        return None
    if "sports" in b:
        return 5 if sports_group(cand) else None
    mil = {m.group(0).lower() for m in MILITARY_WORDS.finditer(title)}
    combat = COMBAT.search(title)
    if mil and not re.search(r"\b(nfl|nba|game|season|coach)\b", title, re.I) \
            and not ("tech" in b and not combat and TECH_WORDS.search(title)):
        return 0
    if "us" in b and POLITICS_WORDS.search(title):
        return 1
    if "tech" in b or (b & {"us", "world", "science"} and len(TECH_WORDS.findall(title)) >= 2):
        return 2
    if "world" in b and not US_DOMESTIC.search(title):
        return 3
    if b & {"science", "odd"} and not POLITICS_WORDS.search(title) and not TECH_WORDS.search(title):
        return 4
    return None


def classify(cand, section_title):
    """Does this fresh feed story belong in this section? (kept for callers/tests)"""
    i = sec_index(section_title)
    return i is not None and place(cand) == i


def sports_group(cand):
    """'Favorites' (Kentucky men's basketball, NY Giants, USC football), 'General' (NFL, men's
    college basketball, college football, PGA, UFC, men's Grand Slam tennis, NBA), or None."""
    text = _txt(cand)
    sport = cand.get("sport") or sport_of(text)
    hint = cand.get("team") or TEAM_OF_SOURCE.get(cand.get("source") or "")
    if fav_team(text, sport, title=cand.get("title", ""), hint=hint):
        return "Favorites"
    if OFF_SPORT.search(text) or PREP.search(text):
        return None
    sport = sport_of(text, cand.get("sport"))
    return "General" if sport else None


def item_group(item):
    """Group for an item already in the edition. The brief's own items are judged leniently
    (no sport word needed), feed fillers strictly. None = not one of Eli's sports at all."""
    text = f"{item.get('lead', '')} {item.get('body', '')}"
    if item.get("auto"):
        return sports_group({"title": item.get("lead", ""), "desc": item.get("body", ""),
                             "cats": item.get("cats", ""), "sport": item.get("sport"),
                             "team": item.get("team"), "source": item.get("source")})
    if fav_team(text, item.get("sport"), lenient=True):
        return "Favorites"
    if OFF_SPORT.search(item.get("lead", "")):      # the brief's story: judged on its headline
        return None
    return "General"


def rank(cand, si):
    """Higher is better: a real hard-news headline with its own photo, then recency."""
    title = cand.get("title", "")
    sc = 0.0
    sc += 1.5 * len(HARD_NEWS.findall(title))
    if si == 0:
        sc += len(MILITARY_WORDS.findall(title))
    elif si == 1:
        sc += len(POLITICS_WORDS.findall(title))
    elif si == 2:
        sc += len(TECH_WORDS.findall(title))
    sc += 1.0 if cand.get("img") else 0.0
    sc -= 2.0 if cand.get("gnews") else 0.0           # no photo, no article text: last resort
    if re.search(r"\?$", title):
        sc -= 1.0                                     # question headlines are usually features
    return sc


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


def same_story(title, text, thresh=0.5):
    """Two headlines about one event (different outlets, different wording)."""
    head = (text or "")[:len(title or "") + 40]           # the headline part of "lead body"
    return similar(title, head) >= thresh


def team_of(item):
    """Which favorite team an edition item is about (None for General / non-sports)."""
    if item.get("group") != "Favorites":
        return None
    return item.get("team") or fav_team(f"{item.get('lead', '')} {item.get('body', '')}", item.get("sport"), lenient=True)


def order_sports(sec):
    """Favorites first, then General -- stable, so the brief's own order survives inside a group."""
    items = sec.get("items", [])
    sec["items"] = [i for i in items if i.get("group") == "Favorites"] + \
                   [i for i in items if i.get("group") != "Favorites"]


def normalize(edition):
    """Repair what is already in the edition (every run, so a bad edition heals itself):
      * feed fillers that are junk or in the wrong section are moved or removed
      * every Sports item gets the right group; off-list sports come out; Favorites lead
    The brief's own non-sports items are never moved. Returns a list of change notes."""
    notes = []
    secs = edition.setdefault("sections", [])
    by_i = {}
    for s in secs:
        i = sec_index(s.get("title"))
        if i is not None:
            by_i.setdefault(i, s)
    for s in list(secs):
        si = sec_index(s.get("title"))
        keep = []
        for it in s.get("items", []):
            if si == 5:
                g = item_group(it)
                if g is None:
                    notes.append(f"removed off-list sport: {it.get('lead', '')[:60]}")
                    continue
                if it.get("group") != g:
                    notes.append(f"regrouped {it.get('group')}->{g}: {it.get('lead', '')[:60]}")
                    it["group"] = g
                keep.append(it)
                continue
            if not it.get("auto"):
                keep.append(it)
                continue
            if it.get("buckets") is None:
                dest = _legacy_place(it, si)
            else:
                dest = place({"title": it.get("lead", ""), "desc": it.get("body", ""), "url": it.get("url", ""),
                              "buckets": set(it.get("buckets") or [])})
            if dest is None:
                notes.append(f"removed filler: {it.get('lead', '')[:60]}")
            elif dest != si and dest in by_i:
                by_i[dest].setdefault("items", []).append(it)
                notes.append(f"moved to {SECTION_ORDER[dest]}: {it.get('lead', '')[:60]}")
            else:
                keep.append(it)
        s["items"] = keep
    seen = []
    for s in secs:                       # one event, one story: a filler that repeats one goes
        keep = []
        for it in s.get("items", []):
            if it.get("auto") and any(same_story(it.get("lead", ""), x) for x in seen):
                notes.append(f"removed duplicate: {it.get('lead', '')[:60]}")
                continue
            seen.append(it.get("lead", ""))
            keep.append(it)
        s["items"] = keep
    for s in secs:
        if sec_index(s.get("title")) == 5:
            order_sports(s)
    return notes


def _legacy_place(it, si):
    """Fillers published before Oct 15 never recorded which feed they came from, so their section
    can't be re-derived. Conservative repair: drop non-stories, move only the unmistakable."""
    title = it.get("lead", "")
    if is_junk(title, it.get("url")):
        return None
    tech = bool(TECH_WORDS.search(title))
    if MILITARY_WORDS.search(title) and not (si == 2 and tech and not COMBAT.search(title)):
        return 0
    if POLITICS_WORDS.search(title) and si in (0, 3, 4) and not MILITARY_WORDS.search(title):
        return 1
    if si == 1 and not POLITICS_WORDS.search(title):
        return 2 if len(TECH_WORDS.findall(title)) >= 1 else None
    return si


def top_up(edition, now, pool, prior_urls=(), prior_leads=(), min_items=MIN_ITEMS,
           fresh_hours=FRESH_HOURS, prior_sim=0.6, cutoff=None, min_fav=2, min_gen=2):
    """Fill thin sections from `pool`. Returns {section title: n_added}.
    Each candidate can only go to the one section place() picks; within a section the best-ranked
    candidates go first. Sports must end with >= min_fav Favorites AND >= min_gen General."""
    cutoff = cutoff or (now - datetime.timedelta(hours=fresh_hours))
    fresh = [c for c in pool if cutoff <= c["when"] <= now + datetime.timedelta(hours=2)]
    secs = edition.setdefault("sections", [])
    present = {sec_index(s.get("title")) for s in secs}
    for i, title in enumerate(SECTION_ORDER):       # a section the brief skipped entirely comes back
        if i not in present:
            secs.append({"title": title, "items": []})
    secs.sort(key=lambda s: sec_index(s.get("title")) if sec_index(s.get("title")) is not None else 99)

    used_urls = {it.get("url", "").split("?")[0] for s in secs for it in s.get("items", []) if it.get("url")}
    used_text = [f"{it.get('lead', '')} {it.get('body', '')}" for s in secs for it in s.get("items", [])]
    by_sec = {}
    for c in fresh:
        si = place(c)
        if si is not None:
            by_sec.setdefault(si, []).append(c)
    added = {}

    def ok(c):
        u = c["url"].split("?")[0]
        if u in used_urls or u in prior_urls:
            return False
        blob = f"{c['title']} {c['desc']}"
        # Oct 15, 2026: "Pakistan school collapse kills almost 30..." (CBS) and "Pakistan school roof
        # collapse kills at least 34..." (Guardian) both ran -- the long summaries diluted the
        # overlap. Headlines are compared to headlines as well.
        return not (any(similar(blob, t) >= 0.5 or same_story(c["title"], t) for t in used_text)
                    or any(similar(blob, t) >= prior_sim or same_story(c["title"], t, 0.6) for t in prior_leads))

    def add(sec, c, group=None):
        item = {
            "lead": c["title"],
            "body": c["desc"] or f"{c['source']} is reporting this story as it develops.",
            "url": c["url"], "source": c["source"],
            "published": c["when"].isoformat(timespec="seconds"),
            "pubchk": c["when"].isoformat(timespec="seconds"), "pubsrc": "feed",
            "storyKey": _slug(c["title"]), "auto": True,            # wire-feed top-up, not the brief
            "buckets": sorted(c.get("buckets") or []),
        }
        if c.get("img"):
            item["img"] = c["img"]
            item["imgCaption"] = c["source"]
        if c.get("cats"):
            item["cats"] = c["cats"][:200]
        if c.get("sport"):
            item["sport"] = c["sport"]
        if c.get("team"):
            item["team"] = c["team"]
        if group:
            item["group"] = group
        if c.get("gnews"):
            item["dchk"] = 1                      # a Google redirect has no article text to pull
        sec.setdefault("items", []).append(item)
        used_urls.add(c["url"].split("?")[0])
        used_text.append(f"{c['title']} {c['desc']}")
        added[sec.get("title")] = added.get(sec.get("title"), 0) + 1

    for sec in secs:
        si = sec_index(sec.get("title"))
        cands = sorted(by_sec.get(si, []), key=lambda c: (-rank(c, si), -c["when"].timestamp()))
        if si != 5:
            for c in cands:
                if len(sec.get("items", [])) >= min_items:
                    break
                if ok(c):
                    add(sec, c)
            continue
        # Sports: Favorites quota, General quota, then the floor, Favorites preferred.
        favs = [c for c in cands if sports_group(c) == "Favorites"]
        gens = [c for c in cands if sports_group(c) == "General"]
        n = lambda g: sum(1 for i in sec.get("items", []) if i.get("group") == g)
        for pool_g, g, need in ((favs, "Favorites", min_fav), (gens, "General", min_gen)):
            for c in pool_g:
                if n(g) >= need:
                    break
                if ok(c):
                    add(sec, c, g)
        for c in favs + gens:
            if len(sec.get("items", [])) >= min_items:
                break
            if ok(c):
                add(sec, c, sports_group(c))
        # every one of Eli's three teams that has fresh news gets a story (Favorites capped at 5)
        covered = {team_of(i) for i in sec.get("items", [])}
        for team in ("uk", "nyg", "usc"):
            if team in covered or sum(1 for i in sec.get("items", []) if i.get("group") == "Favorites") >= 5:
                continue
            for c in favs:
                h = c.get("team") or TEAM_OF_SOURCE.get(c.get("source") or "")
                if fav_team(_txt(c), c.get("sport"), title=c["title"], hint=h) == team and ok(c):
                    add(sec, c, "Favorites")
                    covered.add(team)
                    break
        order_sports(sec)
    if any(s.get("items") for s in secs):
        edition["sections"] = [s for s in secs if s.get("items")] + [s for s in secs if not s.get("items")]
    return added


def build_pool(fetch, feeds=None, sports=True):
    """Every candidate story from every source. Returns (pool, number of sources that answered)."""
    pool, ok = [], 0

    def pull(url, parse):
        nonlocal ok
        try:
            status, _, body, _ = fetch(url, 12, 900_000)
            if status != 200:
                return
            got = parse(body)
            if got:
                ok += 1
                pool.extend(got)
        except Exception:  # noqa: BLE001
            return

    for url, (outlet, buckets) in (feeds or FEEDS).items():
        pull(url, lambda b, o=outlet, k=buckets: parse_feed(b, o, k))
    if feeds is None and sports:
        for url, (outlet, sport, team) in SPORT_FEEDS.items():
            pull(url, lambda b, o=outlet, s=sport, t=team: parse_feed(b, o, {"sports"}, s, t))
        for path, sport in ESPN_LEAGUES.items():
            pull(ESPN_API + path + ("&" if "?" in path else "?") + "limit=25", lambda b, s=sport: parse_espn(b, s))
        for path, (sport, team) in ESPN_TEAMS.items():
            pull(ESPN_API + path + "&limit=25", lambda b, s=sport, t=team: parse_espn(b, s, t))
        for team, (q, sport) in GNEWS_TEAMS.items():
            if sum(1 for c in pool if "sports" in c["buckets"]
                   and fav_team(_txt(c), c.get("sport"), title=c["title"]) == team) < 2:
                url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:2d")
                       + "&hl=en-US&gl=US&ceid=US:en")
                pull(url, lambda b, s=sport, t=team: parse_gnews(b, s, t))
    return pool, ok


def run(edition, date, repo, passphrase, decrypt, fetch, dry=False):
    """Guard + top-up on an in-memory edition. Returns (changed, report dict)."""
    now = edition_now(date)
    cutoff = edition_cutoff(date, now)
    before = json.dumps(edition, sort_keys=True)
    dropped = drop_stale(edition, now, fetch, cutoff=cutoff)
    fixes = normalize(edition)
    thin = thin_sections(edition)
    sp = next((s for s in edition.get("sections", []) if sec_index(s.get("title")) == 5), {"items": []})
    groups_short = sum(i.get("group") == "Favorites" for i in sp["items"]) < 2 or \
        sum(i.get("group") == "General" for i in sp["items"]) < 2
    added, pool_n, feeds_ok = {}, 0, 0
    teams_due = False
    last = parse_dt(edition.get("teamchk"))
    if len({team_of(i) for i in sp["items"]} - {None}) < 3 and (not last or now - last > datetime.timedelta(hours=2)):
        teams_due = True
        edition["teamchk"] = now.isoformat(timespec="seconds")
    if thin or groups_short or teams_due:
        pool, feeds_ok = build_pool(fetch)
        pool_n = len(pool)
        urls, leads = prior_stories(repo, decrypt, passphrase, date)
        added = top_up(edition, now, pool, urls, leads, cutoff=cutoff)
    changed = json.dumps(edition, sort_keys=True) != before
    counts = {s.get("title"): len(s.get("items", [])) for s in edition.get("sections", [])}
    return changed, {"dropped_stale": dropped, "added": added, "section_counts": counts, "fixes": fixes,
                     "feeds_ok": feeds_ok, "pool": pool_n, "thin_before": thin, "missing_before": thin}

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


def _teaser(lead, n=7):
    """A glance chip from a headline: cut at a natural break, never mid-phrase with an ellipsis
    when a clean break exists."""
    t = re.sub(r"[.!?]+$", "", re.sub(r"\s+", " ", (lead or "").strip()))
    for sep in (": ", " — ", " – ", "; ", ", ", " as ", " after ", " amid ", " while "):
        head = t.split(sep)[0]
        if 3 <= len(head.split()) <= n and len(head) < len(t):
            return head
    w = t.split()
    if len(w) <= n + 1:
        return t
    w = w[:n]
    while w and w[-1].lower() in STOPWORDS | {"as", "after", "amid", "with", "to", "of"}:
        w.pop()
    return " ".join(w)


def _clean_lead(lead):
    return re.sub(r"\s+", " ", re.sub(r"[.!]+$", "", (lead or "").strip()))


def refresh_framing(edition, thin_before, force=False):
    """Headline / subtitle / glance chips must describe stories the edition actually carries.

    Oct 15, 2026: when this rewrote a headline it glued the first six words of two different
    sections' stories together with ellipses -- which is how "Morning news brief" (an NPR
    newsletter) became the front page headline. The hero card on the site IS sections[0].items[0],
    so a rebuilt headline is now that story's full headline, and the subtitle is its own summary."""
    changed = False
    secs_list = [s for s in edition.get("sections", []) if s.get("items")]
    secs = {sec_index(s.get("title")): s for s in secs_list}
    for g in edition.get("glance", []) or []:
        sec = secs.get(sec_index(g.get("label")))
        if not sec:
            continue
        orphan = max((similar(g.get("teaser", ""), f"{i.get('lead','')} {i.get('body','')}") for i in sec["items"]), default=0) < 0.34
        if force or orphan or not g.get("teaser") or QUIET_WORDS.search(g.get("teaser", "")) or "…" in g.get("teaser", "") \
                or (sec.get("title") in thin_before and any(i.get("auto") for i in sec["items"])):
            first = next((i for i in sec["items"] if not i.get("auto")), sec["items"][0])
            nt = _teaser(first.get("lead"))
            if nt and nt != g.get("teaser"):
                g["teaser"] = nt
                changed = True
    if not secs_list:
        return changed
    hero = secs_list[0]["items"][0]
    leads = [f"{i.get('lead','')} {i.get('body','')}" for s in secs_list for i in s.get("items", [])]
    lines = [x for x in (edition.get("headline") or "").split("\n") if x.strip()]
    stale = (not lines or any(max(similar(x, t) for t in leads) < 0.34 for x in lines)
             or "…" in (edition.get("headline") or "") or similar(lines[0], f"{hero.get('lead','')} {hero.get('body','')}") < 0.2)
    if force or stale:
        nh = _clean_lead(hero.get("lead"))
        if nh and nh != edition.get("headline"):
            edition["headline"] = nh
            body = (hero.get("body") or "").strip()
            edition["subtitle"] = "" if re.search(r"is reporting this story as it develops", body) else body
            changed = True
    if QUIET_WORDS.search(edition.get("subtitle", "") or ""):
        body = (hero.get("body") or "").strip()
        edition["subtitle"] = "" if re.search(r"is reporting this story as it develops", body) else body
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
    cutoff = edition_cutoff(date, now)
    _, rep = run(edition, date, repo, passphrase, decrypt, fetch)
    widened = {}
    if thin_sections(edition):
        # Still short: loosen ONLY the near-repeat filter (never a repeated URL) -- never the date
        # window. Eli's rule is that no story is older than its edition (Oct 8, 2026).
        pool, ok = build_pool(fetch)
        rep["feeds_ok"], rep["pool"] = max(rep.get("feeds_ok", 0), ok), max(rep.get("pool", 0), len(pool))
        urls, leads = prior_stories(repo, decrypt, passphrase, date)
        widened = top_up(edition, now, pool, urls, leads, prior_sim=0.85, cutoff=cutoff)
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
    if edition.get("auto_edition"):
        # an all-filler edition: best-ranked story leads each section (the brief's order otherwise)
        for s in edition.get("sections", []):
            si = sec_index(s.get("title"))
            if si is not None and si != 5 and all(i.get("auto") for i in s.get("items", [])):
                s["items"].sort(key=lambda i: -rank({"title": i.get("lead", ""), "img": i.get("img")}, si))
        edition["sources"] = ", ".join(sorted({i.get("source") for s in edition["sections"] for i in s.get("items", []) if i.get("source")}))
    refresh_framing(edition, thin_before, force=bool(edition.get("auto_edition")))
    rep.update({"widened": widened, "details_added": details, "still_thin": thin_sections(edition),
                "section_counts": section_counts(edition), "thin_before": thin_before,
                "sports_groups": sports_counts(edition)})
    return json.dumps(edition, sort_keys=True) != before, rep


def sports_counts(edition):
    sp = next((s for s in edition.get("sections", []) if sec_index(s.get("title")) == 5), {"items": []})
    return {g: sum(1 for i in sp.get("items", []) if i.get("group") == g) for g in ("Favorites", "General")}


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
                verdict, d = check_item(probe, now, fetch, budget, edition_cutoff(date, now))
                lead = (it.get("lead") or "")[:70]
                if verdict == "stale":
                    probs.append(f"ARTICLE IS OLD -- the page itself says {d.date()} (you wrote {it.get('published')}): {lead}  <{it.get('url')}>  Replace this story.")
                elif verdict == "unknown":
                    probs.append(f"could not confirm a publication date on the article page: {lead}  <{it.get('url')}>  Use an article whose page shows its date.")
    for t, n in section_counts(edition).items():
        if n < min_items:
            probs.append(f"section '{t}' has {n} stories -- needs at least {min_items}. Search again with different outlets.")
    now = edition_now(date)
    cutoff = edition_cutoff(date, now)
    for s in edition.get("sections", []):
        for it in s.get("items", []):
            if is_junk(it.get("lead"), it.get("url")):
                probs.append(f"not a news story (newsletter / column / how-to / live blog): {(it.get('lead') or '')[:70]}")
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
    # Sports (Oct 15, 2026): two groups, both present, each story in the right one.
    sp = next((x for x in edition.get("sections", []) if sec_index(x.get("title")) == 5), {"items": []})
    for it in sp.get("items", []):
        lead = (it.get("lead") or "")[:70]
        want = item_group(it)
        if it.get("group") not in ("Favorites", "General"):
            probs.append(f"Sports story has no group (Favorites or General): {lead}")
        elif want is None:
            probs.append(f"not one of Eli's sports (NFL, men's college basketball, college football, PGA, UFC, men's Grand Slam tennis, NBA): {lead}")
        elif it.get("group") != want:
            probs.append(f"Sports story is in '{it.get('group')}' but belongs in '{want}' (Favorites = ONLY Kentucky men's basketball, NY Giants, USC football): {lead}")
    for g in ("Favorites", "General"):
        n = sum(1 for i in sp.get("items", []) if i.get("group") == g)
        if n < 2:
            probs.append(f"Sports has {n} '{g}' stories -- must carry 2 or more.")
    secs = [x for x in edition.get("sections", []) if x.get("items")]
    if secs:
        leads = [f"{i.get('lead','')} {i.get('body','')}" for x in secs for i in x["items"]]
        for line in (edition.get("headline") or "").split("\n"):
            if line.strip() and max(similar(line, t) for t in leads) < 0.25:
                probs.append(f"headline line is not about any story in the edition: {line[:70]}")
    return probs
