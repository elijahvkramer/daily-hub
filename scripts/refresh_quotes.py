#!/usr/bin/env python3
"""Server-side price snapshot for the Portfolio: data/quotes.json.enc

Why this exists (Sep 2026): the Portfolio tab and the Home snapshot pull live prices from
Yahoo Finance through free, shared CORS proxies -- and those proxies hang or rate-limit
exactly when everyone else is checking their portfolio, i.e. right at the close. The site
had no fallback except the stale Fidelity export, so a proxy outage meant either a long
"fetching live prices…" stall or numbers from weeks ago.

This runs inside the Calendar Refresh workflow (GitHub's own servers, no proxy in the
way), pulls every holding's quote + today's 5-minute session series straight from Yahoo,
and publishes it encrypted. The browser now loads THIS first (one fast same-CDN fetch
that always works), paints immediately, and only then tries the live proxies to get
fresher-than-the-last-tick numbers. After the close it is exactly the closing print.

Usage: python3 refresh_quotes.py <passphrase_file> [repo_dir]
Exit code is always 0 -- a Yahoo outage must never fail the calendar refresh job.
"""
import concurrent.futures
import datetime
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_json, encrypt_json, read_passphrase  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]


def fetch_chart(sym, session):
    """Yahoo v8 chart: 1 day of 5-minute bars + the quote meta. Returns None on failure."""
    for host in HOSTS:
        url = f"{host}/v8/finance/chart/{requests.utils.quote(sym, safe='')}"
        try:
            r = session.get(url, params={"interval": "5m", "range": "1d", "includePrePost": "false"},
                            timeout=12)
            if r.status_code != 200:
                continue
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                continue
            res = res[0]
            meta = res.get("meta") or {}
            price = meta.get("regularMarketPrice")
            if not isinstance(price, (int, float)):
                continue
            prev = meta.get("chartPreviousClose", meta.get("previousClose", price))
            reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
            out = {"price": price, "prev": prev, "time": meta.get("regularMarketTime")}
            ts = res.get("timestamp")
            closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close")
            if ts and closes and len(ts) == len(closes):
                # round to 4dp and keep nulls (the browser forward-fills gaps)
                c = [round(x, 4) if isinstance(x, (int, float)) else None for x in closes]
                out["series"] = {"t": ts, "c": c, "start": reg.get("start"), "end": reg.get("end")}
            return out
        except Exception as e:  # noqa: BLE001
            print(f"  ! {sym} via {host}: {e}", file=sys.stderr)
            continue
        else:
            print(f"  ! {sym} via {host}: HTTP {r.status_code}", file=sys.stderr)
    return None


def stooq_symbol(sym):
    if "-" in sym:            # crypto (BTC-USD): not on Stooq; the browser's live path covers it
        return None
    return sym.lower() + ".us"


def fetch_stooq(syms, session):
    """Fallback when Yahoo refuses the runner: Stooq's delayed quote + daily history (no key).
    Gives price and previous close; no intraday series."""
    out = {}
    pairs = [(s, stooq_symbol(s)) for s in syms if stooq_symbol(s)]
    if not pairs:
        return out
    try:
        r = session.get("https://stooq.com/q/l/", params={"s": ",".join(p[1] for p in pairs), "f": "sd2t2ohlcv", "h": "", "e": "csv"}, timeout=15)
        r.raise_for_status()
        rows = [ln.split(",") for ln in r.text.strip().splitlines()[1:]]
        quotes = {row[0].lower(): row for row in rows if len(row) >= 7}
    except Exception as e:  # noqa: BLE001
        print(f"  ! stooq quote list: {e}", file=sys.stderr)
        return out
    for sym, st in pairs:
        row = quotes.get(st)
        if not row or row[6] in ("N/D", ""):
            continue
        try:
            price = float(row[6])
            qdate = row[1]  # YYYY-MM-DD
            prev = None
            try:
                h = session.get("https://stooq.com/q/d/l/", params={"s": st, "i": "d"}, timeout=15)
                lines = [ln.split(",") for ln in h.text.strip().splitlines()[1:] if ln.strip()]
                closes = [(ln[0], float(ln[4])) for ln in lines[-3:] if len(ln) >= 5 and ln[4] not in ("", "N/D")]
                if closes:
                    prev = closes[-2][1] if closes[-1][0] == qdate and len(closes) > 1 else closes[-1][1]
            except Exception:  # noqa: BLE001
                prev = None
            # stamp the quote at 4pm New York on its date so the site's "is this today's move" logic works
            ts = int(datetime.datetime.strptime(qdate + " 16:00", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/New_York")).timestamp())
            out[sym] = {"price": price, "prev": prev if prev is not None else price, "time": ts, "src": "stooq"}
        except Exception as e:  # noqa: BLE001
            print(f"  ! stooq {sym}: {e}", file=sys.stderr)
    return out


def fetch_cnbc(syms, session):
    """Third source: CNBC's public quote endpoint. Yahoo blocks datacenter IPs and Stooq has
    also come back empty from the runner, so this is the one that has to carry it. Shape is
    parsed defensively -- any field that isn't there is simply skipped."""
    out = {}
    if not syms:
        return out
    tickers = [s for s in syms if "-" not in s]
    if not tickers:
        return out
    url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
           "?symbols=" + "%7C".join(tickers) +
           "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json&events=1")
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  ! cnbc: {e}", file=sys.stderr)
        return out
    quotes = (((payload or {}).get("FormattedQuoteResult") or {}).get("FormattedQuote")) or []
    if isinstance(quotes, dict):
        quotes = [quotes]
    for q in quotes:
        try:
            sym = (q.get("symbol") or q.get("issue_id") or "").upper()
            last = q.get("last") or q.get("lastPrice")
            prev = q.get("previous_day_closing") or q.get("previousDayClosing") or last
            if not sym or last is None:
                continue
            price = float(str(last).replace(",", ""))
            prevf = float(str(prev).replace(",", ""))
            ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            out[sym] = {"price": price, "prev": prevf, "time": ts, "src": "cnbc"}
        except Exception:  # noqa: BLE001
            continue
    return out


NY = ZoneInfo("America/New_York")
PROXIES = [
    "https://api.allorigins.win/raw?url=",
    "https://cors-get-proxy.sirjosh.workers.dev/?url=",
    "https://api.codetabs.com/v1/proxy?quest=",
]


def series_from_bars(bars):
    """bars: [(epoch_seconds, close)] -> the site's intraday series: 5-minute buckets inside the
    regular session of the newest bar's day, plus that session's start/end (epoch)."""
    buckets = {}
    for ts, c in bars:
        if isinstance(c, (int, float)):
            buckets[int(ts) // 300 * 300] = float(c)
    if len(buckets) < 5:
        return None
    last = max(buckets)
    day = datetime.datetime.fromtimestamp(last, NY).date()
    start = int(datetime.datetime.combine(day, datetime.time(9, 30), NY).timestamp())
    end = int(datetime.datetime.combine(day, datetime.time(16, 0), NY).timestamp())
    keep = [t for t in sorted(buckets) if start <= t <= end]
    if len(keep) < 5:
        return None
    return {"t": keep, "c": [round(buckets[t], 4) for t in keep], "start": start, "end": end}


def series_cnbc(sym, session):
    """CNBC's chart endpoint (1-minute bars for the current/last session)."""
    url = f"https://ts-api.cnbc.com/harmony/app/charts/1D.json?symbol={requests.utils.quote(sym, safe='')}"
    r = session.get(url, timeout=10)
    r.raise_for_status()
    bars = (((r.json() or {}).get("barData") or {}).get("priceBars")) or []
    out = []
    for b in bars:
        try:
            t = datetime.datetime.strptime(str(b["tradeTime"])[:14], "%Y%m%d%H%M%S").replace(tzinfo=NY)
            out.append((int(t.timestamp()), float(str(b["close"]).replace(",", ""))))
        except Exception:  # noqa: BLE001
            continue
    return series_from_bars(out)


def series_yahoo_via_proxy(sym, session):
    """Yahoo's 5-minute chart, relayed through the same free proxies the browser uses."""
    target = (f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym, safe='')}"
              f"?interval=5m&range=1d&includePrePost=false&_ts={int(time.time())}")
    last_err = None
    for px in PROXIES:
        try:
            r = session.get(px + requests.utils.quote(target, safe=""), timeout=10)
            r.raise_for_status()
            res = ((r.json().get("chart") or {}).get("result") or [None])[0]
            ts = res.get("timestamp")
            closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close")
            if ts and closes:
                return series_from_bars(list(zip(ts, closes)))
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"proxies failed ({last_err})")


def attach_series(quotes, syms, session):
    """The CNBC/Stooq fallbacks give a price but no intraday bars, and the Portfolio chart needs
    bars. For every equity symbol without a fresh series, try to fetch one (CNBC chart first, then
    Yahoo through proxies). Never fails the run; the site draws a clean fallback with no bars."""
    now = time.time()

    def fresh(q):
        ser = (q or {}).get("series")
        return bool(ser and ser.get("t") and ser["t"][-1] > now - 6 * 3600)

    todo = [s for s in syms if "-" not in s and not fresh(quotes.get(s))]
    got = 0

    def work(sym):
        for name, fn in (("cnbc", series_cnbc), ("yahoo-proxy", series_yahoo_via_proxy)):
            try:
                ser = fn(sym, session)
                if ser:
                    return sym, name, ser
                print(f"  . series {sym} via {name}: no usable bars", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"  ! series {sym} via {name}: {str(e)[:160]}", file=sys.stderr)
        return sym, None, None

    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for sym, name, ser in ex.map(work, todo):
                if ser:
                    quotes[sym]["series"] = ser
                    got += 1
                    print(f"  + series {sym} via {name}: {len(ser['t'])} bars", file=sys.stderr)
    # a series older than ~30h is yesterday's news; the site would use it as its time axis
    for s, q in list(quotes.items()):
        ser = q.get("series")
        if ser and ser.get("t") and ser["t"][-1] < now - 30 * 3600:
            q.pop("series", None)
    return got


def write_status(repo, **kv):
    """Plaintext, symbol-free status so a run can be diagnosed from the repo alone."""
    try:
        with open(os.path.join(repo, "data", "quotes.status.json"), "w") as f:
            json.dump({"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **kv}, f, indent=1)
    except Exception:  # noqa: BLE001
        pass


def main():
    if len(sys.argv) < 2:
        print("usage: refresh_quotes.py <passphrase_file> [repo_dir]", file=sys.stderr)
        return 0
    passphrase = read_passphrase(sys.argv[1])
    repo = sys.argv[2] if len(sys.argv) > 2 else "."
    hold_path = os.path.join(repo, "data", "holdings.json.enc")
    out_path = os.path.join(repo, "data", "quotes.json.enc")
    if not os.path.exists(hold_path):
        print("no holdings file; nothing to do")
        return 0

    holdings = decrypt_json(json.load(open(hold_path)), passphrase)
    syms = []
    for p in holdings.get("positions", []):
        if p.get("type") == "cash":
            continue
        s = p.get("fetchSymbol") or p.get("ticker")
        if s and s not in syms:
            syms.append(s)

    previous = {}
    if os.path.exists(out_path):
        try:
            previous = decrypt_json(json.load(open(out_path)), passphrase).get("quotes", {})
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not read the previous quotes file ({e}); starting fresh", file=sys.stderr)

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json"})
    quotes, fresh, errors = dict(previous), 0, []
    for s in syms:
        q = fetch_chart(s, session)
        if q:
            quotes[s] = q
            fresh += 1
        time.sleep(0.25)  # be polite; 12 symbols is well under any limit
    source = "yahoo"
    if not fresh:
        # Yahoo refused every call (it rate-limits datacenter IPs on and off)
        for name, fn in (("stooq", fetch_stooq), ("cnbc", fetch_cnbc)):
            got = fn(syms, session)
            for s, q in got.items():
                if "series" not in q and (previous.get(s) or {}).get("series"):
                    q["series"] = previous[s]["series"]     # keep the last good bars until new ones land
                quotes[s] = q
                fresh += 1
            if fresh:
                source = name
                break

    if not fresh:
        print("Yahoo and Stooq returned nothing for any symbol; leaving the previous quotes file untouched")
        write_status(repo, ok=False, source=None, refreshed=0, symbols=len(syms), note="no source answered")
        return 0

    nseries = 0
    try:
        nseries = attach_series(quotes, syms, session)
    except Exception as e:  # noqa: BLE001
        print(f"  ! attach_series: {e}", file=sys.stderr)

    # Skip the commit when nothing actually moved (weekends, overnight): a re-encrypt with a
    # new IV would otherwise look like a change every single tick.
    if quotes == previous:
        print(f"quotes unchanged for all {len(syms)} symbols; not rewriting")
        write_status(repo, ok=True, source=source, refreshed=fresh, symbols=len(syms), series=nseries, note="unchanged")
        return 0
    write_status(repo, ok=True, source=source, refreshed=fresh, symbols=len(syms), series=nseries, note="written")

    payload = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "quotes": quotes,
    }
    with open(out_path, "w") as f:
        json.dump(encrypt_json(payload, passphrase), f)
    print(f"wrote {out_path}: {fresh}/{len(syms)} symbols refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
