#!/usr/bin/env python3
"""Frequent, computer-independent refresh of the Daily Hub site's `today`/`radar`
calendar fields — runs on a GitHub Actions cron, authenticating as a Google
service account (calendar-refresh-bot@daily-hub-todos.iam.gserviceaccount.com)
that Eli has shared his calendars with. No OAuth popups, no token expiry,
no dependence on Eli's computer or the Cowork app being open (that's the
whole point vs. the retired client-side browser-OAuth approach and vs. a
Cowork scheduled task).

Fetches events from Eli's primary + Family + Brooklyn+Eli + Redtail calendars, plus the
public Holidays in United States calendar (readable without a share), buckets
them into today/radar using the exact same lead/body convention the
daily-calendar-report SKILL.md and the site's parseEventLead() expect
(ported from index.html's gcalFormatEvent/gcalBucketEvents), then merges
ONLY those two fields into the current day's published calendar file —
preserving weather/hourly/urgent/chill/close, which remain the daily
scheduled task's job — and re-publishes.

Usage: python3 refresh_calendar.py <service_account_json_path> <passphrase_file> [repo_dir]
repo_dir defaults to the current directory (the GitHub Actions checkout).
"""
import base64
import datetime
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
import google.auth.transport.requests as gareq

CT = ZoneInfo("America/Chicago")
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Shared with calendar-refresh-bot@daily-hub-todos.iam.gserviceaccount.com
# at "See all event details" — see MEMORY.md.
CALENDAR_IDS = [
    "elijahvkramer@gmail.com",
    "family13940029292116141615@group.calendar.google.com",
    "6668454f24e15ba8a30ea5f496b32f9713c27bb5bde025f21eaaea6f831a09e9@group.calendar.google.com",
    "73f90f2a90ecc6ef0895707a47af926c2f70714a4ad692aac3871c38054681e3@group.calendar.google.com",  # Redtail
    "en.usa#holiday@group.v.calendar.google.com",  # public calendar; readable without an explicit share
]

WINDOW_DAYS = 15  # matches the retired live-sync window (today + 15 days)


# ---------- Google Calendar ----------

def get_credentials(key_path):
    creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    creds.refresh(gareq.Request())
    return creds


def fetch_events(creds, calendar_id, time_min, time_max):
    from urllib.parse import quote
    url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"
    headers = {"Authorization": f"Bearer {creds.token}"}
    items = []
    page_token = None
    while True:
        params = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 404:
            print(f"  ! calendar not found/not shared: {calendar_id}", file=sys.stderr)
            return items
        r.raise_for_status()
        j = r.json()
        items.extend(j.get("items", []))
        page_token = j.get("nextPageToken")
        if not page_token:
            break
    return items


# ---------- lead/body formatting (ported from gcalFormatEvent/gcalBucketEvents) ----------

def fmt_ct_time(dt):
    d = dt.astimezone(CT)
    h = d.strftime("%I").lstrip("0") or "12"
    m = d.strftime("%M")
    ap = d.strftime("%p").lower()
    return (h if m == "00" else f"{h}:{m}") + ap


def ct_date_str(dt):
    return dt.astimezone(CT).strftime("%Y-%m-%d")


def format_event(ev, is_today):
    s = ev.get("start") or {}
    e = ev.get("end") or {}
    is_all_day = bool(s.get("date") and not s.get("dateTime"))
    summary = ev.get("summary") or "(No title)"
    location = ev.get("location")
    body = summary + (f" · {location}" if location else "")

    time_line = ""
    end_date_str = None  # only set for events that genuinely span 2+ calendar days (CT)
    if is_all_day:
        dt = datetime.datetime.strptime(s["date"], "%Y-%m-%d")
        weekday = dt.strftime("%a")
        md = f"{dt.month}/{dt.day}"
        if e.get("date"):
            # Google's all-day "end.date" is EXCLUSIVE (the day after the event
            # actually ends), so the last inclusive day is end.date minus one.
            end_dt = datetime.datetime.strptime(e["date"], "%Y-%m-%d") - datetime.timedelta(days=1)
            if end_dt.date() > dt.date():
                end_date_str = end_dt.strftime("%Y-%m-%d")
    else:
        # timed events NEVER get an endDate/span treatment, even when they
        # run past midnight (e.g. a 9:30pm-12:30am reception) -- they still
        # belong to their start day, just with their full time range shown
        # (matching how Google Calendar's own month view handles them). Only
        # genuine multi-day ALL-DAY events (the is_all_day branch above,
        # e.g. a multi-day "FOCUS" block) render as a spanning bar.
        start_dt = datetime.datetime.fromisoformat(s["dateTime"])
        start_ct = start_dt.astimezone(CT)
        weekday = start_ct.strftime("%a")
        md = f"{start_ct.month}/{start_ct.day}"
        time_line = fmt_ct_time(start_dt)
        if e.get("dateTime"):
            end_dt = datetime.datetime.fromisoformat(e["dateTime"])
            time_line += "–" + fmt_ct_time(end_dt)
        time_line += " CT"

    if is_today:
        lead = f"{time_line} —" if time_line else ""
    else:
        lead = f"{weekday} {md}" + (f", {time_line}" if time_line else "") + " —"
    result = {"lead": lead, "body": body}
    if end_date_str:
        result["endDate"] = end_date_str  # additive field; existing {lead,body} shape unchanged otherwise
    return result


def bucket_events(items, today_date_str):
    today, radar = [], []
    for ev in items:
        if ev.get("status") == "cancelled":
            continue
        s = ev.get("start") or {}
        is_all_day = bool(s.get("date") and not s.get("dateTime"))
        if is_all_day:
            date_str = s["date"]
        elif s.get("dateTime"):
            date_str = ct_date_str(datetime.datetime.fromisoformat(s["dateTime"]))
        else:
            continue
        if date_str == today_date_str:
            today.append(format_event(ev, True))
        elif date_str > today_date_str:
            radar.append(format_event(ev, False))
    # stable de-dup: same calendar item can't repeat, but overlapping calendars
    # (e.g. an event Eli is on in two calendars) could — collapse exact dupes.
    def dedupe(lst):
        seen = set()
        out = []
        for item in lst:
            key = (item["lead"], item["body"], item.get("endDate"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out
    return dedupe(today), dedupe(radar)


# ---------- encryption (shared with every other script: scripts/dh_crypto.py) ----------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import encrypt_bytes as encrypt_payload, decrypt_payload  # noqa: E402


# ---------- main ----------

# ---------------------------------------------------------------------------------------
# Countdowns: Eli, Sep 2026 -- "always be looking at my calendar for things that need to be
# added to the countdown at the top, and keep them there (upcoming bdays, drill, bach party,
# southwest credits expiring, next big holidays, golf trips, registration for things, etc.).
# Use discretion but always err on the side of adding it, because I can always delete it
# myself after it's added."
#
# So this scans a whole year of calendar ahead (not the 15-day dashboard window), classifies
# anything that reads like an occasion, and publishes it as `countdowns`. Ids are stable
# (cal:<date>:<slug>), which is what lets the site remember the ones he deleted. Routine work
# meetings are the only thing filtered out; everything else gets in.
COUNTDOWN_HORIZON_DAYS = 400

# (emoji, kind, pattern) -- first match wins, so the specific ones come first.
COUNTDOWN_RULES = [
    ("\U0001F382", "birthday",   r"\bb-?day\b|\bbirthday\b|\bturns \d+"),
    ("\U0001F396",  "drill",      r"\bdrill\b|battle assembly|\bUTA\b|annual training|\bAT\b(?![a-z])|reserve weekend|\bmuster\b"),
    ("\U0001F942", "party",      r"bachelor|bachelorette|\bstag\b|rehearsal dinner|\bengagement party\b"),
    ("\U0001F48D", "wedding",    r"\bwedding\b|\belopement\b|\bceremony\b|\bofficiant\b"),
    ("⏳",      "deadline",   r"\bexpir\w*|\bdeadline\b|\blast day\b|\bdue\b|\brenew\w*|\bfile by\b|\bcutoff\b|\bends\b"),
    ("\U0001F4DD", "signup",     r"\bregistration\b|\bregister\b|\bsign ?up\b|\benroll\w*|\bapplication\b|\bapply by\b|\bdraft\b.*\bdeadline\b"),
    ("⛳",      "golf",       r"\bgolf\b|\btee time\b|\btee off\b|\bscramble\b|\bmember-guest\b|\bcourse\b"),
    ("✈",      "trip",       r"\bflight\b|\bflights\b|\btrip\b|\bvacation\b|\bgetaway\b|\bdepart\w*|\blanding\b|\bhoneymoon\b|\bcruise\b|\bairport\b"),
    ("\U0001F3AB", "event",      r"\bconcert\b|\bgame\b|\btickets?\b|\bfestival\b|\bshow\b|\bmatch\b|\bkickoff\b|\bpremiere\b"),
    ("\U0001F393", "exam",       r"\bexam\b|\btest\b|\bseries \d+|\bSIE\b|\blicens\w*|\bproctor\w*|\bcertification\b"),
    ("\U0001F3E1", "home",       r"\bclosing\b|\binspection\b|\bappraisal\b|\bmove-?in\b|\bwalkthrough\b|\bclosing day\b"),
    ("\U0001F3E5", "appt",       r"\bdentist\b|\bdoctor\b|\bappointment\b|\bphysical\b|\bvet\b|\bfingerprint\w*"),
    ("\U0001F389", "holiday",    r"\banniversary\b|\bgraduation\b|\bbaby shower\b|\breunion\b|\bretirement\b"),
]
# Routine work noise. Deliberately short -- Eli would rather delete one chip than miss one.
COUNTDOWN_SKIP = re.compile(
    r"^\s*(1:1|one[- ]on[- ]one|stand ?up|daily sync|weekly sync|sync\b|check ?in\b|"
    r"team meeting|staff meeting|office hours|focus( time)?|block\b|hold\b|busy\b|lunch\b|"
    r"gym\b|workout\b|prospecting|cold calls?|admin\b|email\b|commute\b|drive\b)", re.I)

# Fixed-date federal / cultural holidays worth a countdown, plus the movable ones we can
# compute. Month/day pairs; the movable ones are handled below.
FIXED_HOLIDAYS = [
    ((1, 1),   "New Year's Day",   "\U0001F386"),
    ((2, 14),  "Valentine's Day",  "❤"),
    ((7, 4),   "Fourth of July",   "\U0001F386"),
    ((10, 31), "Halloween",        "\U0001F383"),
    ((12, 24), "Christmas Eve",    "\U0001F384"),
    ((12, 25), "Christmas",        "\U0001F384"),
    ((12, 31), "New Year's Eve",   "\U0001F942"),
]


def _nth_weekday(year, month, weekday, n):
    """n-th (1-based) `weekday` of a month; n = -1 means the last one."""
    d = datetime.date(year, month, 1)
    offs = (weekday - d.weekday()) % 7
    first = d + datetime.timedelta(days=offs)
    if n > 0:
        return first + datetime.timedelta(weeks=n - 1)
    last = first
    while (last + datetime.timedelta(weeks=1)).month == month:
        last += datetime.timedelta(weeks=1)
    return last


def holiday_countdowns(today, horizon):
    """The next occurrence of each big holiday inside the horizon."""
    out = []
    for year in (today.year, today.year + 1):
        cands = [(datetime.date(year, m, d), name, emo) for (m, d), name, emo in FIXED_HOLIDAYS]
        cands.append((_nth_weekday(year, 11, 3, 4), "Thanksgiving", "\U0001F983"))       # 4th Thursday
        cands.append((_nth_weekday(year, 5, 0, -1), "Memorial Day", "\U0001F1FA\U0001F1F8"))   # last Monday
        cands.append((_nth_weekday(year, 9, 0, 1), "Labor Day", "\U0001F1FA\U0001F1F8"))        # 1st Monday
        for d, name, emo in cands:
            if today < d <= horizon:
                out.append({"id": f"hol:{d.isoformat()}", "label": name, "date": d.isoformat(),
                            "kind": "holiday", "emoji": emo, "auto": True})
    seen, uniq = set(), []
    for c in sorted(out, key=lambda x: x["date"]):
        if c["label"] in seen:
            continue
        seen.add(c["label"])
        uniq.append(c)
    return uniq


def classify_countdown(title):
    for emo, kind, pat in COUNTDOWN_RULES:
        if re.search(pat, title, re.I):
            return emo, kind
    return None, None


def slugify(s, n=28):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:n] or "event"


def build_countdowns(items, today):
    """One countdown per occasion in the next ~13 months, most imminent first."""
    horizon = today + datetime.timedelta(days=COUNTDOWN_HORIZON_DAYS)
    out, seen = [], set()
    for ev in items:
        title = (ev.get("summary") or "").strip()
        if not title or COUNTDOWN_SKIP.search(title):
            continue
        start = ev.get("start") or {}
        raw = start.get("date") or (start.get("dateTime") or "")[:10]
        if not raw:
            continue
        try:
            d = datetime.date.fromisoformat(raw)
        except ValueError:
            continue
        if d <= today or d > horizon:
            continue
        emo, kind = classify_countdown(title)
        all_day = bool(start.get("date"))
        if emo is None:
            # Not a keyword match. An all-day event more than a few days out is still an
            # occasion (that is how "Bach party" or "Southwest credits" get in when they are
            # named something we did not anticipate); a timed weekday meeting is not.
            if not all_day or (d - today).days < 4:
                continue
            emo, kind = "\U0001F4CC", "event"
        key = (slugify(title), d.isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": f"cal:{d.isoformat()}:{slugify(title)}",
                    "label": title[:48], "date": d.isoformat(),
                    "kind": "deadline" if kind in ("deadline", "signup", "exam") else "countdown",
                    "emoji": emo, "auto": True})
    out.sort(key=lambda c: c["date"])
    return out[:14]


def main():
    if len(sys.argv) < 3:
        print("usage: refresh_calendar.py <service_account_json_path> <passphrase_file> [repo_dir]", file=sys.stderr)
        sys.exit(2)
    key_path, pass_path = sys.argv[1], sys.argv[2]
    repo_dir = sys.argv[3] if len(sys.argv) > 3 else "."
    passphrase = open(pass_path, "rb").read().strip()

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today_ct = now_utc.astimezone(CT).date()
    today_str = today_ct.isoformat()

    time_min = datetime.datetime(today_ct.year, today_ct.month, today_ct.day, tzinfo=CT)
    time_max = time_min + datetime.timedelta(days=WINDOW_DAYS)

    print(f"refresh_calendar: today (CT) = {today_str}, window = {time_min.isoformat()} .. {time_max.isoformat()}")

    creds = get_credentials(key_path)
    all_items = []
    for cal_id in CALENDAR_IDS:
        items = fetch_events(creds, cal_id, time_min, time_max)
        print(f"  {cal_id}: {len(items)} events")
        all_items.extend(items)

    today_list, radar_list = bucket_events(all_items, today_str)
    print(f"  bucketed: today={len(today_list)} radar={len(radar_list)}")

    # A second, much wider sweep purely for the countdown rail at the top of the Home tab.
    # It is deliberately separate from the 15-day dashboard window: birthdays, drill
    # weekends, a bachelor party and expiring travel credits all live months out.
    countdowns = []
    try:
        far_max = time_min + datetime.timedelta(days=COUNTDOWN_HORIZON_DAYS)
        far_items = []
        for cal_id in CALENDAR_IDS:
            far_items.extend(fetch_events(creds, cal_id, time_min, far_max))
        countdowns = build_countdowns(far_items, today_ct)
        holidays = holiday_countdowns(today_ct, today_ct + datetime.timedelta(days=COUNTDOWN_HORIZON_DAYS))
        have = {c["label"].lower() for c in countdowns}
        countdowns += [h for h in holidays if h["label"].lower() not in have][:4]
        countdowns.sort(key=lambda c: c["date"])
        print(f"  countdowns: {len(countdowns)} ({', '.join(c['label'] for c in countdowns[:6])})")
    except Exception as e:  # noqa: BLE001
        print(f"  ! countdowns: {e}", file=sys.stderr)

    calendar_dir = os.path.join(repo_dir, "data", "calendar")
    os.makedirs(calendar_dir, exist_ok=True)
    today_file = os.path.join(calendar_dir, f"{today_str}.json.enc")

    base = None
    if os.path.exists(today_file):
        with open(today_file) as f:
            payload = json.load(f)
        base = json.loads(decrypt_payload(payload, passphrase))
        print(f"  merging into existing published file for {today_str}")
    else:
        # carry forward the most recent prior day's weather/hourly/urgent/chill/close
        # so the site doesn't look blank between midnight and the 9am daily briefing
        manifest_path = os.path.join(repo_dir, "data", "manifest.json")
        prior_date = None
        if os.path.exists(manifest_path):
            with open(manifest_path) as f:
                manifest = json.load(f)
            dates = sorted(d for d in manifest.get("calendar", []) if d < today_str)
            if dates:
                prior_date = dates[-1]
        if prior_date:
            prior_file = os.path.join(calendar_dir, f"{prior_date}.json.enc")
            if os.path.exists(prior_file):
                with open(prior_file) as f:
                    payload = json.load(f)
                base = json.loads(decrypt_payload(payload, passphrase))
                print(f"  no file for {today_str} yet; carrying forward non-calendar fields from {prior_date}")
        if base is None:
            base = {"weather": "", "hourly": [], "urgent": [], "chill": [], "close": None}
            print(f"  no prior file found at all; using blank defaults for non-calendar fields")

    merged = dict(base)
    merged["date"] = today_str
    merged["today"] = today_list
    merged["radar"] = radar_list
    if countdowns:
        merged["countdowns"] = countdowns

    plaintext = json.dumps(merged).encode()
    json.loads(plaintext)  # validate

    enc_payload = encrypt_payload(plaintext, passphrase)
    with open(today_file, "w") as f:
        json.dump(enc_payload, f)
    print(f"  wrote {today_file}")

    # rebuild manifest (same approach as scripts/publish.sh)
    manifest_path = os.path.join(repo_dir, "data", "manifest.json")
    m = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    for kind in ("market", "news", "calendar"):
        d = os.path.join(repo_dir, "data", kind)
        dates = []
        if os.path.isdir(d):
            for fn in os.listdir(d):
                base_name = fn.split(".json")[0]
                if len(base_name) == 10 and base_name.count("-") == 2:
                    dates.append(base_name)
        m[kind] = sorted(set(dates), reverse=True)
    with open(manifest_path, "w") as f:
        json.dump(m, f, indent=1)
    print("  manifest rebuilt")

    run_companions(pass_path, repo_dir)


def run_companions(pass_path, repo_dir):
    """Piggyback the two other server-side refreshes on this run -- it is the one schedule
    GitHub honors reliably for this repo, and keeping them here (rather than as extra
    workflow steps) means no workflow-file edits, which the publishing token can't make.
    Neither may ever fail the calendar refresh: a Yahoo hiccup or a slow crossword build
    just leaves the previous file in place and the site falls back gracefully."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    jobs = [
        ("portfolio quotes", [sys.executable, os.path.join(here, "refresh_quotes.py"), pass_path, repo_dir], 120),
        ("rate board", [sys.executable, os.path.join(here, "refresh_rates.py"), pass_path, repo_dir], 90),
        ("news photos", [sys.executable, os.path.join(here, "enrich_news.py"), pass_path, repo_dir], 240),
        ("crossword", ["node", os.path.join(here, "build_crossword.js"), pass_path, repo_dir], 240),
    ]
    for name, cmd, timeout in jobs:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = (r.stdout or "").strip().splitlines()
            print(f"  {name}: " + (out[-1] if out else f"exit {r.returncode}"))
            if r.returncode != 0 and r.stderr:
                print("   " + r.stderr.strip().splitlines()[-1], file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: skipped ({e})", file=sys.stderr)


if __name__ == "__main__":
    main()
