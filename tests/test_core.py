"""Offline tests: DSE parsers (against saved real pages), indicators, scoring, alerts.
Run:  python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

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

        days = predict.next_trading_days(date(2026, 11, 19), 3)  # Thursday
        self.assertEqual(days, [date(2026, 11, 22), date(2026, 11, 23), date(2026, 11, 24)])  # Sun, Mon, Tue
        self.assertTrue(all(d.weekday() not in (4, 5) for d in predict.next_trading_days(date(2026, 1, 1), 50)))

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
