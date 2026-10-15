#!/usr/bin/env python3
"""Who wrote today's news edition, and how many times has the brief tried today?

  TODAY=YYYY-MM-DD brief_state.py <manifest.json> <attempts.json> <passphrase_file>
      prints "<kind> <attempts>" where kind is model | auto | missing | unreadable.
      Used by daily-brief.yml's guard: only a MODEL-written edition counts as published.

  TODAY=YYYY-MM-DD brief_state.py --record <passphrase_file> <brief step outcome>
      bumps data/brief-attempts.json for today and records whether the live edition is the
      model's own. (plain JSON: a date and two counters -- nothing about the news itself)

Why (Oct 15, 2026): the brief's model step was refused three mornings running, the news floor
built a wire-feed-only edition each time, and the guard then treated the day as published, so the
brief was never retried. The cap of 3 attempts keeps a persistently failing brief from re-running
every 15 minutes all day.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def edition_kind(raw, passphrase_file):
    try:
        from dh_crypto import decrypt_json, read_passphrase
        ed = decrypt_json(json.loads(raw), read_passphrase(passphrase_file))
    except Exception:  # noqa: BLE001
        return "unreadable"
    return "auto" if ed.get("auto_edition") else "model"


def main():
    t = os.environ.get("TODAY", "")
    if sys.argv[1:2] == ["--record"]:
        path = "data/brief-attempts.json"
        try:
            a = json.load(open(path))
        except Exception:  # noqa: BLE001
            a = {}
        if a.get("date") != t:
            a = {"date": t, "attempts": 0}
        a["attempts"] = a.get("attempts", 0) + 1
        p = f"data/news/{t}.json.enc"
        a["model_edition"] = os.path.exists(p) and edition_kind(open(p).read(), sys.argv[2]) == "model"
        a["last_brief_step"] = sys.argv[3] if len(sys.argv) > 3 else ""
        json.dump(a, open(path, "w"), indent=1)
        print("brief attempts:", a)
        return 0
    manifest, attempts, pf = sys.argv[1:4]
    try:
        a = json.load(open(attempts))
    except Exception:  # noqa: BLE001
        a = {}
    tries = a.get("attempts", 0) if a.get("date") == t else 0
    try:
        news = json.load(open(manifest)).get("news", [])
    except Exception:  # noqa: BLE001
        news = []
    if t not in news:
        print("missing", tries)
        return 0
    r = subprocess.run(["git", "show", f"origin/main:data/news/{t}.json.enc"], capture_output=True, text=True)
    print(edition_kind(r.stdout, pf) if r.returncode == 0 else "unreadable", tries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
