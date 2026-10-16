#!/usr/bin/env python3
"""Tests for scripts/refresh_quotes.py -- the server-side price snapshot the Portfolio paints first.

Run: python3 scripts/test_quotes.py

Regression guards for three Oct 2026 bugs, all with synthetic data (no network):
  * every CNBC quote was stamped "now", so a mutual fund still on yesterday's NAV (BPTRX) counted
    yesterday's move as today's day change;
  * crypto was skipped by the CNBC fallback, so BTC sat on a stale price;
  * the intraday series counted as "fresh" for 6 hours, so the Today chart froze at ~10am ET.
"""
import datetime
import os
import sys
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import refresh_quotes as rq  # noqa: E402

NY = ZoneInfo("America/New_York")


def cnbc_payload(rows):
    resp = mock.Mock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {"FormattedQuoteResult": {"FormattedQuote": rows}}
    return resp


class CnbcTimeTests(unittest.TestCase):
    def test_date_only_is_that_days_close_in_new_york(self):
        t = rq.parse_cnbc_time("2026-10-15")
        self.assertEqual(datetime.datetime.fromtimestamp(t, NY), datetime.datetime(2026, 10, 15, 16, 0, tzinfo=NY))

    def test_full_stamp_with_millis_and_offset(self):
        t = rq.parse_cnbc_time("2026-10-16T16:00:00.783-0400")
        self.assertEqual(datetime.datetime.fromtimestamp(t, NY).strftime("%Y-%m-%d %H:%M"), "2026-10-16 16:00")

    def test_garbage_falls_back(self):
        self.assertEqual(rq.parse_cnbc_time("soon", 7), 7)
        self.assertEqual(rq.parse_cnbc_time(None, 9), 9)


class FetchCnbcTests(unittest.TestCase):
    def test_fund_keeps_its_nav_date_and_crypto_is_mapped(self):
        rows = [
            {"symbol": "BPTRX", "last": "279.84", "previous_day_closing": "277.52", "last_time": "2026-10-15"},
            {"symbol": "BTC.CM=", "last": "81,558.57", "previous_day_closing": "82,354.74",
             "last_time": "2026-10-16T16:08:36.000-0400"},
            {"symbol": "AAPL", "last": "343.46", "change": "+1.46", "last_time": "2026-10-16T16:00:00.000-0400"},
        ]
        session = mock.Mock()
        session.get.return_value = cnbc_payload(rows)
        out = rq.fetch_cnbc(["BPTRX", "BTC-USD", "AAPL"], session)
        self.assertEqual(set(out), {"BPTRX", "BTC-USD", "AAPL"})
        nav_day = datetime.datetime.fromtimestamp(out["BPTRX"]["time"], NY).date()
        self.assertEqual(nav_day, datetime.date(2026, 10, 15), "a fund's quote must keep its NAV date")
        self.assertAlmostEqual(out["BTC-USD"]["price"], 81558.57)
        self.assertAlmostEqual(out["AAPL"]["prev"], 342.0, msg="prev derived from change when missing")
        url = session.get.call_args[0][0]
        self.assertIn("BTC.CM%3D", url)


class SeriesFreshnessTests(unittest.TestCase):
    def series(self, last_bar, end):
        return {"series": {"t": [last_bar - 600, last_bar], "c": [1, 2], "start": end - 23400, "end": end}}

    def run_attach(self, q):
        calls = []

        def fake_cnbc(sym, session):
            calls.append(sym)
            return None
        with mock.patch.object(rq, "series_cnbc", fake_cnbc), \
             mock.patch.object(rq, "series_yahoo_via_proxy", side_effect=RuntimeError("down")):
            rq.attach_series({"AAPL": q}, ["AAPL"], None)
        return calls

    def test_midsession_series_is_refetched(self):
        now = int(time.time())
        q = self.series(now - 300, now + 3 * 3600)          # last bar 5 min ago, close 3h away
        self.assertEqual(self.run_attach(q), ["AAPL"], "a series that hasn't reached the close must refresh")

    def test_completed_session_is_left_alone(self):
        now = int(time.time())
        q = self.series(now - 3600, now - 3600)              # reached its close an hour ago
        self.assertEqual(self.run_attach(q), [])


class FallbackTests(unittest.TestCase):
    def test_symbols_yahoo_missed_still_get_a_fallback_quote(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, "data"))
            pw = os.path.join(repo, "pw.txt")
            open(pw, "w").write("pass")
            holdings = {"positions": [{"ticker": "AAPL"}, {"ticker": "BTC", "fetchSymbol": "BTC-USD"}]}
            with open(os.path.join(repo, "data", "holdings.json.enc"), "w") as f:
                json.dump(rq.encrypt_json(holdings, rq.read_passphrase(pw)), f)
            yahoo = lambda sym, session: {"price": 1.0, "prev": 1.0, "time": 1} if sym == "AAPL" else None
            cnbc_calls = []

            def fake_cnbc(syms, session):
                cnbc_calls.append(list(syms))
                return {"BTC-USD": {"price": 2.0, "prev": 2.0, "time": 2, "src": "cnbc"}}
            with mock.patch.object(rq, "fetch_chart", yahoo), mock.patch.object(rq.time, "sleep", lambda s: None), \
                 mock.patch.object(rq, "fetch_stooq", lambda syms, s: {}), mock.patch.object(rq, "fetch_cnbc", fake_cnbc), \
                 mock.patch.object(rq, "attach_series", lambda *a: 0), mock.patch.object(rq, "fetch_board", lambda s: {}), \
                 mock.patch.object(sys, "argv", ["x", pw, repo]):
                rq.main()
            self.assertEqual(cnbc_calls, [["BTC-USD"]], "only the symbol Yahoo missed goes to the fallback")
            with open(os.path.join(repo, "data", "quotes.json.enc")) as f:
                out = rq.decrypt_json(json.load(f), rq.read_passphrase(pw))
            self.assertEqual(out["quotes"]["BTC-USD"]["price"], 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
