#!/usr/bin/env python3
"""Live rate board -> data/rates.json.enc

Feeds the Home tab's rate board (every rate plotted on one shared scale). Sources are public
and key-free:
  * U.S. Treasury daily par yield curve (CSV)            -> 1/3/6-mo, 1/2/5/10/30-yr
  * Freddie Mac Primary Mortgage Market Survey (CSV)     -> 30-yr and 15-yr fixed
  * FRED graph CSV                                       -> Brent/WTI crude, dollar, S&P, Nasdaq, Dow
  * Stooq daily CSV                                      -> VTI
  * gold-api.com / CoinGecko                             -> gold, bitcoin, ether

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
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dh_crypto import decrypt_json, encrypt_json, read_passphrase  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={ids}&cosd={cosd}"
GOLD_API = "https://api.gold-api.com/price/XAU"
COINGECKO = ("https://api.coingecko.com/api/v3/simple/price"
             "?ids=bitcoin,ethereum,pax-gold&vs_currencies=usd&include_24hr_change=true")
# Stooq serves key-free daily CSV history for US-listed tickers. It is the only free,
# datacenter-friendly source for a plain ETF close (FRED has the indexes but no VTI, and
# Yahoo hard-blocks GitHub Actions IPs), so the broad-market ETFs come from here.
STOOQ_CSV = "https://stooq.com/q/d/l/?s={sym}&i=d"
STOOQ_ROWS = [("vti.us", "VTI", "total US market")]

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


def stooq_last_two(sym):
    """(latest_close, prior_close) for a Stooq symbol, or (None, None)."""
    try:
        rows = [r for r in csv.DictReader(io.StringIO(fetch(STOOQ_CSV.format(sym=sym), timeout=15)))
                if (r.get("Close") or "").strip()]
    except Exception as e:  # noqa: BLE001
        print(f"  ! stooq {sym}: {e}", file=sys.stderr)
        return None, None
    if not rows:
        return None, None
    try:
        cur = float(rows[-1]["Close"])
    except (ValueError, KeyError):
        return None, None
    prev = None
    if len(rows) > 1:
        try:
            prev = float(rows[-2]["Close"])
        except (ValueError, KeyError):
            prev = None
    return cur, prev


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
        f = fred_series(["DCOILBRENTEU", "DCOILWTICO", "DTWEXBGS", "SP500", "NASDAQCOM", "DJIA"])
        # Eli, Sep 2026: the board should carry Brent, not WTI. WTI stays as the fallback so
        # the crude row never simply disappears when the Brent series lags a day.
        if "DCOILBRENTEU" in f:
            cur, prev = f["DCOILBRENTEU"]
            prices.append({"label": "Brent Crude", "value": f"${cur:,.2f}", "pct": pct_note(cur, prev),
                           "note": "Brent"})
            got.append("brent")
        elif "DCOILWTICO" in f:
            cur, prev = f["DCOILWTICO"]
            prices.append({"label": "Brent Crude", "value": f"${cur:,.2f}", "pct": pct_note(cur, prev),
                           "note": "WTI proxy"})
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

    # --- broad-market ETFs (VTI) -------------------------------------------------------
    for sym, label, note in STOOQ_ROWS:
        cur, prev = stooq_last_two(sym)
        if cur is not None:
            prices.append({"label": label, "value": f"${cur:,.2f}", "pct": pct_note(cur, prev),
                           "note": note})
            got.append(label.lower())

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
        eth = c.get("ethereum") or {}
        if isinstance(eth.get("usd"), (int, float)):
            prices.append({"label": "Ethereum", "value": f"${eth['usd']:,.0f}",
                           "pct": round(eth.get("usd_24h_change"), 2) if isinstance(eth.get("usd_24h_change"), (int, float)) else None,
                           "note": "24h"})
            got.append("eth")
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


# Last resort for the price rows. FRED's graph CSV and Stooq both refuse GitHub Actions' IP
# range, which is why the board was publishing three prices out of nine all through Sep 2026 --
# S&P, Nasdaq, Dow, VTI, Brent and the dollar printed "awaiting refresh" on every load. The
# browser gets these from Yahoo through a public CORS worker; the same worker answers from here,
# so the published file can carry them too (for first paint, and for any device whose own proxy
# call fails). Entirely best-effort: anything that throws just leaves the row to the live path.
YAHOO_SPARK = ("https://query1.finance.yahoo.com/v7/finance/spark"
               "?symbols={syms}&range=1d&interval=1d")
CORS_WORKER = "https://cors-get-proxy.sirjosh.workers.dev/?url={url}"
# (yahoo symbol, board label, note, format)
YAHOO_ROWS = [
    ("%5EGSPC", "S&P 500", "prior close", "int"),
    ("%5EIXIC", "Nasdaq", "prior close", "int"),
    ("%5EDJI", "Dow", "prior close", "int"),
    ("VTI", "VTI", "total US market", "usd2"),
    ("GC%3DF", "Gold", "spot / oz", "usd0"),
    ("BZ%3DF", "Brent Crude", "Brent", "usd2"),
    ("DX-Y.NYB", "Dollar", "broad index", "num2"),
]


def yahoo_prices(have):
    """Fill in whatever the direct sources didn't answer for. `have` is a set of lowercase labels."""
    wanted = [r for r in YAHOO_ROWS if r[1].lower() not in have]
    if not wanted:
        return [], []
    target = YAHOO_SPARK.format(syms="%2C".join(r[0] for r in wanted))
    url = CORS_WORKER.format(url=urllib.parse.quote(target, safe=""))
    try:
        data = json.loads(fetch(url, timeout=20))
    except Exception as e:  # noqa: BLE001
        print(f"  ! yahoo proxy: {e}", file=sys.stderr)
        return [], []
    results = ((data.get("spark") or {}).get("result")) or []
    by_sym = {}
    for item in results:
        resp = (item.get("response") or [{}])[0]
        meta = resp.get("meta") or {}
        if isinstance(meta.get("regularMarketPrice"), (int, float)):
            by_sym[item.get("symbol")] = (
                meta["regularMarketPrice"],
                meta.get("chartPreviousClose") or meta.get("previousClose"),
            )
    out, got = [], []
    for sym, label, note, fmt in wanted:
        raw = urllib.parse.unquote(sym)
        cur_prev = by_sym.get(raw) or by_sym.get(sym)
        if not cur_prev:
            continue
        cur, prev = cur_prev
        if fmt == "int":
            value = f"{cur:,.0f}"
        elif fmt == "usd0":
            value = f"${cur:,.0f}"
        elif fmt == "usd2":
            value = f"${cur:,.2f}"
        else:
            value = f"{cur:,.2f}"
        out.append({"label": label, "value": value, "pct": pct_note(cur, prev), "note": note})
        got.append("y:" + label.lower())
    return out, got


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

    try:
        prices, pgot = market_prices()
    except Exception as e:  # noqa: BLE001 -- never let one bad source take down the whole run
        print(f"  ! market_prices: {e}", file=sys.stderr)
        prices, pgot = [], []
    got += pgot

    # whatever is still missing, try Yahoo through the CORS worker
    try:
        extra, egot = yahoo_prices({p["label"].lower() for p in prices})
        prices += extra
        got += egot
    except Exception as e:  # noqa: BLE001
        print(f"  ! yahoo fill: {e}", file=sys.stderr)

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
