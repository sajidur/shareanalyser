"""Flask web app: JSON API + the single-page dashboard UI."""
from __future__ import annotations

import json
import logging
import threading
import webbrowser
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import mysql.connector
from flask import Flask, abort, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from . import (__version__, alerts, analysis, bdfactors, collector, config, db, llm, news, notifier, portfolio,
               predict, report, settings, tradeplan)
from .monitor import JOBS, MarketMonitor

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat(timespec="seconds")
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class JSONProvider(DefaultJSONProvider):
    default = staticmethod(_json_default)
    sort_keys = False


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _quotes_rows() -> list[dict]:
    return db.query(
        "SELECT i.code, i.name, i.sector, i.category, i.board, i.asset_type, i.trailing_pe, "
        "q.trade_date, q.ltp, q.ycp, q.open, q.high, q.low, q.close, q.volume, q.value_mn, q.trades, "
        "q.change_pct, q.updated_at FROM instruments i JOIN live_quotes q ON q.code=i.code"
    )


def _latest_signals(columns: str) -> dict[str, dict]:
    sd = analysis.latest_signal_date()
    if not sd:
        return {}
    return {s["code"]: s for s in db.query(f"SELECT code, {columns} FROM signals WHERE trade_date=%s", (sd,))}


def create_app(start_monitor: bool = False) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.json = JSONProvider(app)
    monitor = MarketMonitor() if start_monitor else None

    allowed_hosts = {"127.0.0.1", "localhost", config.get("server", "host")}
    extra = config.load()["server"].get("allowed_hosts", "")
    allowed_hosts.update(h.strip() for h in extra.split(",") if h.strip())

    @app.before_request
    def guard():
        # Local personal tool: reject foreign Host headers (DNS rebinding) and
        # cross-site form posts (mutations must be JSON, which forces a CORS preflight).
        host = (request.host or "").rsplit(":", 1)[0].strip("[]")
        if host not in allowed_hosts:
            abort(403, "Host not allowed")
        if request.method in ("POST", "PUT", "DELETE") and request.path.startswith("/api/") and not request.is_json:
            return jsonify(error="Requests must use Content-Type: application/json"), 415

    @app.errorhandler(Exception)
    def on_error(e):
        if isinstance(e, HTTPException):
            return jsonify(error=e.description), e.code
        if isinstance(e, portfolio.PortfolioError):
            return jsonify(error=str(e)), 400
        if isinstance(e, mysql.connector.Error):
            log.error("Database error: %s", e)
            return jsonify(error=f"Database error: {e.msg if hasattr(e, 'msg') else e}", db_error=True), 503
        log.exception("API error on %s", request.path)
        return jsonify(error=str(e)), 500

    # ------------------------------------------------------------------ pages

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    # ------------------------------------------------------------------ status & market

    @app.get("/api/status")
    def status():
        hist = db.query_one(
            "SELECT COUNT(*) AS n_rows, COUNT(DISTINCT code) AS codes, MIN(trade_date) AS first_date, "
            "MAX(trade_date) AS last_date FROM daily_prices"
        )
        hist["days"] = db.scalar("SELECT COUNT(DISTINCT trade_date) FROM daily_prices")
        hist["codes_with_history"] = db.scalar(
            "SELECT COUNT(*) FROM (SELECT code FROM daily_prices GROUP BY code HAVING COUNT(*) >= 30) t"
        )
        bp = db.get_state("backfill_progress")
        if bp and bp.get("running"):
            try:  # a stale "running" marker means the process died
                if (datetime.now() - datetime.fromisoformat(bp["at"])).total_seconds() > 300:
                    bp["running"] = False
            except (KeyError, TypeError, ValueError):
                bp["running"] = False
        return jsonify(
            backfill_progress=bp,
            version=__version__,
            market=db.get_state("market"),
            history=hist,
            instruments=db.scalar("SELECT COUNT(*) FROM instruments"),
            last_report=db.scalar("SELECT MAX(report_date) FROM daily_reports"),
            signal_date=analysis.latest_signal_date(),
            jobs=JOBS.status(),
            runs=db.query("SELECT job, started_at, finished_at, status, message FROM job_runs ORDER BY id DESC LIMIT 12"),
            monitor=(monitor.last_cycle if monitor else None),
        )

    @app.get("/api/market")
    def market():
        quotes = _quotes_rows()
        sigs = _latest_signals("score, label, rsi")
        for q in quotes:
            s = sigs.get(q["code"]) or {}
            q["score"], q["label"] = s.get("score"), s.get("label")
        shares = [q for q in quotes if not analysis.is_debt(q["asset_type"])]  # exclude bonds
        traded = [q for q in shares if q["volume"]]
        idx_hist: dict[str, list] = {}
        for r in db.query(
            "SELECT code, trade_date, value FROM index_daily WHERE trade_date >= CURDATE() - INTERVAL 400 DAY ORDER BY trade_date"
        ):
            idx_hist.setdefault(r["code"], []).append({"time": r["trade_date"], "value": r["value"]})
        labels: dict[str, int] = {}
        for s in sigs.values():
            labels[s["label"]] = labels.get(s["label"], 0) + 1
        watch_codes = {w["code"] for w in db.query("SELECT code FROM watchlist")}
        return jsonify(
            state=db.get_state("market"),
            gainers=sorted(traded, key=lambda q: -(q["change_pct"] or 0))[:10],
            losers=sorted(traded, key=lambda q: q["change_pct"] or 0)[:10],
            turnover=sorted(traded, key=lambda q: -(q["value_mn"] or 0))[:10],
            sectors=report.sector_performance(shares),
            index_history=idx_hist,
            market_history=db.query(
                "SELECT * FROM (SELECT * FROM market_daily ORDER BY trade_date DESC LIMIT 60) t ORDER BY trade_date"
            ),
            watchlist=sorted((q for q in quotes if q["code"] in watch_codes), key=lambda q: q["code"]),
            portfolio=portfolio.summary()["totals"],
            events=db.query("SELECT * FROM alert_events ORDER BY id DESC LIMIT 15"),
            news=news.latest(12),
            tag_labels=news.TAG_LABELS,
            signal_breadth=labels,
            last_report=db.scalar("SELECT MAX(report_date) FROM daily_reports"),
        )

    @app.get("/api/quotes")
    def quotes():
        sigs = _latest_signals("score, label, rsi, vol_ratio, flags, high52, low52, ret_1w, ret_1m, ret_3m, ret_1y, sma50, sma200")
        watch = {w["code"] for w in db.query("SELECT code FROM watchlist")}
        rows = _quotes_rows()
        for r in rows:
            s = sigs.get(r["code"]) or {}
            for k in ("score", "label", "rsi", "vol_ratio", "high52", "low52", "ret_1w", "ret_1m", "ret_3m", "ret_1y", "sma50", "sma200"):
                r[k] = s.get(k)
            r["flags"] = analysis.parse_flags(s.get("flags"))
            r["watch"] = r["code"] in watch
        rows.sort(key=lambda r: r["code"])
        return jsonify(rows=rows, signal_date=analysis.latest_signal_date(), state=db.get_state("market"), flag_labels=analysis.FLAG_LABELS)

    # ------------------------------------------------------------------ stock detail

    @app.get("/api/stock/<code>")
    def stock(code: str):
        code = code.strip().upper()
        if not db.query_one("SELECT code FROM instruments WHERE code=%s", (code,)):
            abort(404, f"Unknown trading code {code}")
        warning = None
        try:
            collector.ensure_history(code)
        except Exception as e:
            warning = f"Could not download history from DSE: {e}"
        inst = db.query_one("SELECT * FROM instruments WHERE code=%s", (code,))
        bars = db.query(
            "SELECT trade_date, open, high, low, close, volume, value_mn, change_pct FROM daily_prices "
            "WHERE code=%s AND close > 0 ORDER BY trade_date DESC LIMIT 520",
            (code,),
        )[::-1]
        for b in bars:
            b["open"] = b["open"] or b["close"]
            b["high"] = b["high"] or max(b["open"], b["close"])
            b["low"] = b["low"] or min(b["open"], b["close"])
        quote = db.query_one("SELECT * FROM live_quotes WHERE code=%s", (code,))
        live_bar = False
        if quote and quote["volume"] and quote["trade_date"] and (not bars or quote["trade_date"] > bars[-1]["trade_date"]):
            price = quote["ltp"] or quote["close"]
            bars.append({"trade_date": quote["trade_date"], "open": quote["open"] or price, "high": quote["high"] or price,
                         "low": quote["low"] or price, "close": price, "volume": quote["volume"],
                         "value_mn": quote["value_mn"], "change_pct": quote["change_pct"]})
            live_bar = True
        an = analysis.analyze_series(bars, inst.get("category"), settings.get_float("circuit_pct")) if bars else None
        info = db.query_one("SELECT * FROM company_info WHERE code=%s", (code,))
        if info:
            try:
                info["extra"] = json.loads(info.pop("extra_json") or "{}")
            except ValueError:
                info["extra"] = {}
        pf = portfolio.summary()
        al = db.query("SELECT * FROM alerts WHERE code=%s ORDER BY id DESC", (code,))
        for a in al:
            a["description"] = alerts.describe(a["kind"], a["threshold"])
        return jsonify(
            instrument=inst,
            info=info,
            quote=quote,
            live_bar=live_bar,
            warning=warning,
            candles=[{"time": b["trade_date"], "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]} for b in bars],
            volumes=[{"time": b["trade_date"], "value": b["volume"], "up": b["close"] >= b["open"]} for b in bars],
            series=analysis.chart_series(bars) if bars else {},
            analysis=an,
            flag_labels=analysis.FLAG_LABELS,
            watch=bool(db.scalar("SELECT COUNT(*) FROM watchlist WHERE code=%s", (code,))),
            position=next((h for h in pf["holdings"] if h["code"] == code), None),
            transactions=portfolio.list_transactions(code),
            alerts=al,
            events=db.query("SELECT * FROM alert_events WHERE code=%s ORDER BY id DESC LIMIT 20", (code,)),
            commission_rate=settings.get_float("commission_rate"),
        )

    @app.post("/api/stock/<code>/refresh")
    def stock_refresh(code: str):
        n = collector.backfill_code(code.strip().upper())
        return jsonify(rows=n)

    # ------------------------------------------------------------------ prediction

    @app.get("/api/predict")
    def predict_overview():
        return jsonify(
            meta=predict.meta(),
            live=db.get_state("predict_live") or {},
            picks=predict.picks(10),
            watchlist=[w["code"] for w in db.query("SELECT code FROM watchlist ORDER BY code")],
        )

    @app.get("/api/predict/<code>")
    def predict_stock(code: str):
        code = code.strip().upper()
        inst = db.query_one("SELECT * FROM instruments WHERE code=%s", (code,))
        if not inst:
            abort(404, f"Unknown trading code {code}")
        bars = db.query(
            "SELECT trade_date, open, high, low, close, volume FROM daily_prices WHERE code=%s AND close > 0 "
            "ORDER BY trade_date DESC LIMIT 300",
            (code,),
        )[::-1]
        fc = predict.forecast(code)
        reason = None
        if not fc:
            reason = ("Forecasts cover shares and mutual funds, not bonds." if analysis.is_debt(inst.get("asset_type"))
                      else "Not enough price history yet (needs about 60 sessions).")
        levels, an = None, None
        if bars:
            for b in bars:
                b["open"] = b["open"] or b["close"]
                b["high"] = b["high"] or max(b["open"], b["close"])
                b["low"] = b["low"] or min(b["open"], b["close"])
            an = analysis.analyze_series(bars, inst.get("category"), settings.get_float("circuit_pct"))
            close = bars[-1]["close"]
            last20, last60 = bars[-20:], bars[-60:]
            atr = (fc or {}).get("atr") or (an or {}).get("atr")
            stop = close - 1.5 * atr if atr else None
            m1 = next((h for h in (fc or {}).get("horizons", []) if h["h"] == 20), None)
            target = m1["high68"] if m1 else None
            levels = {
                "close": close, "date": bars[-1]["trade_date"],
                "low20": min(b["low"] for b in last20), "high20": max(b["high"] for b in last20),
                "low60": min(b["low"] for b in last60), "high60": max(b["high"] for b in last60),
                "pivots": (an or {}).get("pivots"), "atr": atr,
                "stop": round(stop, 2) if stop else None, "target": target,
                "reward_risk": round((target - close) / (close - stop), 2) if stop and target and close > stop else None,
            }
        setup = bool(an and predict.is_setup(an["close"], an["sma20"], an["sma50"], an["rsi"]))
        meta = predict.meta()
        plan = tradeplan.plan_for(code) if fc else None
        intraday = None
        if fc:
            quote = db.query_one("SELECT * FROM live_quotes WHERE code=%s", (code,))
            q1 = (meta.get("horizons", {}).get("1") or {}).get("quantiles")
            h1 = next((h for h in fc["horizons"] if h["h"] == 1), None)
            if q1 and h1 and plan:
                intraday = tradeplan.intraday_range(code, quote, db.get_state("market"), plan["sigma_pct"] / 100, q1)
        note = llm.cached_note(code, fc["trade_date"]) if fc else None
        return jsonify(
            bd=bdfactors.stock_view(code),
            ai_profile=llm.get_profiles([code]).get(code),
            plan=plan,
            intraday=intraday,
            ai_note=note,
            ai=llm.status() if fc else None,
            buy_record=(db.get_state("trade_tables") or {}).get("buy_record"),
            instrument=inst,
            forecast=fc,
            reason=reason,
            history=[{"time": b["trade_date"], "value": b["close"]} for b in bars[-130:]],
            levels=levels,
            analysis={k: (an or {}).get(k) for k in ("score", "label", "rsi", "sma20", "sma50", "sma200", "ret_1m", "high52", "low52")},
            setup=setup,
            setup_rule=meta.get("setup_rule"),
            track=predict.stock_track(code),
            live=db.get_state("predict_live") or {},
            news=news.latest(15, code=code),
            tag_labels=news.TAG_LABELS,
        )

    @app.get("/api/plans")
    def plans():
        ap = tradeplan.all_plans()
        ap.pop("buy_all", None)
        return jsonify(**ap, record=tradeplan.signal_record(0) | {"rows": []})

    @app.get("/api/signals/log")
    def signal_log():
        return jsonify(tradeplan.signal_record(200))

    @app.get("/api/bd")
    def bd_overview():
        st = db.get_state("bd_factors") or {}
        spec = sorted(((c, v) for c, v in (st.get("spec_today") or {}).items() if v["score"] >= 45),
                      key=lambda kv: -kv[1]["score"])[:25]
        names = {r["code"]: r["name"] for r in db.query("SELECT code, name FROM instruments")}
        recent = db.query(
            "SELECT code, news_date, summary, LEFT(body, 3000) AS body FROM news WHERE news_date >= CURDATE() - INTERVAL 30 DAY "
            "ORDER BY news_date DESC, news_time DESC LIMIT 600")
        flagged = []
        for r in recent:
            kinds = bdfactors.classify_news(r["summary"] or "", r["body"] or "")
            if kinds:
                flagged.append({**r, "body": (r["body"] or "")[:400], "kinds": kinds, "name": names.get(r["code"])})
        pol = st.get("political") or {}
        shock = sorted(((c, v) for c, v in (pol.get("shock") or {}).items() if v["days"] >= 10),
                       key=lambda kv: -kv[1]["beta"])[:15]
        govt = sorted((pol.get("govt_hold") or {}).items(), key=lambda kv: -kv[1])
        pol_events = sorted(((d, c, t) for c, evs in (pol.get("events") or {}).items() for d, t in evs), reverse=True)[:40]
        return jsonify(
            political={k: pol.get(k) for k in ("shock_day_pct", "shock_days", "groups", "power_changes")},
            shock_top=[{"code": c, "name": names.get(c), **v} for c, v in shock],
            govt_top=[{"code": c, "name": names.get(c), "govt_hold": g, **((pol.get("shock") or {}).get(c) or {})} for c, g in govt],
            political_events=[{"date": d, "code": c, "type": t, "name": names.get(c)} for d, c, t in pol_events],
            links=[{**lk, "name": names.get(lk["code"])} for lk in bdfactors.political_links()],
            parties=bdfactors.PARTIES, roles=bdfactors.ROLES, alignments=bdfactors.ALIGNMENTS,
            situations={k: bdfactors.ALIGNMENTS[k] for k in bdfactors.SITUATIONS},
            timeline=bdfactors.timeline(),
            ruling_now=bdfactors.PARTIES.get(bdfactors.ruling_party(date.today()) or "", "no party (interim / caretaker)"),
            built_at=st.get("built_at"), history_from=st.get("history_from"), history_to=st.get("history_to"),
            studies=st.get("studies", {}), regime_now=st.get("regime_now"), regime_stats=st.get("regime_stats", {}),
            regime_history=st.get("regime_history", []), events=st.get("events", []),
            user_events=[e for e in bdfactors.market_events() if e["source"] == "you"],
            spec_stats=st.get("spec_stats", {}),
            spec_top=[{"code": c, "name": names.get(c), **v} for c, v in spec],
            recent_flagged=flagged[:60],
        )

    @app.post("/api/bd/events")
    def bd_event_add():
        try:
            new_id = bdfactors.add_event(_body())
        except ValueError as e:
            abort(400, str(e))
        JOBS.start("bdfactors", lambda progress=None: bdfactors.build())
        return jsonify(id=new_id)

    @app.delete("/api/bd/events/<int:event_id>")
    def bd_event_delete(event_id: int):
        bdfactors.delete_event(event_id)
        JOBS.start("bdfactors", lambda progress=None: bdfactors.build())
        return jsonify(ok=True)

    @app.post("/api/bd/links")
    def bd_link_add():
        try:
            new_id = bdfactors.add_link(_body())
        except ValueError as e:
            abort(400, str(e))
        JOBS.start("bdfactors", lambda progress=None: bdfactors.build())
        return jsonify(id=new_id)

    @app.post("/api/bd/timeline")
    def bd_timeline_save():
        try:
            tl = bdfactors.save_timeline((_body() or {}).get("periods") or [])
        except ValueError as e:
            abort(400, str(e))
        JOBS.start("bdfactors", lambda progress=None: bdfactors.build())
        return jsonify(timeline=tl)

    @app.delete("/api/bd/links/<int:link_id>")
    def bd_link_delete(link_id: int):
        bdfactors.delete_link(link_id)
        JOBS.start("bdfactors", lambda progress=None: bdfactors.build())
        return jsonify(ok=True)

    @app.post("/api/ai/note/<code>")
    def ai_note(code: str):
        code = code.strip().upper()
        st = llm.status()
        if not st["available"]:
            abort(503, f"Local AI not available ({st.get('error') or 'model ' + st['model'] + ' not installed'})")
        fc = predict.forecast(code)
        if not fc:
            abort(400, "No forecast for this instrument")
        plan = tradeplan.plan_for(code)
        inst = db.query_one("SELECT code, name, sector, category FROM instruments WHERE code=%s", (code,))
        info = db.query_one("SELECT pe, eps, nav, dividend_yield, market_cap, free_float FROM company_info WHERE code=%s", (code,))
        sig = db.query_one("SELECT score, label, rsi, sma20, sma50, sma200, ret_1m, ret_3m, high52, low52, flags "
                           "FROM signals WHERE code=%s ORDER BY trade_date DESC LIMIT 1", (code,))
        facts = {
            "company": inst, "last_close": fc["close"], "date": fc["trade_date"],
            "technical": sig, "fundamentals": info,
            "forecast_ranges": [{k: h[k] for k in ("label", "low50", "high50", "low68", "high68", "median")} for h in fc["horizons"]],
            "direction_model": "not reliable (no edge in tests); do not state a direction",
            "trade_plan": {k: plan.get(k) for k in ("signal", "reasons", "buy_zone", "stop", "t1", "t2", "rr2",
                                                   "win_rate", "ev_pct", "support", "resistance")} if plan else None,
            "recent_news": [{k: n[k] for k in ("news_date", "summary", "tags")} | {"ai_impact": n.get("ai_impact")}
                            for n in db.query("SELECT news_date, summary, tags, ai_impact FROM news WHERE code=%s "
                                              "ORDER BY news_date DESC LIMIT 8", (code,))],
        }
        note = llm.analyst_note(code, facts)
        llm.save_note(code, fc["trade_date"], note)
        return jsonify(note=llm.cached_note(code, fc["trade_date"]))

    @app.post("/api/ai/profile/<code>")
    def ai_profile(code: str):
        if not llm.status()["available"]:
            abort(503, "Local AI not available (start Ollama)")
        llm.company_profile(code.strip().upper())
        return jsonify(profile=llm.get_profiles([code.strip().upper()]).get(code.strip().upper()))

    @app.get("/api/ai/portfolio")
    def ai_portfolio_get():
        return jsonify(review=db.query_one(
            "SELECT note, model, created_at FROM ai_notes WHERE code='__PORTFOLIO__' ORDER BY trade_date DESC LIMIT 1"))

    @app.post("/api/ai/portfolio")
    def ai_portfolio_post():
        if not llm.status()["available"]:
            abort(503, "Local AI not available (start Ollama)")
        try:
            return jsonify(review=llm.portfolio_review())
        except ValueError as e:
            abort(400, str(e))

    @app.get("/api/ai/status")
    def ai_status():
        pending = len(llm.pending_news()) if llm.status()["available"] else None
        return jsonify(**llm.status(), pending_news=pending,
                       scored=db.scalar("SELECT COUNT(*) FROM news WHERE ai_impact IS NOT NULL AND ai_reason <> 'Routine notice'"),
                       profiles=db.scalar("SELECT COUNT(*) FROM ai_profiles"),
                       profiles_pending=len(llm.profile_queue()) if llm.status()["available"] else None)

    @app.get("/api/accuracy")
    def accuracy():
        days = min(400, max(7, int(request.args.get("days", 120))))
        ov = predict.accuracy_overview(days)
        h = int(request.args.get("h", 1))
        d = request.args.get("date")
        check_date = date.fromisoformat(d) if d else ov["latest"]
        details = predict.accuracy_details(check_date, h) if check_date else {"rows": []}
        return jsonify(
            **ov, details=details, horizons=predict.HORIZONS,
            watchlist=[w["code"] for w in db.query("SELECT code FROM watchlist")],
            holdings=[x["code"] for x in portfolio.summary()["holdings"]],
        )

    @app.get("/api/news")
    def news_list():
        code = (request.args.get("code") or "").strip().upper() or None
        limit = min(200, max(1, int(request.args.get("limit", 40))))
        return jsonify(news=news.latest(limit, code=code), tag_labels=news.TAG_LABELS,
                       fetched_at=db.get_state("news_fetched_at"))

    # ------------------------------------------------------------------ watchlist

    @app.get("/api/watchlist")
    def watchlist_get():
        rows = db.query(
            "SELECT w.code, w.note, w.added_at, i.name, i.sector, i.category, q.ltp, q.close, q.ycp, q.change_pct, "
            "q.volume, q.value_mn, q.trade_date FROM watchlist w LEFT JOIN instruments i ON i.code=w.code "
            "LEFT JOIN live_quotes q ON q.code=w.code ORDER BY w.code"
        )
        sigs = _latest_signals("score, label, rsi, vol_ratio, flags, high52, low52, ret_1w, ret_1m, sma50, sma200")
        for r in rows:
            s = sigs.get(r["code"]) or {}
            r.update({k: v for k, v in s.items() if k != "code"})
            r["flags"] = analysis.parse_flags(s.get("flags"))
        return jsonify(rows=rows, flag_labels=analysis.FLAG_LABELS)

    @app.post("/api/watchlist")
    def watchlist_add():
        b = _body()
        code = str(b.get("code") or "").strip().upper()
        if not db.query_one("SELECT code FROM instruments WHERE code=%s", (code,)):
            abort(400, f"Unknown trading code {code}")
        db.execute(
            "INSERT INTO watchlist (code, note, added_at) VALUES (%s, %s, NOW()) ON DUPLICATE KEY UPDATE note=VALUES(note)",
            (code, (b.get("note") or "")[:255]),
        )
        return jsonify(ok=True)

    @app.delete("/api/watchlist/<code>")
    def watchlist_delete(code: str):
        db.execute("DELETE FROM watchlist WHERE code=%s", (code.strip().upper(),))
        return jsonify(ok=True)

    # ------------------------------------------------------------------ portfolio

    @app.get("/api/portfolio")
    def portfolio_get():
        return jsonify(
            summary=portfolio.summary(),
            transactions=portfolio.list_transactions(),
            commission_rate=settings.get_float("commission_rate"),
        )

    @app.post("/api/transactions")
    def tx_add():
        return jsonify(id=portfolio.add_transaction(_body()))

    @app.delete("/api/transactions/<int:tx_id>")
    def tx_delete(tx_id: int):
        portfolio.delete_transaction(tx_id)
        return jsonify(ok=True)

    # ------------------------------------------------------------------ alerts

    @app.get("/api/alerts")
    def alerts_get():
        rows = db.query("SELECT a.*, i.name FROM alerts a LEFT JOIN instruments i ON i.code=a.code ORDER BY a.active DESC, a.code")
        codes = sorted({a["code"] for a in rows})
        prices = portfolio.latest_prices(codes)
        for a in rows:
            a["description"] = alerts.describe(a["kind"], a["threshold"])
            a["price"] = (prices.get(a["code"]) or {}).get("price")
        return jsonify(
            alerts=rows,
            kinds=alerts.KINDS,
            events=db.query("SELECT * FROM alert_events ORDER BY id DESC LIMIT 100"),
        )

    @app.post("/api/alerts")
    def alerts_add():
        b = _body()
        code = str(b.get("code") or "").strip().upper()
        kind = str(b.get("kind") or "")
        if not db.query_one("SELECT code FROM instruments WHERE code=%s", (code,)):
            abort(400, f"Unknown trading code {code}")
        if kind not in alerts.KINDS:
            abort(400, "Unknown alert type")
        threshold = None
        if alerts.KINDS[kind]["needs"]:
            try:
                threshold = float(b.get("threshold"))
            except (TypeError, ValueError):
                abort(400, "This alert needs a numeric threshold")
        new_id = db.execute(
            "INSERT INTO alerts (code, kind, threshold, note, repeat_daily, active, created_at) VALUES (%s,%s,%s,%s,%s,1,NOW())",
            (code, kind, threshold, (b.get("note") or "")[:255], 1 if b.get("repeat", True) else 0),
        )
        return jsonify(id=new_id)

    @app.delete("/api/alerts/<int:alert_id>")
    def alerts_delete(alert_id: int):
        db.execute("DELETE FROM alerts WHERE id=%s", (alert_id,))
        return jsonify(ok=True)

    @app.post("/api/alerts/<int:alert_id>/toggle")
    def alerts_toggle(alert_id: int):
        db.execute("UPDATE alerts SET active=1-active WHERE id=%s", (alert_id,))
        return jsonify(ok=True)

    # ------------------------------------------------------------------ reports

    @app.get("/api/reports")
    def reports_list():
        return jsonify(reports=report.list_reports())

    @app.get("/api/reports/<d>")
    def report_get(d: str):
        try:
            rep = report.get(date.fromisoformat(d))
        except ValueError:
            abort(400, "Invalid date")
        if not rep:
            abort(404, f"No report for {d}")
        return jsonify(report=rep, flag_labels=analysis.FLAG_LABELS)

    # ------------------------------------------------------------------ settings & jobs

    @app.get("/api/settings")
    def settings_get():
        c = config.load()
        return jsonify(
            settings=settings.get_all(),
            mysql={"host": c.get("mysql", "host"), "port": c.get("mysql", "port"), "user": c.get("mysql", "user"), "database": c.get("mysql", "database")},
            monitor=dict(c["monitor"]),
        )

    @app.post("/api/settings")
    def settings_post():
        return jsonify(settings=settings.update(_body()))

    @app.post("/api/notify/test")
    def notify_test():
        sent = notifier.notify("BD Stock Tracker", "Test notification: alerts and the daily report will arrive like this.")
        return jsonify(sent=sent)

    @app.get("/api/jobs")
    def jobs_get():
        return jsonify(jobs=JOBS.status())

    @app.post("/api/jobs/<name>")
    def jobs_start(name: str):
        b = _body()
        if name == "refresh":
            snap = collector.refresh_live()
            return jsonify(started=True, state=snap.market_state())
        if name == "daily":
            return jsonify(started=JOBS.start("daily", collector.run_daily, force=bool(b.get("force"))))
        if name == "backfill":
            return jsonify(started=JOBS.start("backfill", collector.backfill))
        if name == "predict":
            return jsonify(started=JOBS.start("predict", lambda progress=None: predict.run()))
        if name == "simulate":
            return jsonify(started=JOBS.start(
                "simulate", lambda progress=None: {"forecasts": predict.simulate_history(), "checks": predict.run_checks()}))
        if name == "news":
            return jsonify(started=JOBS.start("news", lambda progress=None: news.fetch_latest()))
        if name == "ai_profiles":
            return jsonify(started=JOBS.start("ai_profiles", lambda progress=None: llm.profile_batch(max_minutes=180,
                                                                                                    progress=progress)))
        if name == "ai_news":
            return jsonify(started=JOBS.start("ai_news", lambda progress=None: llm.score_news(days=120, max_minutes=240,
                                                                                              progress=progress)))
        if name == "analyze":
            d = db.scalar("SELECT MAX(trade_date) FROM daily_prices")
            if not d:
                abort(400, "No price data yet")
            return jsonify(started=JOBS.start("analyze", lambda progress=None: analysis.analyze_all(d)))
        abort(404, "Unknown job")

    if monitor:
        monitor.start()
        app.extensions["market_monitor"] = monitor
    return app


def serve(open_browser: bool = False, monitor: bool = True) -> None:
    from waitress import serve as waitress_serve

    db.init_db()
    app = create_app(start_monitor=monitor)
    host, port = config.get("server", "host"), config.getint("server", "port")
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    log.info("BD Stock Tracker running at %s", url)
    print(f"BD Stock Tracker running at {url}  (Ctrl+C to stop)")
    waitress_serve(app, host=host, port=port, threads=8, ident="BDStockTracker")
