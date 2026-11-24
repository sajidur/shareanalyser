"""Offline tests: DSE parsers (against saved real pages), indicators, scoring, alerts.
Run:  python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import math
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bdstock import alerts, analysis, collector, dse_client as dse, indicators as ind  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class ParserTests(unittest.TestCase):
    def test_snapshot(self):
        snap = dse.parse_snapshot(json.loads(read("snapshot.json")))
        self.assertEqual(snap.trade_date, date(2026, 11, 22))
        self.assertGreater(len(snap.quotes), 600)
        self.assertFalse(snap.is_open)
        gp = next(q for q in snap.quotes if q["code"] == "GP")
        self.assertEqual(gp["close"], 226.7)
        self.assertEqual(gp["sector"], "Telecom")
        self.assertEqual(gp["category"], "A")
        self.assertGreater(gp["volume"], 0)
        # untraded instrument gets a flat bar at its close, never zeros
        untraded = next(q for q in snap.quotes if q["volume"] == 0)
        self.assertTrue(untraded["close"] and untraded["open"] == untraded["close"] == untraded["high"])
        self.assertEqual({i["key"] for i in snap.indices}, {"DSEX", "DS30", "DSES"})
        self.assertTrue(snap.daily_totals)

    def test_snapshot_retries_empty_feed(self):
        from unittest import mock

        good = json.loads(read("snapshot.json"))
        empty = {"market": good["market"], "prices": {"cols": good["prices"]["cols"], "rows": []}}
        with mock.patch.object(dse, "_get", side_effect=[empty, good]), mock.patch.object(dse.time, "sleep"):
            snap = dse.fetch_snapshot()
        self.assertGreater(len(snap.quotes), 600)
        with mock.patch.object(dse, "_get", side_effect=[empty, empty, empty]), mock.patch.object(dse.time, "sleep"):
            with self.assertRaises(dse.DSEError):
                dse.fetch_snapshot()

    def test_trade_date_falls_back_to_session(self):
        self.assertEqual(dse._parse_trade_date(None, {"sessionDate": "2026-11-19"}), date(2026, 11, 19))
        self.assertEqual(dse._parse_trade_date("Sept 3, 2026 at 2:40 PM", {}), date(2026, 9, 3))

    def test_company_page(self):
        data = dse.parse_company_page(read("company_GP.html"))
        c, bars = data["company"], data["bars"]
        self.assertEqual(c["code"], "GP")
        self.assertEqual(c["name"], "Grameenphone Ltd.")
        self.assertGreater(len(bars), 200)
        self.assertEqual(bars[-1]["trade_date"], date(2026, 11, 22))
        self.assertEqual(bars[-1]["close"], 226.7)
        self.assertTrue(all(b["low"] <= b["close"] <= b["high"] for b in bars))
        self.assertTrue(all(a["trade_date"] < b["trade_date"] for a, b in zip(bars, bars[1:])))
        self.assertIsNone(bars[0]["ycp"])
        self.assertEqual(bars[1]["ycp"], bars[0]["close"])

    def test_pe_page(self):
        pe = dse.parse_pe_page(read("pe.html"))
        self.assertGreater(len(pe), 300)
        self.assertAlmostEqual(pe["GP"], 10.69)
        self.assertIsNone(pe["1JANATAMF"])  # 'n/a'

    def test_recent_market_page(self):
        rows = dse.parse_recent_market_page(read("recent_market.html"))
        self.assertGreaterEqual(len(rows), 20)
        self.assertEqual(rows[-1]["trade_date"], date(2026, 11, 22))
        self.assertAlmostEqual(rows[-1]["DSEX"], 5565.59267)
        self.assertTrue(all(a["trade_date"] < b["trade_date"] for a, b in zip(rows, rows[1:])))

    def test_num(self):
        self.assertEqual(dse.num("1,234.5"), 1234.5)
        self.assertIsNone(dse.num("n/a"))
        self.assertIsNone(dse.num(""))
        self.assertIsNone(dse.num(float("nan")))
        self.assertEqual(dse.num(3), 3.0)


class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(ind.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_ema_seeded_with_sma(self):
        out = ind.ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)  # 4*0.5 + 2*0.5
        self.assertAlmostEqual(out[4], 4.0)

    def test_rsi_extremes(self):
        up = [float(i) for i in range(1, 40)]
        self.assertEqual(ind.rsi(up)[-1], 100.0)
        down = list(reversed(up))
        self.assertAlmostEqual(ind.rsi(down)[-1], 0.0)
        flat = [5.0] * 30
        self.assertEqual(ind.rsi(flat)[-1], 50.0)

    def test_rsi_known_value(self):
        # StockCharts' Wilder RSI example. They publish 70.53 / 66.32 because they round
        # the averages; exact: avg gain 3.34/14, avg loss 1.40/14 -> 70.464, then 66.250.
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                  45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
        r = ind.rsi(closes, 14)
        self.assertAlmostEqual(r[14], 70.464, delta=0.01)
        self.assertAlmostEqual(r[15], 66.250, delta=0.01)

    def test_bollinger_flat(self):
        mid, up, lo = ind.bollinger([10.0] * 25, 20, 2)
        self.assertEqual(mid[-1], 10.0)
        self.assertEqual(up[-1], 10.0)
        self.assertEqual(lo[-1], 10.0)

    def test_macd_flat_is_zero(self):
        line, sig, hist = ind.macd([10.0] * 60)
        self.assertAlmostEqual(line[-1], 0.0)
        self.assertAlmostEqual(hist[-1], 0.0)
        self.assertIsNone(line[24])
        self.assertIsNotNone(line[25])

    def test_crossed(self):
        a = [1, 2, 3, 4]
        b = [3, 3, 3, 3]
        self.assertEqual(ind.crossed(a, b, 3, 3), 1)
        self.assertEqual(ind.crossed(b, a, 3, 3), -1)
        self.assertEqual(ind.crossed(a, b, 3, 1), 1)
        self.assertEqual(ind.crossed([5, 5, 5, 5], b, 3, 3), 0)

    def test_atr_constant_range(self):
        h = [11.0] * 30
        lo = [9.0] * 30
        c = [10.0] * 30
        self.assertAlmostEqual(ind.atr(h, lo, c, 14)[-1], 2.0)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.bars = dse.parse_company_page(read("company_GP.html"))["bars"]

    def test_real_series(self):
        a = analysis.analyze_series(self.bars, "A")
        self.assertIsNotNone(a)
        self.assertTrue(-100 <= a["score"] <= 100)
        self.assertEqual(a["label"], analysis.label_for(a["score"]))
        self.assertEqual(a["score"], max(-100, min(100, sum(r["points"] for r in a["reasons"]))))
        self.assertTrue(0 <= a["rsi"] <= 100)
        self.assertLessEqual(a["low52"], a["close"])
        self.assertGreaterEqual(a["high52"], a["close"])
        self.assertTrue(set(a["flags"]) <= set(analysis.FLAG_LABELS))
        self.assertEqual(set(a["pivots"]), {"R2", "R1", "P", "S1", "S2"})

    def test_chart_series_aligned(self):
        s = analysis.chart_series(self.bars)
        for key, pts in s.items():
            self.assertEqual(len(pts), len(self.bars), key)
        self.assertNotIn("value", s["sma200"][0])
        self.assertIn("value", s["sma20"][-1])

    def test_too_short(self):
        self.assertIsNone(analysis.analyze_series(self.bars[:10]))

    def test_uptrend_is_bullish_and_flags(self):
        bars = [{"trade_date": i, "open": 10 + i * 0.1, "high": 10.2 + i * 0.1, "low": 9.9 + i * 0.1,
                 "close": 10.1 + i * 0.1, "volume": 1000} for i in range(260)]
        bars[-1]["volume"] = 5000
        a = analysis.analyze_series(bars, "Z", circuit_pct=9.5)
        self.assertGreaterEqual(a["score"], 40)
        self.assertIn("new_52w_high", a["flags"])
        self.assertIn("vol_spike", a["flags"])
        self.assertIn("z_category", a["flags"])
        self.assertIn("above_sma200", a["flags"])


class AlertTests(unittest.TestCase):
    def test_price_alerts(self):
        self.assertTrue(alerts.evaluate("price_above", 100, {"price": 100.0})[0])
        self.assertFalse(alerts.evaluate("price_above", 100, {"price": 99.9})[0])
        self.assertTrue(alerts.evaluate("price_below", 50, {"price": 49})[0])

    def test_pct_alerts_use_magnitude(self):
        self.assertTrue(alerts.evaluate("pct_down", 3, {"change_pct": -3.2})[0])
        self.assertTrue(alerts.evaluate("pct_down", -3, {"change_pct": -3.2})[0])
        self.assertFalse(alerts.evaluate("pct_up", 3, {"change_pct": -5})[0])

    def test_flag_alerts(self):
        self.assertTrue(alerts.evaluate("golden_cross", None, {"price": 10, "flags": {"golden_cross"}})[0])
        self.assertFalse(alerts.evaluate("new_52w_high", None, {"price": 10, "flags": set()})[0])

    def test_indicator_alerts(self):
        self.assertTrue(alerts.evaluate("rsi_below", 30, {"rsi": 25.0})[0])
        self.assertTrue(alerts.evaluate("score_above", 40, {"score": 45})[0])
        self.assertFalse(alerts.evaluate("score_below", -40, {"score": -10})[0])

    def test_describe(self):
        self.assertIn("Tk 230", alerts.describe("price_above", 230))
        self.assertEqual(alerts.describe("new_52w_high", None), "Makes a new 52-week high")


class PortfolioTests(unittest.TestCase):
    def test_average_cost_and_realized(self):
        from bdstock import portfolio

        txs = [
            {"code": "GP", "side": "BUY", "qty": 100, "price": 230.0, "fee": 92.0, "trade_date": date(2026, 11, 10)},
            {"code": "GP", "side": "BUY", "qty": 50, "price": 220.0, "fee": 40.0, "trade_date": date(2026, 11, 12)},
            {"code": "GP", "side": "SELL", "qty": 30, "price": 228.0, "fee": 27.36, "trade_date": date(2026, 11, 20)},
        ]
        p = portfolio.compute_positions(txs)["GP"]
        self.assertEqual(p["qty"], 120)
        self.assertAlmostEqual(p["cost"], 27305.6, places=2)          # 34132 - 30 * 227.5467
        self.assertAlmostEqual(p["cost"] / p["qty"], 227.5467, places=4)
        self.assertAlmostEqual(p["realized"], -13.76, places=2)       # 6840 - 27.36 - 6826.40
        self.assertAlmostEqual(p["fees"], 159.36, places=2)

    def test_full_exit_resets_cost(self):
        from bdstock import portfolio

        txs = [
            {"code": "X", "side": "BUY", "qty": 10, "price": 10.0, "fee": 0.0, "trade_date": date(2026, 1, 1)},
            {"code": "X", "side": "SELL", "qty": 10, "price": 12.0, "fee": 0.0, "trade_date": date(2026, 1, 2)},
        ]
        p = portfolio.compute_positions(txs)["X"]
        self.assertEqual((p["qty"], p["cost"], p["realized"]), (0, 0.0, 20.0))


class PredictTests(unittest.TestCase):
    def test_trading_days_skip_bd_weekend(self):
        from bdstock import predict

        days = predict.next_trading_days(date(2026, 11, 19), 3, holidays=set())  # Thursday
        self.assertEqual(days, [date(2026, 11, 22), date(2026, 11, 23), date(2026, 11, 24)])  # Sun, Mon, Tue
        self.assertTrue(all(d.weekday() not in (4, 5) for d in predict.next_trading_days(date(2026, 1, 1), 50, set())))

    def test_holidays_published_and_projected(self):
        from bdstock import predict

        data = {"2026": [{"date": "2026-12-16", "name": "Victory Day", "moon": False},
                         {"date": "2026-03-20", "name": "Eid-ul-Fitr", "moon": True}]}
        h = predict.holiday_dates(range(2026, 2028), data)
        self.assertIn(date(2026, 12, 16), h)
        self.assertIn(date(2027, 12, 16), h)          # fixed-date holiday repeats
        self.assertIn(date(2027, 3, 9), h)            # moon holiday ~11 days earlier next year
        days = predict.next_trading_days(date(2026, 12, 15), 2, holidays=h)  # Tue 15 Dec
        self.assertEqual(days, [date(2026, 12, 17), date(2026, 12, 20)])    # skips Victory Day + weekend

    def test_holiday_page_parsing(self):
        self.assertEqual(dse.expand_holiday("11-12 February", 2026), [date(2026, 2, 11), date(2026, 2, 12)])
        self.assertEqual(len(dse.expand_holiday("17 March - 23 March", 2026)), 7)
        self.assertEqual(dse.expand_holiday("4 September", 2026), [date(2026, 9, 4)])
        self.assertEqual(dse.expand_holiday("TBA", 2026), [])

    def test_band_quantiles_normal(self):
        import numpy as np
        from bdstock import predict

        rng = np.random.default_rng(1)
        sigma = np.full(200_000, 0.02)
        y = rng.normal(0, 0.02 * 2, 200_000)  # horizon 4 -> sigma*sqrt(4)
        q = predict.band_quantiles(y, sigma, 4)
        self.assertAlmostEqual(q["q16"], -0.994, delta=0.02)
        self.assertAlmostEqual(q["q84"], 0.994, delta=0.02)
        self.assertAlmostEqual(q["q50"], 0.0, delta=0.02)

    def test_setup_rule(self):
        from bdstock import predict

        self.assertTrue(predict.is_setup(102, 100, 95, 55))      # uptrend, near 20-day avg
        self.assertFalse(predict.is_setup(110, 100, 95, 55))     # >5% above 20-day avg: chasing
        self.assertFalse(predict.is_setup(102, 100, 95, 72))     # overbought
        self.assertFalse(predict.is_setup(94, 100, 95, 55))      # below 50-day avg
        self.assertFalse(predict.is_setup(102, None, 95, 55))

    def test_pct_rank_and_eps_growth(self):
        from bdstock import predict

        self.assertEqual(predict._pct_rank([10, None, 5, 5]), [1.0, None, 0.25, 0.25])
        self.assertEqual(predict._pct_rank([10, 5], higher_better=False), [0.0, 1.0])
        g, pos = predict._eps_growth([{"year": 2022, "epsBasic": 1.0}, {"year": 2023, "epsBasic": 1.1}, {"year": 2024, "epsBasic": 1.21}])
        self.assertAlmostEqual(g, 0.10, places=6)
        self.assertTrue(pos)
        self.assertEqual(predict._eps_growth([{"year": 2024, "epsBasic": 1.0}]), (None, False))

    def test_model_learns_signal(self):
        import numpy as np
        from bdstock import predict

        rng = np.random.default_rng(0)
        X = rng.normal(size=(20_000, 3))
        y = 0.01 * X[:, 0] + rng.normal(0, 0.01, 20_000)
        m = predict.Model().fit(X, y)
        prob, ret = m.predict(np.array([[2.0, 0, 0], [-2.0, 0, 0]]))
        self.assertGreater(prob[0], 0.6)
        self.assertLess(prob[1], 0.4)
        self.assertGreater(ret[0], 0)
        self.assertLess(ret[1], 0)


class NewsTests(unittest.TestCase):
    def test_company_announcements_with_text_refs(self):
        data = dse.parse_company_page(read("company_GP.html"))
        ann = data["announcements"]
        self.assertGreaterEqual(len(ann), 30)
        self.assertTrue(all(a["date"] and not a["body"].startswith("$") for a in ann))
        div = next(a for a in ann if a["summary"].startswith("Declaration of Interim Dividend"))
        self.assertIn("Record date", div["body"])

    def test_classify_earnings_dividend_record(self):
        from bdstock import news

        c = news.classify("Financials", "Q3 Financials",
                          "(Q3 Un-audited): EPS was Tk. 1.20 for July-September 2026 as against Tk. 1.00 for July-September 2025")
        self.assertAlmostEqual(c["eps_growth"], 0.2)
        self.assertIn("earnings", c["tags"])
        self.assertGreater(c["tone"], 0)
        loss = news.classify("Financials", "Q1", "EPS was Tk. (0.50) for July-September 2026 as against Tk. 0.25 for July-September 2025")
        self.assertEqual(loss["eps_growth"], -1.0)
        d = news.classify("Dividend", "Declaration of Interim Dividend",
                          "The Board has declared Interim Cash Dividend at the rate of 105% of the paid-up capital. "
                          "Record date for entitlement of Interim Cash Dividend is August 12, 2026.")
        self.assertIn("dividend", d["tags"])
        self.assertEqual(d["record_date"], date(2026, 8, 12))
        self.assertEqual(d["cash_pct"], 105.0)
        nd = news.classify("Dividend", "Dividend Declaration", "The Board of Directors has recommended No dividend for the year.")
        self.assertIn("no_dividend", nd["tags"])
        self.assertLess(nd["tone"], 0)
        r2 = news.classify("Notice", "Record date", "Record Date: 06.08.2026.")
        self.assertEqual(r2["record_date"], date(2026, 8, 6))
        r3 = news.classify("Notice", "Suspension for Record Date",
                           "Trading of the shares of the company will remain suspended on record date i.e., 12-Aug-2026.")
        self.assertEqual(r3["record_date"], date(2026, 8, 12))
        r4 = news.classify("Notice", "Resumption after Record Date", "Trading of the shares of the company will resume on 13-Aug-2026.")
        self.assertIsNone(r4["record_date"])
        op = news.classify("Notice", "Operating update", "operating results were discussed")
        self.assertNotIn("rating", op["tags"])

    def test_classify_cash_flow_nav_and_fund_nav(self):
        from bdstock import news

        q = news.classify("Financials", "Q1 Financials",
                          "(Q1 Un-audited): Consolidated EPS was Tk. 3.43 for July-September 2024 as against Tk. 2.70 for "
                          "July-September 2023; Consolidated NOCFPS was Tk. 2.85 for July-September 2024 as against Tk. (2.65) "
                          "for July-September 2023. Consolidated NAV per share was Tk. 51.46 as on September 30, 2024 and "
                          "Tk. 48.03 as on June 30, 2024.")
        self.assertAlmostEqual(q["eps_growth"], (3.43 - 2.70) / 2.70, places=4)
        self.assertEqual(q["nocf_growth"], 1.0)  # negative to positive cash flow, capped
        self.assertAlmostEqual(q["nav_growth"], round((51.46 - 48.03) / 48.03, 4), places=4)
        self.assertIsNone(q["mf_nav"])
        mf = news.classify("NAV", "Weekly NAV",
                           "On the close of operation on 18-Oct-2026, the Fund has reported Net Asset Value (NAV) of Tk. 8.17 "
                           "per unit on the basis of current market price and Tk. 12.13 per unit on the basis of cost price.")
        self.assertEqual(mf["mf_nav"], 8.17)
        self.assertIsNone(mf["nocf_growth"])

    def test_news_features_point_in_time(self):
        from bdstock import news

        ev = [(date(2026, 1, 10), 0.5, 0.2, True, False, date(2026, 1, 20))]
        f = news.features([date(2026, 1, 9), date(2026, 1, 12), date(2026, 3, 1), date(2026, 5, 1)], ev)
        self.assertEqual(f["news_cnt"], [0.0, 1.0, 0.0, 0.0])   # nothing known before the news date
        self.assertEqual(f["record_soon"], [0.0, 1.0, 0.0, 0.0])
        self.assertEqual(f["div_event"], [0.0, 1.0, 0.0, 0.0])
        self.assertEqual(f["eps_surprise"], [0.0, 0.2, 0.2, 0.0])  # kept for 90 days

    def test_fundamentals_only_after_publication(self):
        import json as _json
        import numpy as np
        from bdstock import predict

        info = {"face_value": 10, "year_end": "December", "extra_json": _json.dumps({
            "multiYearFinancials": [{"year": 2024, "epsBasic": 2.0, "nav": 20}, {"year": 2025, "epsBasic": 3.0, "nav": 22}],
            "dividendHistory": [{"year": 2025, "cash": 15}]})}
        pts = predict.fundamental_points(info)
        self.assertEqual(pts[-1][0], date(2025, 12, 31) + __import__("datetime").timedelta(days=120))
        dates = [date(2026, 3, 1), date(2026, 6, 1)]
        f = predict.fundamental_features(dates, np.array([30.0, 30.0]), pts)
        self.assertAlmostEqual(f["ey"][0], 2.0 / 30)      # FY2025 not published yet in March
        self.assertAlmostEqual(f["ey"][1], 3.0 / 30)      # published by June
        self.assertAlmostEqual(f["dy"][1], 1.5 / 30)
        self.assertAlmostEqual(f["eps_g"][1], 0.5)
        self.assertEqual(list(f["f_missing"]), [0.0, 0.0])


class RangeModelTests(unittest.TestCase):
    def test_vol_model_tracks_true_volatility_and_calibrates(self):
        import numpy as np
        from bdstock import predict

        rng = np.random.default_rng(3)
        n = 60_000
        true_sigma = np.exp(rng.uniform(np.log(0.005), np.log(0.05), n))
        V = np.column_stack([np.log(true_sigma) + rng.normal(0, 0.2, n), rng.normal(size=n)])
        y = rng.normal(0, true_sigma)
        vm = predict.VolModel().fit(V[:40_000], y[:40_000])
        s = vm.sigma(V[40_000:])
        self.assertGreater(np.corrcoef(np.log(s), np.log(true_sigma[40_000:]))[0, 1], 0.9)
        q = vm.q_all
        z = y[40_000:] / s
        self.assertAlmostEqual(((z >= q["q25"]) & (z <= q["q75"])).mean(), 0.50, delta=0.02)
        self.assertAlmostEqual(((z >= q["q5"]) & (z <= q["q95"])).mean(), 0.90, delta=0.02)
        # a narrower 50% band than 68% band
        self.assertLess(q["q75"] - q["q25"], q["q84"] - q["q16"])

    def test_ownership_point_in_time(self):
        import json as _json
        from bdstock import predict

        info = {"extra_json": _json.dumps({"sharePattern": [
            {"date": "Dec 31, 2025 (year ended)", "pattern": {"sponsor": 60, "institution": 10, "foreign": 1}},
            {"date": "Sep 30, 2026", "pattern": {"sponsor": 55, "institution": 15, "foreign": 2}}]})}
        f = predict.ownership_features([date(2026, 10, 15), date(2026, 11, 5)], info)
        self.assertEqual(list(f["sponsor"]), [60.0, 55.0])  # Sep 30 pattern counts only after ~30 days
        self.assertEqual(list(f["institution"]), [10.0, 15.0])


class LongHorizonTests(unittest.TestCase):
    def test_long_ranges_grow_with_sqrt_time(self):
        import math as _m
        import numpy as np
        from bdstock import predict

        class FakeRanger:
            def at(self, st, t):
                return 0.10, {"q5": -1.6, "q16": -1.0, "q25": -0.67, "q35": -0.39, "q50": 0.0,
                              "q65": 0.39, "q75": 0.67, "q84": 1.0, "q95": 1.6}

        st = {"c": np.full(300, 100.0), "f": {"_atr": np.full(300, 2.0), "_liq_mn": np.full(300, 10.0)}}
        rows = predict._long_rows("X", FakeRanger(), st, 299, 60, date(2026, 11, 23))
        self.assertEqual([r[2] for r in rows], list(predict.LONG_HORIZONS))
        # row layout: ..., median(7), low30(8), high30(9), low50(10), high50(11), low68(12), high68(13), ...
        w6 = _m.log(rows[0][13] / rows[0][12])   # 68% range width at 6 months (log)
        w12 = _m.log(rows[1][13] / rows[1][12])  # at 1 year
        self.assertAlmostEqual(w6, 2 * 0.10 * _m.sqrt(120 / 60), places=2)
        self.assertAlmostEqual(w12 / w6, _m.sqrt(245 / 120), places=2)
        self.assertIsNone(rows[0][4])  # no direction probability for estimates


class BangladeshFactorTests(unittest.TestCase):
    def test_classify_bd_news(self):
        from bdstock import bdfactors as bf

        self.assertEqual(bf.classify_news("Query Response", "In response to the DSE query dated November 02, 2026 ... unusual price hike"), ["dse_query"])
        self.assertIn("rumour", bf.classify_news("Clarification on News Published in Online News", "news published in the online news"))
        self.assertEqual(bf.classify_news("Buy Declaration of a Sponsor Director", "intention to buy 1,024,138 shares"), ["sponsor_buy"])
        self.assertEqual(bf.classify_news("Sale declaration of a Sponsor", "intention to sell 1,000,000 units"), ["sponsor_sell"])
        self.assertIn("to_z", bf.classify_news("Category Change", "The Company will be placed in 'Z' category from existing 'A' category"))
        up = bf.classify_news("Category Change", "The Company has been placed in 'A' category from existing 'Z' category")
        self.assertIn("from_z", up)
        self.assertNotIn("to_z", up)
        self.assertIn("margin_ban", bf.classify_news("Prohibition", "requested to abstain from providing loan facilities to purchase securities"))
        self.assertEqual(bf.classify_news("Board Meeting schedule", "meeting of the Board of Directors will be held"), [])

    def test_spec_score(self):
        from bdstock import bdfactors as bf

        hi, why = bf.spec_score(0.1, 20, "Z", 0.40, 3, 4.0, True)
        self.assertEqual(hi, 100)
        self.assertTrue(any("free float" in w for w in why))
        lo, _ = bf.spec_score(0.9, 60, "A", 0.02, 0, 1.0, False)
        self.assertEqual(lo, 0)

    def test_event_features_point_in_time(self):
        from bdstock import bdfactors as bf

        ev = [(date(2026, 3, 1), "dse_query"), (date(2026, 3, 5), "sponsor_sell"), (date(2026, 3, 6), "to_z")]
        f = bf.event_features([date(2026, 2, 28), date(2026, 3, 1), date(2026, 3, 11), date(2026, 6, 30)], ev)
        self.assertEqual(f["bd_query"][0], 0.0)                       # nothing known before the event
        self.assertAlmostEqual(f["bd_query"][1], 1.0)
        self.assertAlmostEqual(f["bd_query"][2], math.exp(-1), places=6)  # decays over ~10 days
        self.assertEqual(f["bd_sponsor"][2], -1.0)
        self.assertEqual(f["bd_toz"][2], 1.0)
        self.assertEqual(f["bd_toz"][3], 0.0)                         # older than 60 days

    def test_classify_political_footprint(self):
        from bdstock import bdfactors as bf

        self.assertIn("state_control", bf.classify_news(
            "Dissolved Board of Directors", "Bangladesh Bank vide letter dated August 18, 2026 has dissolved the Board of Directors"))
        self.assertIn("state_control", bf.classify_news(
            "Appointment of Observer", "Bangladesh Bank has nominated Mr. X as its representative to act as an Observer on the Board"))
        self.assertIn("state_control", bf.classify_news(
            "Reconstitution", "Bangladesh Bank has reconstituted the Board of Directors of the Bank vide letter dated 13 May, 2026"))
        # a board change by the owners themselves is not state control
        self.assertNotIn("state_control", bf.classify_news(
            "Completion of transfer of shares & reconstitution of new Board",
            "The Board of Directors has noted the completion of the off-market transfer of shares and reconstituted the Board"))
        self.assertIn("political_hit", bf.classify_news(
            "Q2 Financials", "NOCFPS was lower, primarily caused by the political unrest and nationwide disruptions"))
        self.assertIn("political_hit", bf.classify_news("Q3 Financials", "revenue growth was restrained due to socio-political factors"))
        self.assertIn("court_hold", bf.classify_news(
            "Rescheduling of AGM", "pursuant to the Hon'ble High Court Division's Order Number 6957, the AGM will be held"))
        self.assertNotIn("regulator", bf.classify_news("Appointment of Administrator", "Administrator appointed by Bangladesh Bank"))
        self.assertIn("wipeout", bf.classify_news(
            "Reduction of entire Paid-up Capital to 0 by Order of Bangladesh Bank",
            "the entire paid-up capital of Union Bank PLC. amounting to BDT 1036,28,04,480 is reduced to zero (0)"))
        self.assertIn("wipeout", bf.classify_news(
            "Suspension of Trading", "Bangladesh Bank has declared the bank as non-viable (okarjokar) with effect from November 5, 2025"))

    def test_ai_political_score_capped_by_filings(self):
        from bdstock import llm

        today = date(2026, 11, 23)
        self.assertEqual(llm.political_cap([], today), 2)
        self.assertEqual(llm.political_cap([(date(2025, 12, 28), "wipeout")], today), -2)
        self.assertEqual(llm.political_cap([(date(2026, 8, 19), "state_control")], today), -2)
        self.assertEqual(llm.political_cap([(date(2025, 6, 1), "state_control")], today), -1)   # over a year ago
        self.assertEqual(llm.political_cap([(date(2026, 11, 22), "court_hold")], today), 0)

    def test_political_event_features_windows(self):
        from bdstock import bdfactors as bf

        ev = [(date(2026, 1, 10), "state_control"), (date(2026, 1, 10), "court_hold"), (date(2026, 1, 10), "political_hit")]
        f = bf.event_features([date(2026, 1, 9), date(2026, 3, 1), date(2026, 5, 1), date(2026, 6, 1), date(2026, 8, 1)], ev)
        self.assertEqual(f["bd_state"], [0.0, 1.0, 1.0, 1.0, 0.0])    # active 180 days
        self.assertEqual(f["bd_polhit"], [0.0, 1.0, 1.0, 0.0, 0.0])   # 120 days
        self.assertEqual(f["bd_court"], [0.0, 1.0, 0.0, 0.0, 0.0])    # 90 days

    def test_political_link_validation(self):
        from bdstock import bdfactors as bf

        with self.assertRaises(ValueError):    # neither a party nor a known situation
            bf.add_link({"code": "GP", "situation": "guess", "source": "x"})
        with self.assertRaises(ValueError):    # a party needs the person's role
            bf.add_link({"code": "GP", "party": "bnp", "source": "x"})
        with self.assertRaises(ValueError):    # a source is required
            bf.add_link({"code": "GP", "party": "bnp", "role": "mp", "source": " "})
        with self.assertRaises(ValueError):
            bf.add_link({"code": "GP", "party": "bnp", "role": "mp", "source": "paper", "since": "13/05/2026"})
        with self.assertRaises(ValueError):
            bf.add_link({"code": "GP", "party": "bnp", "role": "mp", "source": "paper", "since": "2026-05-01", "until": "2026-01-01"})

    def test_political_alignment_follows_who_governs(self):
        from bdstock import bdfactors as bf

        tl = bf.DEFAULT_TIMELINE
        al = {"party": "awami_league", "role": "mp"}
        bnp = {"party": "bnp", "role": "minister"}
        self.assertEqual(bf.link_alignment(al, date(2023, 5, 1), tl), "ruling")
        self.assertEqual(bf.link_alignment(al, date(2025, 1, 1), tl), "ousted")        # lost power Aug 2024
        self.assertEqual(bf.link_alignment(al, date(2030, 1, 1), tl), "opposition")    # more than 5 years later
        self.assertEqual(bf.link_alignment(bnp, date(2025, 1, 1), tl), "opposition")   # interim government
        self.assertEqual(bf.link_alignment(bnp, date(2026, 6, 1), tl), "ruling")
        self.assertIsNone(bf.link_alignment({**bnp, "since": "2026-03-01"}, date(2026, 2, 20), tl))
        self.assertEqual(bf.link_alignment({"exposure": "under_action"}, date(2026, 6, 1), tl), "under_action")
        # the riskiest link decides the company's position
        self.assertEqual(bf.company_alignment([bnp, {"exposure": "under_action"}], date(2026, 6, 1), tl), "under_action")
        self.assertEqual(bf.company_alignment([bnp, al], date(2026, 6, 1), tl), "ousted")
        self.assertIsNone(bf.ruling_party(date(2025, 1, 1), tl))

    def test_shock_beta_point_in_time(self):
        from bdstock import predict

        mret = np.array([0.0, -0.02, 0.01, -0.03, 0.0] * 20)
        r1 = mret * 2.0                       # always moves twice the market
        b = predict.shock_beta_series(r1, mret, np.ones(len(mret), dtype=bool))
        self.assertEqual(b[0], 1.0)          # no sell-off day yet: neutral
        self.assertAlmostEqual(b[-1], 0.8 * 2 + 0.2)   # 40 sell-off days: weight 40/(40+10) towards the raw 2.0
        self.assertLess(b[3], b[-1])         # shrunk while there are few days
        self.assertTrue(np.all(np.diff(b[mret <= -0.01]) >= -1e-12))

    def test_combined_effect_curve_and_weights(self):
        from bdstock import combined as cb

        e = {1: -0.01, 5: -0.03, 20: -0.06}
        self.assertEqual(cb.curve(e, 0), 0.0)
        self.assertAlmostEqual(cb.curve(e, 3), -0.02)               # halfway between 1 and 5 sessions
        self.assertAlmostEqual(cb.curve(e, 40), -0.06)              # flat after 20 sessions
        # an event 3 sessions ago, forecasting 5 sessions ahead: the effect between sessions 3 and 8
        self.assertAlmostEqual(cb.curve(e, 8) - cb.curve(e, 3), (-0.03 - 0.006) + 0.02, places=6)
        self.assertEqual(cb.weight(100, 0.8, True), 0.0)            # weak evidence counts for nothing
        self.assertEqual(cb.weight(100, 3.0, False), 0.0)           # median and trimmed mean disagree
        self.assertAlmostEqual(cb.weight(100, 3.0, True), 100 / 120)
        rng = np.random.default_rng(1)
        s = cb.summarize(rng.normal(-0.05, 0.02, 200))
        self.assertGreater(s["w"], 0.8)
        self.assertLess(s["eff"], -0.04)
        self.assertEqual(cb.summarize(np.array([0.1, 0.2]))["w"], 0.0)  # too few cases

    def test_combined_shift(self):
        from bdstock import predict

        shift, d = predict.combine(None, -0.04, 0.5, 0.01, 0.002, use_model=False)
        self.assertAlmostEqual(shift, -0.02)                        # half strength, model not used
        self.assertFalse(d["model"]["used"])
        shift2, _ = predict.combine(None, -0.04, 1.0, 0.012, 0.002, use_model=True)
        self.assertAlmostEqual(shift2, 0.01 - 0.04)                 # model part = its move minus the base midpoint
        shift3, _ = predict.combine(None, -2.0, 1.0, 0.0, 0.0, use_model=False)
        self.assertEqual(shift3, -0.30)                             # capped

    def test_range_model_with_bd_inputs(self):
        from bdstock import predict

        rng = np.random.default_rng(3)
        n = 5000
        V = rng.normal(size=(n, len(predict.V_ALL)))
        y = rng.normal(size=n) * np.exp(-4 + 0.3 * V[:, 0] + 0.4 * V[:, len(predict.V_BASE)])
        rows = {"V": V, "y": y, "sigma": np.full(n, 0.02)}
        r_plain = predict.Ranger("vol", rows, 1)
        r_bd = predict.Ranger("vol_bd_binned", rows, 1)
        self.assertEqual(len(r_plain.vm.b), len(predict.V_BASE) + 1)
        self.assertEqual(len(r_bd.vm.b), len(predict.V_ALL) + 1)
        self.assertGreater(abs(r_bd.vm.b[1 + len(predict.V_BASE)]), 0.1)   # picks up the BD input
        st = {"V": V, "f": {"_sigma": np.full(n, 0.02), "vol20": np.full(n, 0.02)}}
        s, q = r_bd.at(st, 10)
        self.assertGreater(s, 0)
        self.assertIn("q25", q)
        self.assertEqual(predict.Ranger("unknown", rows, 1).method, "baseline")


class PaperTradeTests(unittest.TestCase):
    def _bars(self, rows):
        return [{"trade_date": date(2026, 12, d), "open": o, "high": h, "low": lo, "close": c} for d, o, h, lo, c in rows]

    def test_target_hit(self):
        from bdstock import tradeplan as tp

        s = {"status": "pending", "entry_price": None, "entry_date": None, "entry_limit": 100.0, "stop": 95.0,
             "t2": 110.0, "exit_by": date(2026, 12, 20)}
        r = tp._follow(s, self._bars([(1, 101, 102, 99.5, 101), (2, 101, 105, 100, 104), (3, 104, 111, 103, 110)]), 0.4)
        self.assertEqual(r["status"], "target")
        self.assertEqual(r["entry_price"], 100.0)
        self.assertAlmostEqual(r["return_pct"], 10.0 - 0.8)

    def test_missed_and_stopped_and_expired(self):
        from bdstock import tradeplan as tp

        base = {"status": "pending", "entry_price": None, "entry_date": None, "entry_limit": 100.0, "stop": 95.0,
                "t2": 110.0, "exit_by": date(2026, 12, 3)}
        self.assertEqual(tp._follow(base, self._bars([(1, 102, 103, 101, 102)]), 0.4)["status"], "missed")
        st = tp._follow(base, self._bars([(1, 99, 100, 98, 99), (2, 97, 98, 94, 95)]), 0.4)
        self.assertEqual((st["status"], st["exit_price"]), ("stopped", 95.0))
        ex = tp._follow(base, self._bars([(1, 99, 100, 98, 99), (2, 100, 101, 99, 100), (3, 101, 102, 100, 101.5)]), 0.4)
        self.assertEqual((ex["status"], ex["exit_price"]), ("expired", 101.5))
        op = tp._follow(base, self._bars([(1, 99, 100, 98, 99)]), 0.4)
        self.assertEqual(op["status"], "open")


class TradePlanTests(unittest.TestCase):
    def test_tick_rounding(self):
        from bdstock import tradeplan as tp

        self.assertEqual(tp.floor_tick(66.87), 66.8)
        self.assertEqual(tp.ceil_tick(66.81), 66.9)
        self.assertEqual(tp.ceil_tick(66.8), 66.8)

    def test_lookup_interpolates(self):
        from bdstock import tradeplan as tp

        L = tp.LEVELS
        K = len(L)
        # win rate falls as the target gets further away and rises as the stop gets further away
        p = [[max(0.0, min(1.0, 0.5 + 0.08 * (L[j] - L[i]))) for j in range(K)] for i in range(K)]
        tables = {"levels": L, "windows": {"20": {"p_target": p, "p_stop": p, "ev_sigma": p}}}
        a = tp.lookup(tables, 20, 2.0, 2.0)["win_rate"]
        b = tp.lookup(tables, 20, 3.0, 2.0)["win_rate"]
        mid = tp.lookup(tables, 20, 1.75, 2.0)["win_rate"]
        self.assertAlmostEqual(a, 0.5)
        self.assertLess(b, a)
        self.assertTrue(b < mid < tp.lookup(tables, 20, 1.5, 2.0)["win_rate"] + 1e-9)
        self.assertIsNone(tp.lookup(tables, 20, -1, 2))

    def test_intraday_range_narrows(self):
        from datetime import datetime as dt
        from unittest import mock
        from bdstock import tradeplan as tp

        q = {"q5": -1.6, "q25": -0.6, "q75": 0.6, "q95": 1.7}
        state = {"is_open": True, "session": {"today": {"opening": "10:00", "closing": "14:00"}}}
        quote = {"ltp": 100.0}
        widths = []
        for hour in (10, 13):
            with mock.patch.object(tp, "datetime") as m:
                m.now.return_value = dt(2026, 11, 23, hour, 30)
                r = tp.intraday_range("X", quote, state, 0.02, q)
            widths.append(r["high50"] - r["low50"])
        self.assertLess(widths[1], widths[0])  # later in the session -> narrower
        self.assertIsNone(tp.intraday_range("X", quote, {"is_open": False}, 0.02, q))


class EodTimingTests(unittest.TestCase):
    def setUp(self):
        self.snap = dse.parse_snapshot(json.loads(read("snapshot.json")))

    def test_ready_after_post_close_delay(self):
        d = self.snap.trade_date
        self.assertFalse(collector.eod_ready(self.snap, datetime(d.year, d.month, d.day, 14, 5)))
        self.assertTrue(collector.eod_ready(self.snap, datetime(d.year, d.month, d.day, 14, 30)))

    def test_ready_next_day(self):
        self.assertTrue(collector.eod_ready(self.snap, datetime(2026, 11, 23, 9, 0)))

    def test_not_ready_while_open(self):
        self.snap.session["isOpen"] = True
        self.assertFalse(collector.eod_ready(self.snap, datetime(2026, 11, 23, 9, 0)))


if __name__ == "__main__":
    unittest.main()
