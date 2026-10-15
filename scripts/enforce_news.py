#!/usr/bin/env python3
"""The news floor, as a command. Deterministic -- no model involved.

  enforce_news.py <passphrase_file> [repo_dir] [--date=YYYY-MM-DD] [--strict]
      Load today's published edition (or start an empty one if the brief published nothing),
      run news_guard.enforce() on it -- drop stale stories, top up every section to at least
      MIN_ITEMS from fresh publisher feeds, widen the window if still short, fill article text,
      repair "quiet day" framing -- then re-encrypt, rebuild the manifest and write
      data/news.status.json. With --strict, exits 3 if any section is still under the floor.

  enforce_news.py --check /tmp/news.json [--date=YYYY-MM-DD]
      For the brief, BEFORE it publishes: lists every section under the floor, every story
      without a verified in-window date or source URL, and every "quiet" glance chip.
      Exits 1 if there is anything to fix.

Why (Oct 12, 2026): an edition went out with two stories in total and the workflow called it a
success. See the enforcement notes in news_guard.py.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_guard  # noqa: E402
from dh_crypto import decrypt_json, encrypt_json, read_passphrase  # noqa: E402


def today_ct():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    except Exception:  # noqa: BLE001
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=5)).date().isoformat()


def rebuild_manifest(repo):
    m = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    path = os.path.join(repo, "data", "manifest.json")
    try:
        old = json.load(open(path))
        m.update({k: v for k, v in old.items() if k not in ("updated", "market", "news", "calendar")})
    except Exception:  # noqa: BLE001
        pass
    for kind in ("market", "news", "calendar"):
        d = os.path.join(repo, "data", kind)
        dates = [f.split(".json")[0] for f in os.listdir(d)] if os.path.isdir(d) else []
        m[kind] = sorted({x for x in dates if len(x) == 10 and x.count("-") == 2}, reverse=True)
    json.dump(m, open(path, "w"), indent=1)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    date = next((a.split("=", 1)[1] for a in flags if a.startswith("--date=")), None) or today_ct()

    if "--check" in flags:
        ed = json.load(open(args[0]))
        import enrich_news
        fetch = None if "--offline" in flags else (lambda u, t=12, mb=900_000: enrich_news.get(u, timeout=t, max_bytes=mb))
        probs = news_guard.check_plain(ed, ed.get("date") or date, fetch=fetch)
        counts = news_guard.section_counts(ed)
        print("section counts:", counts, "| sports groups:", news_guard.sports_counts(ed))
        if probs:
            print(f"NOT READY -- {len(probs)} problem(s). Fix every one, then run this check again:")
            for p in probs:
                print("  -", p)
            return 1
        print("READY: every section has at least", news_guard.MIN_ITEMS, "dated, sourced stories.")
        return 0

    if not args:
        print(__doc__)
        return 0
    passphrase = read_passphrase(args[0])
    repo = args[1] if len(args) > 1 else "."
    path = os.path.join(repo, "data", "news", f"{date}.json.enc")
    created = False
    if os.path.exists(path):
        edition = decrypt_json(json.load(open(path)), passphrase)
    else:
        edition, created = news_guard.skeleton(date), True
        print(f"no edition for {date} -- building one from wire feeds")

    import enrich_news   # its fetcher sends a real browser UA and caps body size
    fetch = lambda u, t=12, mb=700_000: enrich_news.get(u, timeout=t, max_bytes=mb)  # noqa: E731
    changed, rep = news_guard.enforce(edition, date, repo, passphrase, decrypt_json, fetch)
    for line in rep.get("dropped_stale", []):
        print("  - dropped stale:", line)
    for line in rep.get("fixes", []) or []:
        print("  - repaired:", line)
    print(f"news {date}: counts {rep['section_counts']}; added {rep.get('added')}; widened {rep.get('widened')}; "
          f"details +{rep.get('details_added')}; feeds ok {rep.get('feeds_ok')}; still thin {rep['still_thin']}")

    has_items = any(s.get("items") for s in edition.get("sections", []))
    if (changed or created) and has_items:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(encrypt_json(edition, passphrase), open(path, "w"))
        rebuild_manifest(repo)
        print(f"  wrote {path}")
    try:
        st_path = os.path.join(repo, "data", "news.status.json")
        st = json.load(open(st_path)) if os.path.exists(st_path) else {}
        st.update({"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                   "date": date, "section_counts": rep["section_counts"], "still_thin": rep["still_thin"],
                   "sports_groups": rep.get("sports_groups"), "fixes": len(rep.get("fixes") or []),
                   "auto_edition": bool(edition.get("auto_edition")), "enforced_by": "enforce_news.py"})
        json.dump(st, open(st_path, "w"), indent=1)
    except Exception:  # noqa: BLE001
        pass
    if "--strict" in flags and rep["still_thin"]:
        print("::error::news floor not met for:", ", ".join(rep["still_thin"]))
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
