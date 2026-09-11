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

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={ids}&cosd={cosd}"
GOLD_API = "https://api.gold-api.com/price/XAU"
COINGECKO = ("https://api.coingecko.com/api/v3/simple/price"
             "?ids=bitcoin,pax-gold&vs_currencies=usd&include_24hr_change=true")

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
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv, application/json, */*"})
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


def fred_series(ids):
    """FRED's graph CSV serves several daily series in one key-free request. Returns
    {id: (latest_value, prior_value)} for whatever actually had numbers."""
    cosd = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    text = fetch(FRED_CSV.format(ids=",".join(ids), cosd=cosd))
    rows = list(csv.DictReader(io.StringIO(text)))
    out = {}
    for sid in ids:
        vals = []
        for r in rows:
            raw = (r.get(sid) or "").strip()
            if raw in ("", "."):
                continue
            try:
                vals.append(float(raw))
            except ValueError:
                continue
        if vals:
            out[sid] = (vals[-1], vals[-2] if len(vals) > 1 else None)
    return out


def pct_note(cur, prev):
    if prev in (None, 0):
        return None
    return round((cur - prev) / prev * 100, 2)


def market_prices():
    """The handful of prices Eli actually wants on the board: gold, crude, bitcoin, the
    dollar. Every source is independent and key-free, and each one is allowed to fail on its
    own -- the board simply shows the rows that answered.

    Note on gold: api.gold-api.com is the real spot quote. PAX Gold (a token redeemable for
    one troy ounce, so it tracks spot within a fraction of a percent) is the backstop, because
    CoinGecko answers from datacenter IPs when almost nothing else in commodities does."""
    prices, got = [], []

    # --- FRED: WTI crude, the broad dollar index, and the three headline equity indexes.
    # Eli, Sep 2026: wanted "a couple more prices, maybe broad indexes" on the Running
    # Outlook board -- FRED publishes daily closes for all three (a day behind live, same
    # as the rest of this board), so no separate market-data key is needed. -------------
    try:
        f = fred_series(["DCOILWTICO", "DTWEXBGS", "SP500", "NASDAQCOM", "DJIA"])
        if "DCOILWTICO" in f:
            cur, prev = f["DCOILWTICO"]
            prices.append({"label": "Crude", "value": f"${cur:,.2f}", "pct": pct_note(cur, prev),
                           "note": "WTI"})
            got.append("wti")
        if "DTWEXBGS" in f:
            cur, prev = f["DTWEXBGS"]
            prices.append({"label": "Dollar", "value": f"{cur:,.1f}", "pct": pct_note(cur, prev),
                           "note": "broad index"})
            got.append("dxy")
        if "SP500" in f:
            cur, prev = f["SP500"]
            prices.append({"label": "S&P 500", "value": f"{cur:,.0f}", "pct": pct_note(cur, prev),
                           "note": "prior close"})
            got.append("spx")
        if "NASDAQCOM" in f:
            cur, prev = f["NASDAQCOM"]
            prices.append({"label": "Nasdaq", "value": f"{cur:,.0f}", "pct": pct_note(cur, prev),
                           "note": "prior close"})
            got.append("nasdaq")
        if "DJIA" in f:
            cur, prev = f["DJIA"]
            prices.append({"label": "Dow", "value": f"{cur:,.0f}", "pct": pct_note(cur, prev),
                           "note": "prior close"})
            got.append("dow")
    except Exception as e:  # noqa: BLE001
        print(f"  ! fred: {e}", file=sys.stderr)

    # --- gold ---------------------------------------------------------------------------
    gold = None
    try:
        g = json.loads(fetch(GOLD_API, timeout=12))
        v = g.get("price")
        if isinstance(v, (int, float)) and v > 100:
            gold = {"label": "Gold", "value": f"${v:,.0f}", "note": "spot / oz"}
            got.append("gold")
    except Exception as e:  # noqa: BLE001
        print(f"  ! gold-api: {e}", file=sys.stderr)

    # --- bitcoin (and gold's backstop) ---------------------------------------------------
    try:
        c = json.loads(fetch(COINGECKO, timeout=12))
        btc = c.get("bitcoin") or {}
        if isinstance(btc.get("usd"), (int, float)):
            prices.append({"label": "Bitcoin", "value": f"${btc['usd']:,.0f}",
                           "pct": round(btc.get("usd_24h_change"), 2) if isinstance(btc.get("usd_24h_change"), (int, float)) else None,
                           "note": "24h"})
            got.append("btc")
        pax = c.get("pax-gold") or {}
        if gold is None and isinstance(pax.get("usd"), (int, float)):
            gold = {"label": "Gold", "value": f"${pax['usd']:,.0f}", "note": "PAXG / oz",
                    "pct": round(pax.get("usd_24h_change"), 2) if isinstance(pax.get("usd_24h_change"), (int, float)) else None}
            got.append("gold:paxg")
    except Exception as e:  # noqa: BLE001
        print(f"  ! coingecko: {e}", file=sys.stderr)
    if gold:
        prices.insert(0, gold)
    return prices, got


def fed_funds():
    """Effective fed funds, straight from the New York Fed. Falls back to FRED's DFF."""
    try:
        j = json.loads(fetch("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json", timeout=12))
        r = (j.get("refRates") or [])[0]
        v = r.get("percentRate")
        if isinstance(v, (int, float)):
            return {"label": "Fed funds", "value": f"{v:.2f}%", "note": "effective"}
    except Exception as e:  # noqa: BLE001
        print(f"  ! nyfed effr: {e}", file=sys.stderr)
    try:
        f = fred_series(["DFF"])
        if "DFF" in f:
            return {"label": "Fed funds", "value": f"{f['DFF'][0]:.2f}%", "note": "effective"}
    except Exception as e:  # noqa: BLE001
        print(f"  ! fred DFF: {e}", file=sys.stderr)
    return None


def write_status(repo, **kv):
    try:
        with open(os.path.join(repo, "data", "rates.status.json"), "w") as f:
            json.dump({"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **kv}, f, indent=1)
    except Exception:  # noqa: BLE001
        pass


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

    rates, notes, got = [], [], []
    ff = fed_funds()
    if ff:
        rates.append(ff)
        got.append("effr")
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

    prices, pgot = market_prices()
    got += pgot

    if not rates and not prices:
        print("no rate source answered; leaving the previous rate file untouched")
        write_status(repo, ok=False, sources=[], rows=0, prices=0)
        return 0
    if not rates and previous:
        rates = previous.get("rates", [])
    if not prices and previous:
        prices = previous.get("prices", [])

    # Carry through anything the morning brief published that we don't source ourselves
    # (Fed funds target, money-market yield, credit spreads).
    if previous:
        have = {r["label"].lower() for r in rates}
        for r in previous.get("rates", []):
            if r.get("label", "").lower() not in have and r.get("manual"):
                rates.append(r)

    payload = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "sources": " · ".join(notes), "rates": rates, "prices": prices}
    write_status(repo, ok=True, sources=got, rows=len(rates), prices=len(prices))
    if previous and previous.get("rates") == rates and previous.get("prices") == prices:
        print(f"rates unchanged ({len(rates)} rows); not rewriting")
        return 0
    with open(out_path, "w") as f:
        json.dump(encrypt_json(payload, passphrase), f)
    print(f"wrote {out_path}: {len(rates)} rates, {len(prices)} prices ({' · '.join(notes)}; sources: {','.join(got)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
