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

Layout: {"v": 2, "updated": iso, "series": {"<site symbol>": {"t": [epoch...], "c": [close...]}}}
Oct 2026 fix (Eli: "5Y shows day by day through 2026/2025, then only Sunday by Sunday further back"):
every series is now kept at the source's own resolution (daily) for the whole ~5 years. The old
thin() collapsed everything older than 400 days to one point per ISO week, so the chart's density
changed partway across the window. Bumping "v" makes an older, thinned file rebuild immediately
instead of waiting out REBUILD_HOURS.

A second file, data/history_all.json.enc, carries the longest history each source will give (Treasury
back to 1990, the Freddie Mac survey back to 1971, CNBC's ALL range) for the "All" button. It is only
fetched by the browser when All is clicked, and is rebuilt at most every ALL_REBUILD_HOURS.

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
FORMAT_VERSION = 2        # v1 = daily for 400 days then weekly; v2 = every observation, no thinning
MAX_DAYS = 1830           # ~5 years
ALL_REBUILD_HOURS = 24
ALL_START_YEAR = 1990     # Treasury daily par yield curve data starts in 1990 for these maturities

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


def prepare(points, max_days=MAX_DAYS):
    """points: [(epoch, close)] ascending -> {"t": [...], "c": [...]}.

    Every observation inside max_days is kept as the source gave it: no weekly thinning (that is what
    made old data sparser than recent data). Duplicate timestamps keep the last value; max_days=None
    keeps everything the source returned.
    """
    now = time.time()
    seen = {}
    for t, c in points:
        if c is None:
            continue
        if max_days is not None and now - t > max_days * 86400:
            continue
        seen[int(t)] = c
    out = sorted(seen.items())
    return {"t": [t for t, _ in out], "c": [round(c, 4) for _, c in out]}


def utc_noon(date):
    return int(datetime.datetime(date.year, date.month, date.day, 17, tzinfo=datetime.timezone.utc).timestamp())


def treasury_history(session, max_days=MAX_DAYS):
    series = {k: [] for k in TREASURY_COLS}
    this_year = datetime.date.today().year
    first_year = ALL_START_YEAR if max_days is None else this_year - 5
    years = list(range(first_year, this_year + 1))

    def fetch(year):
        try:
            return year, get(session, TREASURY_CSV.format(year=year)).text
        except Exception as e:  # noqa: BLE001
            print(f"  ! treasury {year}: {str(e)[:120]}", file=sys.stderr)
            return year, None

    # one request per year; in parallel because the All build needs ~37 of them inside a fixed time budget
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        texts = dict(ex.map(fetch, years))
    for year in years:
        text = texts.get(year)
        if not text:
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
    return {s: prepare(sorted(p), max_days) for s, p in series.items() if len(p) > 20}


def mortgage_history(session, max_days=MAX_DAYS):
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
    return {s: prepare(sorted(p), max_days) for s, p in series.items() if len(p) > 20}


def coingecko_history(session, sym, cid):
    j = get(session, COINGECKO.format(cid=cid)).json()
    pts = [(int(ms) // 1000, v) for ms, v in (j.get("prices") or []) if isinstance(v, (int, float))]
    return prepare(sorted(pts)) if len(pts) > 20 else None


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


def cnbc_history(session, sym, cands, max_days=MAX_DAYS):
    """Coarsest range first, finest last, so wherever ranges overlap the finer (daily) bars win.
    max_days=None (the All file) starts from CNBC's ALL range."""
    ranges = ("ALL", "5Y", "1Y") if max_days is None else ("5Y", "1Y")
    for c in cands:
        merged = {}
        for rng in ranges:
            try:
                for t, v in cnbc_bars(session, rng, c):
                    merged[t] = v
            except Exception as e:  # noqa: BLE001
                print(f"  . cnbc {c} {rng}: {str(e)[:100]}", file=sys.stderr)
        if len(merged) > 20:
            return prepare(sorted(merged.items()), max_days)
    return None


def is_fresh(previous, hours, min_series, version=None):
    """True when `previous` was built recently enough, at the current format, with enough series."""
    if not previous.get("updated"):
        return False
    if version is not None and previous.get("v") != version:
        return False
    try:
        age = time.time() - datetime.datetime.fromisoformat(previous["updated"]).timestamp()
    except Exception:  # noqa: BLE001
        return False
    return age < hours * 3600 and len(previous.get("series", {})) >= min_series


def load_previous(path, passphrase):
    if not os.path.exists(path):
        return {}
    try:
        return decrypt_json(json.load(open(path)), passphrase)
    except Exception as e:  # noqa: BLE001
        print(f"  ! could not read previous {os.path.basename(path)} ({e}); rebuilding", file=sys.stderr)
        return {}


def build_series(session, previous_series, max_days, include_crypto=True):
    """One pass over every source. Returns (series, fresh); a source that fails keeps its previous series."""
    series = dict(previous_series)        # a failed source keeps its last good series
    fresh = []

    def put(sym, ser, how):
        if ser and len(ser.get("t", [])) > 5:
            series[sym] = ser
            fresh.append(f"{sym}:{how}")

    for sym, ser in treasury_history(session, max_days).items():
        put(sym, ser, "treasury")
    for sym, ser in mortgage_history(session, max_days).items():
        put(sym, ser, "pmms")

    if include_crypto:                   # CoinGecko's free tier tops out at 365 days, so not for All
        def cg(item):
            sym, cid = item
            try:
                return sym, coingecko_history(session, sym, cid)
            except Exception as e:  # noqa: BLE001
                print(f"  ! coingecko {cid}: {str(e)[:120]}", file=sys.stderr)
                return sym, None

        for sym, ser in map(cg, COINGECKO_IDS.items()):
            put(sym, ser, "coingecko")
            time.sleep(1.5)                          # CoinGecko's free tier is rate-limited

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(cnbc_history, session, sym, c, max_days): sym for sym, c in CNBC_SYMS.items()}
        for f in concurrent.futures.as_completed(futs):
            try:
                put(futs[f], f.result(), "cnbc")
            except Exception as e:  # noqa: BLE001
                print(f"  ! cnbc {futs[f]}: {str(e)[:120]}", file=sys.stderr)

    if include_crypto:
        gsym, gid = GOLD_FALLBACK
        if gsym not in series:
            try:
                put(gsym, coingecko_history(session, gsym, gid), "paxg")
            except Exception as e:  # noqa: BLE001
                print(f"  ! coingecko {gid}: {str(e)[:120]}", file=sys.stderr)
    return series, fresh


def write_payload(path, series, passphrase):
    payload = {"v": FORMAT_VERSION,
               "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "series": series}
    with open(path, "w") as f:
        json.dump(encrypt_json(payload, passphrase), f)


def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json, text/csv, */*"})
    return session


def main():
    if len(sys.argv) < 2:
        return 0
    passphrase = read_passphrase(sys.argv[1])
    repo = sys.argv[2] if len(sys.argv) > 2 else "."
    out_path = os.path.join(repo, "data", "history.json.enc")
    all_path = os.path.join(repo, "data", "history_all.json.enc")

    # ---- the main (~5 year, daily) file ----
    previous = load_previous(out_path, passphrase)
    if is_fresh(previous, REBUILD_HOURS, 8, FORMAT_VERSION):
        age = (time.time() - datetime.datetime.fromisoformat(previous["updated"]).timestamp()) / 3600
        print(f"history is {age:.1f}h old with {len(previous['series'])} series; not rebuilding")
    else:
        session = make_session()
        # An old-format (weekly-thinned) file is not carried forward: a source that fails this time
        # should leave a series absent (the site then falls back to its live path), not keep sparse data.
        carry = previous.get("series", {}) if previous.get("v") == FORMAT_VERSION else {}
        series, fresh = build_series(session, carry, MAX_DAYS)
        if not fresh:
            print("no history source answered; leaving the previous file untouched")
        else:
            write_payload(out_path, series, passphrase)
            print(f"wrote {out_path}: {len(series)} series ({', '.join(fresh)})")

    # ---- the All file (best effort; never blocks the main one) ----
    try:
        prev_all = load_previous(all_path, passphrase)
        if is_fresh(prev_all, ALL_REBUILD_HOURS, 4, FORMAT_VERSION):
            print("all-time history is fresh; not rebuilding")
        else:
            session = make_session()
            carry = prev_all.get("series", {}) if prev_all.get("v") == FORMAT_VERSION else {}
            series, fresh = build_series(session, carry, None, include_crypto=False)
            if fresh:
                write_payload(all_path, series, passphrase)
                print(f"wrote {all_path}: {len(series)} series ({', '.join(fresh)})")
            else:
                print("no all-time source answered; leaving the previous file untouched")
    except Exception as e:  # noqa: BLE001
        print(f"  ! all-time history skipped: {str(e)[:160]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
