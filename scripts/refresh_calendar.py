#!/usr/bin/env python3
"""Frequent, computer-independent refresh of the Daily Hub site's `today`/`radar`
calendar fields — runs on a GitHub Actions cron, authenticating as a Google
service account (calendar-refresh-bot@daily-hub-todos.iam.gserviceaccount.com)
that Eli has shared his calendars with. No OAuth popups, no token expiry,
no dependence on Eli's computer or the Cowork app being open (that's the
whole point vs. the retired client-side browser-OAuth approach and vs. a
Cowork scheduled task).

Fetches events from Eli's primary + Family + Brooklyn+Eli calendars, plus the
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
import sys
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
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
        start_dt = datetime.datetime.fromisoformat(s["dateTime"])
        start_ct = start_dt.astimezone(CT)
        weekday = start_ct.strftime("%a")
        md = f"{start_ct.month}/{start_ct.day}"
        time_line = fmt_ct_time(start_dt)
        if e.get("dateTime"):
            end_dt = datetime.datetime.fromisoformat(e["dateTime"])
            end_ct = end_dt.astimezone(CT)
            time_line += "–" + fmt_ct_time(end_dt)
            if end_ct.date() > start_ct.date():
                end_date_str = end_ct.strftime("%Y-%m-%d")
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


# ---------- encryption (matches scripts/encrypt_calendar.py) ----------

def encrypt_payload(data_bytes, passphrase):
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=300_000).derive(passphrase)
    ct = AESGCM(key).encrypt(iv, data_bytes, None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": 300_000,
            "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt_payload(payload, passphrase):
    salt = base64.b64decode(payload["salt"])
    iv = base64.b64decode(payload["iv"])
    ct = base64.b64decode(payload["ct"])
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=payload.get("iter", 300_000)).derive(passphrase)
    return AESGCM(key).decrypt(iv, ct, None)


# ---------- main ----------

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


if __name__ == "__main__":
    main()
