#!/usr/bin/env python3
"""Print the lead photo (og:image / twitter:image) for one or more article URLs.

Usage: python3 scripts/og_image.py <url> [<url> ...]
Output: one line per URL:  <url>\t<image-url or ->

Used by the daily brief (.github/claude-daily-brief-prompt.md) so each news story can carry
the actual photo from the article it came from, instead of a Wikipedia lookup on a guessed
subject. Never raises; a page that can't be fetched just yields "-".
"""
import html
import re
import sys
import urllib.request
from urllib.parse import urljoin

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
META = re.compile(r'<meta[^>]+>', re.I)
BAD = re.compile(r'logo|icon|favicon|default|placeholder|avatar|sprite|\.svg(\?|$)|1x1|pixel', re.I)


def attrs(tag):
    return {k.lower(): html.unescape(v) for k, v in re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', tag)} | \
           {k.lower(): html.unescape(v) for k, v in re.findall(r"([\w:-]+)\s*=\s*'([^']*)'", tag)}


def lead_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=12) as r:   # stdlib only: no pip dependency in the brief job
            if r.status != 200 or "html" not in (r.headers.get("Content-Type") or ""):
                return None
            final_url = r.geturl()
            head = r.read(400_000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    cands = []
    for tag in META.findall(head):
        a = attrs(tag)
        key = (a.get("property") or a.get("name") or "").lower()
        if key in ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src"):
            v = (a.get("content") or "").strip()
            if v:
                cands.append(urljoin(final_url, v))
    for c in cands:
        if c.startswith("http") and not BAD.search(c):
            return c
    return None


def main():
    for u in sys.argv[1:]:
        print(f"{u}\t{lead_image(u) or '-'}")


if __name__ == "__main__":
    main()
