#!/usr/bin/env python3
"""Season Ticket: next game / last result / record for Eli's teams -> data/sports.json.enc

Kentucky men's basketball, New York Giants, USC football, from ESPN's public site API
(no key). Runs as a companion of the Calendar Refresh workflow (see refresh_calendar.py).
Exit code is always 0; a failed fetch leaves the previous file alone.

Usage: python3 refresh_sports.py <passphrase_file> [repo_dir]
"""
import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_json, encrypt_json, read_passphrase  # noqa: E402

CT = ZoneInfo("America/Chicago")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
TEAMS = [
    {"name": "Kentucky Basketball", "short": "UK", "sport": "basketball", "league": "mens-college-basketball", "id": "96"},
    {"name": "New York Giants", "short": "NYG", "sport": "football", "league": "nfl", "id": "19"},
    {"name": "USC Football", "short": "USC", "sport": "football", "league": "college-football", "id": "30"},
]


def fetch_schedule(t, session):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{t['sport']}/{t['league']}/teams/{t['id']}/schedule"
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_events(js, team_id):
    out = []
    for ev in js.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        us, them = None, None
        for c in comp.get("competitors", []):
            if str((c.get("team") or {}).get("id")) == str(team_id) or str(c.get("id")) == str(team_id):
                us = c
            else:
                them = c
        if not us or not them:
            continue
        status = ((comp.get("status") or ev.get("status") or {}).get("type") or {})
        try:
            when = datetime.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(CT)
        except Exception:  # noqa: BLE001
            continue
        opp = (them.get("team") or {})
        opp_name = opp.get("shortDisplayName") or opp.get("abbreviation") or opp.get("displayName") or "TBD"
        home = us.get("homeAway") == "home"
        item = {
            "date": when.strftime("%Y-%m-%d"),
            "time": (when.strftime("%-I:%M %p").replace(":00", "") + " CT") if comp.get("timeValid", True) else "TBD",
            "label": ("vs. " if home else "@ ") + opp_name,
            "home": home,
            "completed": bool(status.get("completed")),
            "url": next((l.get("href") for l in ev.get("links", []) if "href" in l), None),
        }
        if item["completed"]:
            try:
                s_us = (us.get("score") or {}).get("value") if isinstance(us.get("score"), dict) else us.get("score")
                s_them = (them.get("score") or {}).get("value") if isinstance(them.get("score"), dict) else them.get("score")
                s_us, s_them = int(float(s_us)), int(float(s_them))
                won = us.get("winner") if us.get("winner") is not None else s_us > s_them
                item["result"] = f"{'W' if won else 'L'} {s_us}–{s_them}"
            except Exception:  # noqa: BLE001
                item["result"] = "Final"
        out.append(item)
    out.sort(key=lambda x: x["date"])
    return out


def main():
    if len(sys.argv) < 2:
        return 0
    passphrase = read_passphrase(sys.argv[1])
    repo = sys.argv[2] if len(sys.argv) > 2 else "."
    out_path = os.path.join(repo, "data", "sports.json.enc")
    previous = None
    if os.path.exists(out_path):
        try:
            previous = decrypt_json(json.load(open(out_path)), passphrase)
        except Exception:  # noqa: BLE001
            previous = None

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json"})
    today = datetime.datetime.now(CT).strftime("%Y-%m-%d")
    teams, ok = [], 0
    for t in TEAMS:
        try:
            js = fetch_schedule(t, session)
            events = parse_events(js, t["id"])
            record = None
            try:
                items = ((js.get("team") or {}).get("recordSummary")) or None
                record = items if isinstance(items, str) else None
            except Exception:  # noqa: BLE001
                record = None
            nxt = next((e for e in events if not e["completed"] and e["date"] >= today), None)
            last = next((e for e in reversed(events) if e["completed"]), None)
            teams.append({"name": t["name"], "short": t["short"], "record": record, "next": nxt, "last": last})
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! {t['name']}: {e}", file=sys.stderr)
            if previous:
                prev_t = next((p for p in previous.get("teams", []) if p.get("short") == t["short"]), None)
                if prev_t:
                    teams.append(prev_t)
    if not ok:
        print("ESPN returned nothing; leaving the previous sports file untouched")
        return 0
    payload = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "teams": teams}
    if previous and previous.get("teams") == teams:
        print("sports unchanged; not rewriting")
        return 0
    with open(out_path, "w") as f:
        json.dump(encrypt_json(payload, passphrase), f)
    print(f"wrote {out_path}: {ok}/{len(TEAMS)} teams refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
