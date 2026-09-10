#!/usr/bin/env python3
"""Live rate board -> data/rates.json.enc

Feeds the Home tab's rate board (every rate plotted on one shared scale). Sources are public
and key-free:
  * U.S. Treasury daily par yield curve (CSV)            -> 1/3/6-mo, 1/2/5/10/30-yr
  * Freddie Mac Primary Mortgage Market Survey (CSV)     -> 30-yr and 15-yr fixed

Runs as a companion of the Calendar Refresh workflow, so the board is at most ~30 minutes
stale instead of once-a-morning. Never fails the job; on a bad fetch the previous file stands.

Usage: python3 refresh_rates.py <passphrase_file> [repo_dir]
"""
import csv
import datetime
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_json, encrypt_json, read_passphrase  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

TREASURY_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
                "&field_tdr_date_value={year}&page&_format=csv")
PMMS_CSV = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"

# (published column, board label, group hint)
TREASURY_ROWS = [
    ("3 Mo", "3-mo T-bill"), ("6 Mo", "6-mo T-bill"), ("1 Yr", "1-yr Treasury"),
    ("2 Yr", "2-yr Treasury"), ("5 Yr", "5-yr Treasury"), ("10 Yr", "10-yr Treasury"),
    ("30 Yr", "30-yr Treasury"),
]


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def treasury_rates():
    """Latest and prior row of the daily par yield curve, so we can show the day's move."""
    year = datetime.date.today().year
    text = fetch(TREASURY_CSV.format(year=year))
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return [], None
    def key(r):
        try:
            return datetime.datetime.strptime(r.get("Date", ""), "%m/%d/%Y")
        except Exception:  # noqa: BLE001
            return datetime.datetime.min
    rows.sort(key=key)
    latest, prior = rows[-1], (rows[-2] if len(rows) > 1 else None)
    out = []
    for col, label in TREASURY_ROWS:
        raw = (latest.get(col) or "").strip()
        if not raw:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        note = ""
        if prior and (prior.get(col) or "").strip():
            try:
                bp = round((val - float(prior[col])) * 100)
                note = f"{'+' if bp > 0 else ''}{bp} bp" if bp else "flat"
            except ValueError:
                note = ""
        out.append({"label": label, "value": f"{val:.2f}%", "note": note})
    return out, latest.get("Date")


def mortgage_rates():
    text = fetch(PMMS_CSV)
    rows = [r for r in csv.DictReader(io.StringIO(text)) if (r.get("pmms30") or "").strip()]
    if not rows:
        return [], None
    latest = rows[-1]
    prior = rows[-2] if len(rows) > 1 else None
    out = []
    for col, label in (("pmms30", "30-yr mortgage"), ("pmms15", "15-yr mortgage")):
        raw = (latest.get(col) or "").strip()
        if not raw:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        note = ""
        if prior and (prior.get(col) or "").strip():
            try:
                d = val - float(prior[col])
                note = f"{'+' if d > 0 else ''}{d:.2f} w/w" if abs(d) >= 0.005 else "flat w/w"
            except ValueError:
                note = ""
        out.append({"label": label, "value": f"{val:.2f}%", "note": note})
    return out, (latest.get("date") or latest.get("Date"))


def main():
    if len(sys.argv) < 2:
        return 0
    passphrase = read_passphrase(sys.argv[1])
    repo = sys.argv[2] if len(sys.argv) > 2 else "."
    out_path = os.path.join(repo, "data", "rates.json.enc")

    previous = None
    if os.path.exists(out_path):
        try:
            previous = decrypt_json(json.load(open(out_path)), passphrase)
        except Exception:  # noqa: BLE001
            previous = None

    rates, notes = [], []
    try:
        t, tdate = treasury_rates()
        rates += t
        if tdate:
            notes.append(f"Treasury {tdate}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! treasury: {e}", file=sys.stderr)
    try:
        m, mdate = mortgage_rates()
        rates += m
        if mdate:
            notes.append(f"Freddie Mac {mdate}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! pmms: {e}", file=sys.stderr)

    if not rates:
        print("no rate source answered; leaving the previous rate file untouched")
        return 0

    # Carry through anything the morning brief published that we don't source ourselves
    # (Fed funds target, money-market yield, credit spreads).
    if previous:
        have = {r["label"].lower() for r in rates}
        for r in previous.get("rates", []):
            if r.get("label", "").lower() not in have and r.get("manual"):
                rates.append(r)

    payload = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "sources": " · ".join(notes), "rates": rates}
    if previous and previous.get("rates") == rates:
        print(f"rates unchanged ({len(rates)} rows); not rewriting")
        return 0
    with open(out_path, "w") as f:
        json.dump(encrypt_json(payload, passphrase), f)
    print(f"wrote {out_path}: {len(rates)} rates ({' · '.join(notes)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
