#!/usr/bin/env python3
"""Tests for scripts/refresh_history.py: the chart data must keep one consistent resolution.

Run: python3 scripts/test_history.py

Regression guard for the Oct 2026 report that the 5Y chart showed every day for the last year and only
one point per week before that. These tests use synthetic data -- no network.
"""
import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import refresh_history as rh  # noqa: E402

DAY = 86400


def daily(n_days, step=1, end=None):
    end = end or time.time()
    return [(int(end - (n_days - i) * DAY), 100.0 + i) for i in range(0, n_days + 1, step)]


class PrepareTests(unittest.TestCase):
    def test_keeps_every_daily_point_across_five_years(self):
        pts = daily(1800)
        out = rh.prepare(pts)
        self.assertEqual(len(out["t"]), len(pts))
        gaps = {b - a for a, b in zip(out["t"], out["t"][1:])}
        self.assertEqual(gaps, {DAY}, "old data must not be sparser than recent data")

    def test_no_weekly_collapse_beyond_400_days(self):
        out = rh.prepare(daily(1000))
        old = [t for t in out["t"] if time.time() - t > 500 * DAY]
        self.assertGreater(len(old), 400)       # the old code left ~70 weekly points here

    def test_caps_to_max_days_but_none_keeps_all(self):
        pts = daily(9000)
        self.assertLessEqual(len(rh.prepare(pts)["t"]), 1832)
        self.assertEqual(len(rh.prepare(pts, None)["t"]), len(pts))

    def test_drops_nulls_dedupes_and_sorts(self):
        now = int(time.time())
        out = rh.prepare([(now, 2.0), (now - DAY, None), (now - 2 * DAY, 1.0), (now, 3.0)])
        self.assertEqual(out["t"], [now - 2 * DAY, now])
        self.assertEqual(out["c"], [1.0, 3.0])


class FreshnessTests(unittest.TestCase):
    def prev(self, age_h, version, n=10):
        t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=age_h)
        return {"v": version, "updated": t.isoformat(timespec="seconds"), "series": {str(i): {} for i in range(n)}}

    def test_old_format_rebuilds_immediately(self):
        self.assertFalse(rh.is_fresh(self.prev(0.1, None), 6, 8, rh.FORMAT_VERSION))
        self.assertFalse(rh.is_fresh(self.prev(0.1, 1), 6, 8, rh.FORMAT_VERSION))

    def test_current_format_respects_age_and_series_count(self):
        self.assertTrue(rh.is_fresh(self.prev(1, rh.FORMAT_VERSION), 6, 8, rh.FORMAT_VERSION))
        self.assertFalse(rh.is_fresh(self.prev(7, rh.FORMAT_VERSION), 6, 8, rh.FORMAT_VERSION))
        self.assertFalse(rh.is_fresh(self.prev(1, rh.FORMAT_VERSION, n=3), 6, 8, rh.FORMAT_VERSION))
        self.assertFalse(rh.is_fresh({}, 6, 8, rh.FORMAT_VERSION))


class FakeBitstamp:
    """Serves Bitstamp's real response shape, honouring `start` and the 1000-candle page limit."""
    def __init__(self, first_ts, n_days):
        self.rows = [(first_ts + i * DAY, 100.0 + i) for i in range(n_days)]
        self.calls = 0

    def __call__(self, session, url, timeout=25):
        self.calls += 1
        start = int(url.split("start=")[1])
        page = [r for r in self.rows if r[0] >= start][:1000]
        resp = mock.Mock()
        resp.json.return_value = {"data": {"pair": "BTC/USD", "ohlc": [
            {"timestamp": str(t), "open": "1", "high": "1", "low": "1", "close": f"{c:.2f}", "volume": "1"} for t, c in page]}}
        return resp


class CryptoTests(unittest.TestCase):
    def fake(self, n_days):
        today = int(time.time()) // DAY * DAY
        return FakeBitstamp(today - (n_days - 1) * DAY, n_days)

    def test_pages_through_the_whole_history(self):
        fb = self.fake(4200)
        with mock.patch.object(rh, "get", fb):
            out = rh.bitstamp_history(None, "btcusd", "2011-09-01", None)
        self.assertEqual(len(out["t"]), 4200)
        self.assertEqual({b - a for a, b in zip(out["t"], out["t"][1:])}, {DAY})
        self.assertEqual(fb.calls, 5)                                 # 4 full pages + 1 partial

    def test_five_year_window_is_really_five_years(self):
        with mock.patch.object(rh, "get", self.fake(4200)):
            out = rh.bitstamp_history(None, "btcusd", "2011-09-01", rh.MAX_DAYS)
        self.assertGreater(len(out["t"]), 1820, "BTC's 5Y must be five years (CoinGecko gave one)")
        self.assertLessEqual(len(out["t"]), 1832)

    def test_build_series_btc_and_eth_cover_five_years_and_all(self):
        with mock.patch.object(rh, "get", self.fake(4200)), \
             mock.patch.object(rh, "treasury_history", return_value={}), \
             mock.patch.object(rh, "mortgage_history", return_value={}), \
             mock.patch.object(rh, "cnbc_history", return_value=None):
            main, fresh = rh.build_series(None, {}, rh.MAX_DAYS)
            allf, _ = rh.build_series(None, {}, None)
        self.assertGreater(len(main["BTC-USD"]["t"]), 1820)
        self.assertGreater(len(allf["BTC-USD"]["t"]), 4000)
        self.assertIn("ETH-USD", allf)
        self.assertIn("BTC-USD:bitstamp", fresh)

    def test_falls_back_to_coingecko_only_for_the_main_file(self):
        cg = {"prices": [[(int(time.time()) - (400 - i) * DAY) * 1000, 100.0 + i] for i in range(401)]}
        cgr = mock.Mock(); cgr.json.return_value = cg

        def fake_get(session, url, timeout=25):
            if "bitstamp" in url:
                raise RuntimeError("down")
            return cgr
        with mock.patch.object(rh, "get", fake_get), mock.patch.object(rh.time, "sleep", lambda s: None), \
             mock.patch.object(rh, "treasury_history", return_value={}), mock.patch.object(rh, "mortgage_history", return_value={}), \
             mock.patch.object(rh, "cnbc_history", return_value=None):
            main, _ = rh.build_series(None, {}, rh.MAX_DAYS)
            allf, _ = rh.build_series(None, {}, None)
        self.assertIn("BTC-USD", main)          # one year is better than nothing for the main file
        self.assertNotIn("BTC-USD", allf)       # but a one-year series must never pose as "all time"

    def test_bad_rows_are_skipped_not_fatal(self):
        resp = mock.Mock()
        resp.json.return_value = {"data": {"ohlc": [{"timestamp": "x", "close": "1"}, {"close": "2"}, {"timestamp": "1700000000", "close": "n/a"}]}}
        with mock.patch.object(rh, "get", return_value=resp):
            self.assertIsNone(rh.bitstamp_history(None, "btcusd", "2020-01-01", None))


class CnbcTests(unittest.TestCase):
    """Fed with the shapes CNBC really returns (measured live, Oct 16 2026): "6M" ~3y of daily bars, "1Y" ~2y
    daily, "10Y"/"5Y" weekly bars stamped on the SUNDAY that starts the week, "ALL" quarterly bars stamped on
    the quarter's first day. The old test put weekly and daily bars on identical timestamps, so it passed while
    production shipped a phantom Sunday point every week."""
    NY = rh.NY

    def stamp(self, d):
        return int(datetime.datetime(d.year, d.month, d.day, tzinfo=self.NY).timestamp())

    def fake(self):
        today = datetime.date.today()
        weekdays = lambda days: [today - datetime.timedelta(days=i) for i in range(days, 0, -1)
                                 if (today - datetime.timedelta(days=i)).weekday() < 5]
        close = lambda d: 100 + d.toordinal() % 97          # deterministic "price" per date
        daily3 = [(self.stamp(d), close(d)) for d in weekdays(3 * 365)]
        daily2 = [(self.stamp(d), close(d)) for d in weekdays(2 * 365)]
        sunday = today - datetime.timedelta(days=(today.weekday() + 1) % 7)
        weekly = []
        for k in range(520, -1, -1):
            sun = sunday - datetime.timedelta(weeks=k)
            fri = sun + datetime.timedelta(days=5)
            weekly.append((self.stamp(sun), close(min(fri, today))))
        quarterly = []
        for y in range(1993, today.year + 1):
            for m in (1, 4, 7, 10):
                q = datetime.date(y, m, 1)
                if q <= today:
                    quarterly.append((self.stamp(q), 50.0))
        data = {"6M": daily3, "1Y": daily2, "10Y": weekly, "5Y": weekly, "ALL": quarterly}
        return lambda session, rng, sym: sorted(data.get(rng, [])), close

    def check(self, out):
        self.assertIsNone(rh.validate_series("X", out))
        days = [rh.et_date(t) for t in out["t"]]
        self.assertFalse([d for d in days if d.weekday() >= 5], "no weekend bars, ever")
        return days

    def test_main_file_is_daily_where_daily_exists_and_never_interleaves(self):
        bars, close = self.fake()
        with mock.patch.object(rh, "cnbc_bars", bars):
            out = rh.cnbc_history(None, "SPY", ["SPY"], rh.MAX_DAYS)
        days = self.check(out)
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        cut = next(i for i, g in enumerate(gaps) if g <= 3)            # first daily step
        self.assertTrue(all(g <= 4 for g in gaps[cut:]), "once daily, always daily: no weekly point between days")
        self.assertTrue(all(g == 7 for g in gaps[:cut - 1]), "older stretch is one point per week")
        self.assertGreater(len(days) - cut, 700, "about three years of daily bars")
        # each re-stamped weekly point carries ITS week's Friday close
        for t, c in list(zip(out["t"], out["c"]))[:cut]:
            d = rh.et_date(t)
            self.assertEqual(d.weekday(), 4)
            self.assertEqual(c, close(d))

    def test_all_file_reaches_back_with_quarterly_then_weekly_then_daily(self):
        bars, _ = self.fake()
        with mock.patch.object(rh, "cnbc_bars", bars):
            out = rh.cnbc_history(None, "SPY", ["SPY"], None)
        days = self.check(out)
        self.assertLess(days[0].year, 1994)
        self.assertEqual(days, sorted(set(days)))

    def test_validator_rejects_weekend_and_duplicate_bars(self):
        sat = datetime.date(2026, 10, 10)
        self.assertIn("weekend", rh.validate_series("SPY", {"t": [self.stamp(sat)], "c": [1.0]}))
        mon = self.stamp(datetime.date(2026, 10, 12))
        self.assertIn("one date", rh.validate_series("SPY", {"t": [mon, mon + 3600], "c": [1.0, 2.0]}))
        self.assertIsNone(rh.validate_series("BTC-USD", {"t": [self.stamp(sat)], "c": [1.0]}))

    def test_build_series_refuses_to_publish_an_invalid_series(self):
        sat = self.stamp(datetime.date(2026, 10, 10))
        bad = {"t": [sat + i * DAY for i in range(10)], "c": [1.0] * 10}
        with mock.patch.object(rh, "treasury_history", return_value={}), \
             mock.patch.object(rh, "mortgage_history", return_value={}), \
             mock.patch.object(rh, "holding_history", return_value=(bad, "cnbc")):
            series, fresh = rh.build_series(None, {"SPY": bad}, rh.MAX_DAYS, include_crypto=False, extra_cnbc={"SPY": ["SPY"]})
        self.assertNotIn("SPY", series, "an invalid carried-over series is dropped, not kept")
        self.assertNotIn("SPY:cnbc", fresh)

    def test_nasdaq_daily_wins_and_is_trading_days_only(self):
        today = datetime.date.today()
        rows = [{"date": (today - datetime.timedelta(days=i)).strftime("%m/%d/%Y"), "close": f"${100 + i % 7}.00"}
                for i in range(1, 1830)]
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"data": {"tradesTable": {"rows": rows}}}
        session = mock.Mock()
        session.get.return_value = resp
        ser, how = rh.holding_history(session, "AAPL", ["AAPL"], rh.MAX_DAYS)
        self.assertEqual(how, "nasdaq")
        self.check(ser)
        self.assertGreater(len(ser["t"]), 1200)

    def test_nasdaq_sparse_answer_is_not_trusted(self):
        rows = [{"date": "10/15/2026", "close": "$1.00"}]
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"data": {"tradesTable": {"rows": rows}}}
        session = mock.Mock()
        session.get.return_value = resp
        self.assertIsNone(rh.nasdaq_history(session, "AAPL", rh.MAX_DAYS))


class MainTests(unittest.TestCase):
    """main() writes a v2 main file and, separately, an All file; one failing never blocks the other."""

    def run_main(self, repo, all_ok=True):
        pw = os.path.join(repo, "pw.txt")
        open(pw, "w").write("pass")
        os.makedirs(os.path.join(repo, "data"), exist_ok=True)
        calls = []

        def fake_build(session, carry, max_days, include_crypto=True, extra_cnbc=None):
            calls.append(max_days)
            if max_days is None and not all_ok:
                raise RuntimeError("boom")
            n = 8
            return {f"S{i}": rh.prepare(daily(1500 if max_days else 4000), max_days) for i in range(n)}, ["x:y"]

        with mock.patch.object(rh, "build_series", fake_build), \
             mock.patch.object(rh, "make_session", lambda: None), \
             mock.patch.object(sys, "argv", ["refresh_history.py", pw, repo]):
            rh.main()
        return calls

    def read(self, repo, name):
        pw = rh.read_passphrase(os.path.join(repo, "pw.txt"))
        return rh.decrypt_json(json.load(open(os.path.join(repo, "data", name))), pw)

    def test_writes_both_files_daily_and_versioned(self):
        with tempfile.TemporaryDirectory() as repo:
            self.assertEqual(self.run_main(repo), [rh.MAX_DAYS, None])
            main = self.read(repo, "history.json.enc")
            allf = self.read(repo, "history_all.json.enc")
            self.assertEqual(main["v"], rh.FORMAT_VERSION)
            self.assertEqual(allf["v"], rh.FORMAT_VERSION)
            t = main["series"]["S0"]["t"]
            self.assertEqual({b - a for a, b in zip(t, t[1:])}, {DAY})
            self.assertGreater(len(allf["series"]["S0"]["t"]), len(t))

    def test_second_run_within_window_rebuilds_nothing(self):
        with tempfile.TemporaryDirectory() as repo:
            self.run_main(repo)
            self.assertEqual(self.run_main(repo), [])

    def test_old_thinned_file_is_rebuilt_not_trusted(self):
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "data"))
            pw = os.path.join(repo, "pw.txt")
            open(pw, "w").write("pass")
            old = {"updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                   "series": {f"S{i}": {"t": [1, 2], "c": [1, 2]} for i in range(12)}}      # no "v": the weekly-thinned format
            json.dump(rh.encrypt_json(old, rh.read_passphrase(pw)), open(os.path.join(repo, "data", "history.json.enc"), "w"))
            self.assertIn(rh.MAX_DAYS, self.run_main(repo))

    def test_all_failure_does_not_lose_main(self):
        with tempfile.TemporaryDirectory() as repo:
            self.run_main(repo, all_ok=False)
            self.assertTrue(os.path.exists(os.path.join(repo, "data", "history.json.enc")))
            self.assertFalse(os.path.exists(os.path.join(repo, "data", "history_all.json.enc")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
