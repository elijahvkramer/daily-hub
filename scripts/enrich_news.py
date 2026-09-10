#!/usr/bin/env python3
"""Backfill real photos (and source links) onto the published news edition.

Why this exists (Sep 2026): the site has never reliably shown news photos. Every previous
attempt put the job on the browser (a Wikipedia lookup on a subject the brief guessed) or on
the brief itself (which runs once a day and, before this, published no `img` at all). Today's
edition, for example, shipped with zero `img` and zero `url` on all 17 items.

So this runs on the server instead, as a companion of the Calendar Refresh workflow -- every
~30 minutes, against whatever edition is currently published. It is idempotent and additive:
it only fills fields that are missing, never rewrites prose, and never touches an item that
already has a photo.

Resolution order per item:
  1. `url` already on the item        -> that article's og:image
  2. Google News RSS search on the headline -> best-matching real article
                                       -> fills url + source, then that article's og:image
  3. Wikipedia page image for the item's `imgQuery` or the strongest proper noun in the lead
Every candidate image is verified (HTTP 200, image content-type, >= 400px wide by header or
by a small ranged read) before it is written, so the page never gets a broken box.

Writes data/news.status.json (plaintext, no story content) so a run can be audited from the
repo alone.

Usage: python3 enrich_news.py <passphrase_file> [repo_dir] [--date YYYY-MM-DD] [--dry-run]
Always exits 0 -- enrichment must never fail the calendar refresh job.
"""
import datetime
import html
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_json, encrypt_json, read_passphrase  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
META = re.compile(r"<meta[^>]+>", re.I)
BAD_IMG = re.compile(r"logo|favicon|sprite|placeholder|default[-_.]|avatar|1x1|pixel|spacer|"
                     r"\.svg(\?|$)|blank|promo_|/icons?/", re.I)
STOP = set("""the a an and or but of in on at to for with from by as is are was were be been being
this that these those it its his her their our your my new more most over under after before
during while when where what who why how said says say will would could should can may might
us u.s american americans first second third year years day days week weeks month months""".split())


def get(url, timeout=12, max_bytes=400_000, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers, r.read(max_bytes), r.geturl()


def attrs(tag):
    out = {}
    for k, v in re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', tag):
        out[k.lower()] = html.unescape(v)
    for k, v in re.findall(r"([\w:-]+)\s*=\s*'([^']*)'", tag):
        out.setdefault(k.lower(), html.unescape(v))
    return out


def page_meta(url):
    """Return (og_image, og_title, site_name) for an article URL."""
    try:
        status, headers, body, final = get(url)
        if status != 200 or "html" not in (headers.get("Content-Type") or "").lower():
            return None, None, None
        head = body.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"    . page {url[:60]}: {e}", file=sys.stderr)
        return None, None, None
    img = title = site = None
    for tag in META.findall(head):
        a = attrs(tag)
        key = (a.get("property") or a.get("name") or "").lower()
        val = (a.get("content") or "").strip()
        if not val:
            continue
        if key in ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src") and not img:
            cand = urllib.parse.urljoin(final, val)
            if cand.startswith("http") and not BAD_IMG.search(cand):
                img = cand
        elif key == "og:title" and not title:
            title = val
        elif key == "og:site_name" and not site:
            site = val
    return img, title, site


def image_ok(url):
    """A photo, reachable, and big enough to fill a card."""
    try:
        status, headers, body, _ = get(url, timeout=10, max_bytes=65_536)
        if status != 200:
            return False
        ctype = (headers.get("Content-Type") or "").lower()
        if not ctype.startswith("image/") or "svg" in ctype:
            return False
        length = headers.get("Content-Length")
        if length and int(length) < 12_000:      # thumbnails and tracking pixels
            return False
        try:
            from PIL import Image  # optional; absent on the runner is fine
            w, h = Image.open(io.BytesIO(body)).size
            return w >= 400 and h >= 220
        except Exception:  # noqa: BLE001
            return True
    except Exception as e:  # noqa: BLE001
        print(f"    . image {url[:60]}: {e}", file=sys.stderr)
        return False


def keywords(text, n=8):
    words = re.findall(r"[A-Za-z][A-Za-z'.-]+", text or "")
    out, seen = [], set()
    for w in words:
        lw = w.lower().strip(".'-")
        if len(lw) < 3 or lw in STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= n:
            break
    return out


def news_search(query, when="2d"):
    """Google News RSS -> [(title, link, source, pubDate)]. Public feed, no key."""
    url = ("https://news.google.com/rss/search?q=" +
           urllib.parse.quote(f"{query} when:{when}") + "&hl=en-US&gl=US&ceid=US:en")
    try:
        status, _, body, _ = get(url, timeout=12, max_bytes=300_000)
        if status != 200:
            return []
        root = ET.fromstring(body)
    except Exception as e:  # noqa: BLE001
        print(f"    . news search '{query[:40]}': {e}", file=sys.stderr)
        return []
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # Prefer the publisher's own URL when the description carries it: the <link> is a
        # news.google.com redirect that frequently serves a consent/JS interstitial, which
        # has no og:image on it.
        desc = it.findtext("description") or ""
        m = re.search(r'href="(https?://(?!news\.google\.)[^"]+)"', html.unescape(desc))
        if m:
            link = m.group(1)
        if title and link:
            items.append((title, link, source, (it.findtext("pubDate") or "")))
    return items


def overlap(a, b):
    ta = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", a or "")} - STOP
    tb = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", b or "")} - STOP
    return len(ta & tb) / max(3, min(len(ta), len(tb))) if ta and tb else 0


def resolve_article(item):
    """Find the real article this story is about; returns (url, source) or (None, None)."""
    text = (item.get("lead") or "") + " " + (item.get("body") or "")
    kws = keywords(text, 8)
    if len(kws) < 3:
        return None, None
    for query in (" ".join(kws[:6]), " ".join(kws[:4])):
        for title, link, source, _ in news_search(query)[:6]:
            if overlap(title, text) >= 0.34:
                return link, (source or None)
    return None, None


def wiki_image(subject):
    if not subject:
        return None
    url = ("https://en.wikipedia.org/w/api.php?action=query&generator=search&gsrsearch=" +
           urllib.parse.quote(subject) +
           "&gsrlimit=3&prop=pageimages&piprop=thumbnail|original&pithumbsize=1200&format=json")
    try:
        status, _, body, _ = get(url, timeout=12, max_bytes=200_000)
        if status != 200:
            return None
        pages = ((json.loads(body).get("query") or {}).get("pages") or {})
    except Exception as e:  # noqa: BLE001
        print(f"    . wiki '{subject[:40]}': {e}", file=sys.stderr)
        return None
    norm = lambda t: re.sub(r"[^a-z0-9 ]", "", re.sub(r"\(.*?\)", "", (t or "").lower())).strip()
    want = norm(subject)
    best = None
    for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
        src = (p.get("thumbnail") or {}).get("source")
        if not src or BAD_IMG.search(src):
            continue
        title = norm(p.get("title"))
        if title == want:
            return src
        if best is None and (title in want or want in title):
            best = src
    return best


def lead_subject(item):
    """The strongest proper-noun phrase in the lead -- last-resort photo subject."""
    txt = re.sub(r"\s+", " ", item.get("lead") or "")
    runs = re.findall(r"[A-Z][A-Za-z.'’-]+(?:\s+(?:of\s+|the\s+)?[A-Z][A-Za-z.'’-]+){0,3}", txt)
    out = []
    for r in runs:
        r = re.sub(r"['’]s\b", "", r).strip()          # "Zelenskyy's" -> "Zelenskyy"
        r = re.sub(r"^\w+\s+(?=[A-Z])", "", r) if re.match(r"^[A-Z]{2,4}\s+[A-Z]", r) else r
        if len(r) > 4 and r.split()[0].lower() not in STOP:
            out.append(r)
    return max(out, key=len) if out else None


def enrich(item, budget):
    """Fill img/imgCaption (and url/source when we can find them). Returns a note string."""
    if item.get("img"):
        return None
    # 1. article already known
    url = item.get("url")
    # 2. find the article
    if not url and budget["search"] > 0:
        budget["search"] -= 1
        url, source = resolve_article(item)
        if url:
            item["url"] = url
            if source and not item.get("source"):
                item["source"] = source
    if url and budget["page"] > 0:
        budget["page"] -= 1
        img, _, site = page_meta(url)
        if site and not item.get("source"):
            item["source"] = site
        if img and image_ok(img):
            item["img"] = img
            item.setdefault("imgCaption", (item.get("source") or "News photo"))
            return "article"
    # 3. Wikipedia subject
    subject = item.get("imgQuery") or lead_subject(item)
    if subject and budget["wiki"] > 0:
        budget["wiki"] -= 1
        img = wiki_image(subject)
        if img and image_ok(img):
            item["img"] = img
            item.setdefault("imgCaption", subject)
            return "wiki"
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print("usage: enrich_news.py <passphrase_file> [repo_dir] [--date YYYY-MM-DD] [--dry-run]", file=sys.stderr)
        return 0
    passphrase = read_passphrase(args[0])
    repo = args[1] if len(args) > 1 else "."
    date = next((a.split("=", 1)[1] for a in flags if a.startswith("--date=")), None)
    dry = "--dry-run" in flags

    manifest_path = os.path.join(repo, "data", "manifest.json")
    if not date:
        try:
            date = sorted(json.load(open(manifest_path)).get("news", []), reverse=True)[0]
        except Exception:  # noqa: BLE001
            print("no published news edition to enrich")
            return 0
    path = os.path.join(repo, "data", "news", f"{date}.json.enc")
    if not os.path.exists(path):
        print(f"no news file for {date}")
        return 0

    try:
        edition = decrypt_json(json.load(open(path)), passphrase)
    except Exception as e:  # noqa: BLE001
        print(f"  ! could not read {path}: {e}", file=sys.stderr)
        return 0

    items = [it for sec in edition.get("sections", []) for it in sec.get("items", [])]
    missing = [it for it in items if not it.get("img")]
    if not missing:
        print(f"news {date}: all {len(items)} items already have photos")
        return 0

    # Bounded work per run: the job repeats every ~30 minutes, so it converges over the day
    # instead of hammering anything in one burst.
    budget = {"search": 10, "page": 14, "wiki": 12}
    got = {"article": 0, "wiki": 0}
    for it in missing:
        note = enrich(it, budget)
        if note:
            got[note] += 1
        if budget["page"] <= 0 and budget["wiki"] <= 0:
            break

    filled = got["article"] + got["wiki"]
    status = {"date": date, "items": len(items), "had_photos": len(items) - len(missing),
              "filled_this_run": filled, "from_article": got["article"], "from_wikipedia": got["wiki"],
              "still_missing": len(missing) - filled}
    try:
        with open(os.path.join(repo, "data", "news.status.json"), "w") as f:
            json.dump({"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **status}, f, indent=1)
    except Exception:  # noqa: BLE001
        pass

    if not filled:
        print(f"news {date}: no new photos resolved ({len(missing)} still without)")
        return 0
    if dry:
        print(f"news {date}: DRY RUN -- would fill {filled} ({got})")
        return 0
    with open(path, "w") as f:
        json.dump(encrypt_json(edition, passphrase), f)
    print(f"news {date}: +{filled} photos ({got['article']} from the article, {got['wiki']} from Wikipedia); "
          f"{status['still_missing']} still without")
    return 0


if __name__ == "__main__":
    sys.exit(main())
