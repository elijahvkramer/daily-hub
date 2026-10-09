#!/usr/bin/env python3
"""Price/yield history for the Home tab's market board -> data/history.json.enc

Why this exists (Oct 2026): clicking a tile on the board opens a trend chart, but that chart was fed
only by browser -> public CORS proxy -> Yahoo, so whenever the proxies were slow or down the click
ended in "Price history isn't available". This file gives the board its own server-side history,
built on GitHub's servers from sources that answer datacenter IPs:

  * U.S. Treasury daily par yield curve (CSV, same feed refresh_rates.py uses)  -> 3-mo, 2-yr, 10-yr, 30-yr
  * Freddie Mac PMMS history (CSV, same feed refresh_rates.py uses)             -> 30-yr and 15-yr mortgage
  * CoinGecko market_chart (same host refresh_rates.py uses)                    -> Bitcoin, Ethereum, gold (PAXG)
  * CNBC chart endpoint (same host refresh_quotes.py uses)                      -> S&P, Nasdaq, Dow, VTI, gold, Brent

Every source is independent and best-effort: a symbol that cannot be fetched is simply absent and the
site falls back to its live path. The file is rebuilt at most every REBUILD_HOURS (it moves once a day,
and every rebuild is a commit), and never fails the calendar refresh that runs it.

Layout: {"updated": iso, "series": {"<site symbol>": {"t": [epoch...], "c": [close...]}}}
Each series is daily for the last ~13 months and weekly before that (out to ~5 years), which is all the
1W/1M/3M/6M/YTD/1Y/5Y windows need and keeps the file small.

Usage: python3 refresh_history.py <passphrase_file> [repo_dir]
"""
import concurrent.futures
import csv
import datetime
import io
import json
import os
import sys
import time

import requests
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_json, encrypt_json, read_passphrase  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
REBUILD_HOURS = 6
DAILY_DAYS = 400          # keep every day this far back, weekly beyond it
MAX_DAYS = 1830           # ~5 years

TREASURY_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
                "&field_tdr_date_value={year}&page&_format=csv")
PMMS_CSV = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
COINGECKO = "https://api.coingecko.com/api/v3/coins/{cid}/market_chart?vs_currency=usd&days=365&interval=daily"
CNBC_CHART = "https://ts-api.cnbc.com/harmony/app/charts/{rng}.json?symbol={sym}"

# site symbol -> Treasury column
TREASURY_COLS = {"^IRX": "3 Mo", "2YY=F": "2 Yr", "^TNX": "10 Yr", "^TYX": "30 Yr"}
# site symbol -> CoinGecko id
COINGECKO_IDS = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum"}
GOLD_FALLBACK = ("GC=F", "pax-gold")        # PAXG tracks spot gold; used only if CNBC has nothing
# site symbol -> CNBC symbol candidates (first that answers wins)
CNBC_SYMS = {"^GSPC": [".SPX"], "^IXIC": [".IXIC"], "^DJI": [".DJI"], "VTI": ["VTI"],
             "GC=F": ["@GC.1"], "BZ=F": ["@LCO.1", "LCO-CMD"]}


def get(session, url, timeout=25):
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def thin(points):
    """points: [(epoch, close)] ascending. Daily for the last DAILY_DAYS, weekly (last obs of each
    ISO week) before that, capped at MAX_DAYS."""
    now = time.time()
    pts = [(t, c) for t, c in points if c is not None and now - t <= MAX_DAYS * 86400]
    cut = now - DAILY_DAYS * 86400
    recent, weeks = [], {}
    for t, c in pts:
        if t >= cut:
            recent.append((t, c))
        else:
            d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isocalendar()
            weeks[(d[0], d[1])] = (t, c)
    old = [weeks[k] for k in sorted(weeks)]
    out = sorted(old + recent)
    return {"t": [int(t) for t, _ in out], "c": [round(c, 4) for _, c in out]}


def utc_noon(date):
    return int(datetime.datetime(date.year, date.month, date.day, 17, tzinfo=datetime.timezone.utc).timestamp())


def treasury_history(session):
    series = {k: [] for k in TREASURY_COLS}
    this_year = datetime.date.today().year
    for year in range(this_year - 5, this_year + 1):
        try:
            text = get(session, TREASURY_CSV.format(year=year)).text
        except Exception as e:  # noqa: BLE001
            print(f"  ! treasury {year}: {str(e)[:120]}", file=sys.stderr)
            continue
        for row in csv.DictReader(io.StringIO(text)):
            try:
                d = datetime.datetime.strptime(row.get("Date", ""), "%m/%d/%Y").date()
            except ValueError:
                continue
            for sym, col in TREASURY_COLS.items():
                raw = (row.get(col) or "").strip()
                if raw:
                    try:
                        series[sym].append((utc_noon(d), float(raw)))
                    except ValueError:
                        pass
    return {s: thin(sorted(p)) for s, p in series.items() if len(p) > 20}


def mortgage_history(session):
    try:
        text = get(session, PMMS_CSV).text
    except Exception as e:  # noqa: BLE001
        print(f"  ! pmms: {str(e)[:120]}", file=sys.stderr)
        return {}
    cols = {"MORT30": "pmms30", "MORT15": "pmms15"}
    series = {k: [] for k in cols}
    for row in csv.DictReader(io.StringIO(text)):
        raw_date = (row.get("date") or row.get("Date") or "").strip()
        d = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                d = datetime.datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue
        if not d:
            continue
        for sym, col in cols.items():
            raw = (row.get(col) or "").strip()
            if raw:
                try:
                    series[sym].append((utc_noon(d), float(raw)))
                except ValueError:
                    pass
    return {s: thin(sorted(p)) for s, p in series.items() if len(p) > 20}


def coingecko_history(session, sym, cid):
    j = get(session, COINGECKO.format(cid=cid)).json()
    pts = [(int(ms) // 1000, v) for ms, v in (j.get("prices") or []) if isinstance(v, (int, float))]
    return thin(sorted(pts)) if len(pts) > 20 else None


def cnbc_bars(session, rng, sym):
    j = get(session, CNBC_CHART.format(rng=rng, sym=requests.utils.quote(sym, safe="")), timeout=20).json()
    bars = (((j or {}).get("barData") or {}).get("priceBars")) or []
    ny = ZoneInfo("America/New_York")
    out = []
    for b in bars:
        try:
            t = datetime.datetime.strptime(str(b["tradeTime"])[:14], "%Y%m%d%H%M%S").replace(tzinfo=ny)
            out.append((int(t.timestamp()), float(str(b["close"]).replace(",", ""))))
        except Exception:  # noqa: BLE001
            continue
    return sorted(out)


def cnbc_history(session, sym, cands):
    for c in cands:
        merged = {}
        for rng in ("5Y", "1Y"):               # 1Y last so its (daily) points win over the 5Y ones
            try:
                for t, v in cnbc_bars(session, rng, c):
                    merged[t] = v
            except Exception as e:  # noqa: BLE001
                print(f"  . cnbc {c} {rng}: {str(e)[:100]}", file=sys.stderr)
        if len(merged) > 20:
            return thin(sorted(merged.items()))
    return None


def main():
    if len(sys.argv) < 2:
        return 0
    passphrase = read_passphrase(sys.argv[1])
    repo = sys.argv[2] if len(sys.argv) > 2 else "."
    out_path = os.path.join(repo, "data", "history.json.enc")

    previous = {}
    if os.path.exists(out_path):
        try:
            previous = decrypt_json(json.load(open(out_path)), passphrase)
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not read previous history ({e}); rebuilding", file=sys.stderr)
    if previous.get("updated"):
        try:
            age = time.time() - datetime.datetime.fromisoformat(previous["updated"]).timestamp()
            if age < REBUILD_HOURS * 3600 and len(previous.get("series", {})) >= 8:
                print(f"history is {age/3600:.1f}h old with {len(previous['series'])} series; not rebuilding")
                return 0
        except Exception:  # noqa: BLE001
            pass

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json, text/csv, */*"})
    series = dict(previous.get("series", {}))        # a failed source keeps its last good series
    fresh = []

    def put(sym, ser, how):
        if ser and len(ser.get("t", [])) > 5:
            series[sym] = ser
            fresh.append(f"{sym}:{how}")

    for sym, ser in treasury_history(session).items():
        put(sym, ser, "treasury")
    for sym, ser in mortgage_history(session).items():
        put(sym, ser, "pmms")

    def cg(item):
        sym, cid = item
        try:
            return sym, coingecko_history(session, sym, cid)
        except Exception as e:  # noqa: BLE001
            print(f"  ! coingecko {cid}: {str(e)[:120]}", file=sys.stderr)
            return sym, None

    for sym, ser in map(cg, COINGECKO_IDS.items()):
        put(sym, ser, "coingecko")
        time.sleep(1.5)                              # CoinGecko's free tier is rate-limited

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(cnbc_history, session, sym, c): sym for sym, c in CNBC_SYMS.items()}
        for f in concurrent.futures.as_completed(futs):
            try:
                put(futs[f], f.result(), "cnbc")
            except Exception as e:  # noqa: BLE001
                print(f"  ! cnbc {futs[f]}: {str(e)[:120]}", file=sys.stderr)

    gsym, gid = GOLD_FALLBACK
    if gsym not in series:
        try:
            put(gsym, coingecko_history(session, gsym, gid), "paxg")
        except Exception as e:  # noqa: BLE001
            print(f"  ! coingecko {gid}: {str(e)[:120]}", file=sys.stderr)

    if not fresh:
        print("no history source answered; leaving the previous file untouched")
        return 0
    payload = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "series": series}
    with open(out_path, "w") as f:
        json.dump(encrypt_json(payload, passphrase), f)
    print(f"wrote {out_path}: {len(series)} series ({', '.join(fresh)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
