"""Data collection jobs: live refresh, end-of-day storage + analysis, history backfill."""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable

from . import config, db, dse_client as dse

log = logging.getLogger(__name__)

Progress = Callable[[int, int, str], None] | None

PRICE_UPSERT = """INSERT INTO daily_prices
    (code, trade_date, open, high, low, close, ltp, ycp, volume, value_mn, trades, change_pct, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close),
    ltp=COALESCE(VALUES(ltp), ltp), ycp=COALESCE(VALUES(ycp), ycp), volume=VALUES(volume),
    value_mn=COALESCE(VALUES(value_mn), value_mn), trades=VALUES(trades),
    change_pct=COALESCE(VALUES(change_pct), change_pct), source=VALUES(source)"""

MARKET_UPSERT = """INSERT INTO market_daily
    (trade_date, turnover_mn, volume, trades, advanced, declined, unchanged, market_cap)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE turnover_mn=COALESCE(VALUES(turnover_mn), turnover_mn),
    volume=COALESCE(VALUES(volume), volume), trades=COALESCE(VALUES(trades), trades),
    advanced=COALESCE(VALUES(advanced), advanced), declined=COALESCE(VALUES(declined), declined),
    unchanged=COALESCE(VALUES(unchanged), unchanged), market_cap=COALESCE(VALUES(market_cap), market_cap)"""

INDEX_UPSERT = """INSERT INTO index_daily (code, trade_date, value, change_abs, change_pct)
VALUES (%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE value=VALUES(value), change_abs=COALESCE(VALUES(change_abs), change_abs),
    change_pct=COALESCE(VALUES(change_pct), change_pct)"""


def _int(v) -> int | None:
    n = dse.num(v)
    return int(n) if n is not None else None


# ------------------------------------------------------------------------ live refresh

def store_live(snap: dse.Snapshot) -> None:
    now = datetime.now()
    db.executemany(
        "INSERT INTO instruments (code, sector, category, board, asset_type, updated_at) VALUES (%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE sector=COALESCE(VALUES(sector), sector), category=COALESCE(VALUES(category), category), "
        "board=COALESCE(VALUES(board), board), asset_type=COALESCE(VALUES(asset_type), asset_type), updated_at=VALUES(updated_at)",
        [(q["code"], q["sector"], q["category"], q["board"], q["asset_type"], now) for q in snap.quotes],
    )
    db.executemany(
        "INSERT INTO live_quotes (code, trade_date, ltp, ycp, open, high, low, close, volume, value_mn, trades, change_pct, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE trade_date=VALUES(trade_date), "
        "ltp=VALUES(ltp), ycp=VALUES(ycp), open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close), "
        "volume=VALUES(volume), value_mn=VALUES(value_mn), trades=VALUES(trades), change_pct=VALUES(change_pct), "
        "updated_at=VALUES(updated_at)",
        [
            (q["code"], snap.trade_date, q["ltp"], q["ycp"], q["open"], q["high"], q["low"], q["close"],
             q["volume"], q["value_mn"], q["trades"], q["change_pct"], now)
            for q in snap.quotes
        ],
    )
    db.set_state("market", snap.market_state())


def refresh_live() -> dse.Snapshot:
    snap = dse.fetch_snapshot()
    store_live(snap)
    log.info("Snapshot stored: %d instruments, trade date %s, open=%s", len(snap.quotes), snap.trade_date, snap.is_open)
    return snap


# ------------------------------------------------------------------------ end of day

def eod_ready(snap: dse.Snapshot, now: datetime | None = None) -> bool:
    """True once the session in the snapshot is finished (plus a safety delay)."""
    now = now or datetime.now()
    if snap.is_open:
        return False
    if snap.trade_date < now.date():
        return True
    today = snap.session.get("today") or {}
    post = str(today.get("postClosing") or snap.session.get("closing") or "14:10")
    try:
        hh, mm = (int(x) for x in post.split(":")[:2])
    except ValueError:
        hh, mm = 14, 10
    ready_at = datetime.combine(snap.trade_date, dtime(hh, mm)) + timedelta(minutes=config.getint("monitor", "eod_delay_minutes"))
    return now >= ready_at


def eod_done(trade_date: date) -> bool:
    return bool(db.scalar("SELECT COUNT(*) FROM daily_reports WHERE report_date=%s", (trade_date,)))


def store_eod(snap: dse.Snapshot) -> int:
    d = snap.trade_date
    n = db.executemany(
        PRICE_UPSERT,
        [
            (q["code"], d, q["open"], q["high"], q["low"], q["close"], q["ltp"], q["ycp"], q["volume"],
             q["value_mn"], q["trades"], q["change_pct"], "snapshot")
            for q in snap.quotes
            if q["close"]
        ],
    )
    db.executemany(
        INDEX_UPSERT,
        [
            (ix.get("key"), d, dse.num(ix.get("value")), dse.num(ix.get("change")), dse.num(ix.get("percent")))
            for ix in snap.indices
            if ix.get("key") and dse.num(ix.get("value"))
        ],
    )
    rows = []
    for t in snap.daily_totals:
        try:
            td = date.fromisoformat(str(t.get("date"))[:10])
        except ValueError:
            continue
        rows.append((td, dse.num(t.get("turnover")), _int(t.get("volume")), _int(t.get("trades")),
                     _int(t.get("advanced")), _int(t.get("declined")), _int(t.get("unchanged")), None))
    b, tot = snap.breadth, snap.totals
    rows.append((d, dse.num(tot.get("turnover")), _int(tot.get("volume")), _int(tot.get("trades")),
                 _int(b.get("advanced")), _int(b.get("declined")), _int(b.get("unchanged")), dse.num(tot.get("marketCap"))))
    db.executemany(MARKET_UPSERT, rows)
    db.execute(
        "INSERT INTO synced_dates (trade_date, source, synced_at) VALUES (%s, 'snapshot', NOW()) "
        "ON DUPLICATE KEY UPDATE source='snapshot', synced_at=NOW()",
        (d,),
    )
    log.info("Stored end-of-day prices for %s (%d rows)", d, n)
    return n


def update_recent_market() -> int:
    """Index values + market totals for the last ~30 sessions (fills index history)."""
    rows = dse.fetch_recent_market()
    idx_rows, mkt_rows = [], []
    prev: dict[str, float | None] = {}
    for r in rows:
        for code in ("DSEX", "DS30", "DSES"):
            v = r.get(code)
            if not v:
                continue
            p = prev.get(code)
            idx_rows.append((code, r["trade_date"], v, (v - p) if p else None, ((v / p - 1) * 100) if p else None))
            prev[code] = v
        mkt_rows.append((r["trade_date"], r["turnover_mn"], _int(r["volume"]), _int(r["trades"]), None, None, None, r["market_cap"]))
    db.executemany(INDEX_UPSERT, idx_rows)
    db.executemany(MARKET_UPSERT, mkt_rows)
    return len(rows)


def update_holidays() -> int:
    """Keep DSE's published holiday list (by year) for trading-day calendars."""
    h = dse.fetch_holidays()
    if not h["holidays"]:
        return 0
    state = db.get_state("holidays") or {}
    state[str(h["year"])] = h["holidays"]
    db.set_state("holidays", state)
    return len(h["holidays"])


def update_pe() -> int:
    pe = dse.fetch_pe()
    db.executemany("UPDATE instruments SET trailing_pe=%s WHERE code=%s", [(v, k) for k, v in pe.items()])
    return len(pe)


def gap_dates(lookback_days: int = 60) -> list[date]:
    """Recent trading days (known from DSE's market totals) with no stored full-market prices."""
    rows = db.query(
        "SELECT m.trade_date FROM market_daily m LEFT JOIN synced_dates s ON s.trade_date=m.trade_date "
        "WHERE s.trade_date IS NULL AND m.trade_date >= CURDATE() - INTERVAL %s DAY ORDER BY m.trade_date",
        (lookback_days,),
    )
    return [r["trade_date"] for r in rows]


def _safe(fn: Callable, label: str):
    try:
        return fn()
    except Exception as e:
        log.warning("%s failed: %s", label, e)
        return None


def run_daily(force: bool = False, notify: bool = True, progress: Progress = None) -> dict:
    """The daily monitor: store today's prices, fill gaps, analyse, alert, report, notify."""
    from . import alerts, analysis, news, notifier, predict, report

    with db.named_lock("bdstock_daily") as got:
        if not got:
            return {"status": "skipped", "message": "Another end-of-day run is already in progress."}
        run_id = db.job_start("daily")
        try:
            snap = refresh_live()
            if not force and not eod_ready(snap):
                msg = f"Session for {snap.trade_date} not finished yet (phase: {snap.session.get('phase')}); nothing to do."
                db.job_finish(run_id, "skipped", msg)
                return {"status": "skipped", "message": msg}
            if not snap.any_traded:
                msg = f"No trades in snapshot for {snap.trade_date}; skipped."
                db.job_finish(run_id, "skipped", msg)
                return {"status": "skipped", "message": msg}
            store_eod(snap)
            _safe(update_recent_market, "Recent market history")
            _safe(update_pe, "P/E update")
            _safe(update_holidays, "Holiday list")
            news_codes = _safe(news.fetch_latest, "News") or []
            gaps = gap_dates()
            if gaps:
                log.info("Missing %d trading day(s) (%s ... %s); backfilling history", len(gaps), gaps[0], gaps[-1])
                backfill(progress=progress)
            else:
                # companies with news today: refresh fundamentals + full announcement text
                todays = [r["code"] for r in db.query(
                    "SELECT DISTINCT code FROM news WHERE news_date=%s", (snap.trade_date,))]
                _safe(lambda: refresh_news_companies(sorted(set(news_codes) | set(todays))), "News company refresh")
            analyzed = analysis.analyze_all(snap.trade_date)
            from . import llm, settings as user_settings

            _safe(lambda: llm.score_news(days=120, max_minutes=user_settings.get_float("ai_daily_minutes")),
                  "AI news scoring")
            _safe(lambda: predict.run(snap.trade_date), "Prediction model")
            from . import bdfactors, tradeplan

            _safe(bdfactors.build, "Bangladesh factors")
            _safe(lambda: tradeplan.update_signal_log(snap.trade_date), "Paper-trading log")
            events = alerts.check_eod(snap.trade_date)
            rep = report.build(snap.trade_date)
            report.save(snap.trade_date, rep)
            notified = False
            if notify and not report.was_notified(snap.trade_date):
                title, body = report.headline(rep)
                notifier.notify(title, body, f"reports/{snap.trade_date}")
                report.mark_notified(snap.trade_date)
                notified = True
            msg = f"{snap.trade_date}: analysed {analyzed}, alerts {len(events)}, gaps filled {len(gaps)}"
            db.job_finish(run_id, "ok", msg)
            return {"status": "ok", "trade_date": snap.trade_date.isoformat(), "analyzed": analyzed,
                    "alerts": len(events), "gaps": len(gaps), "notified": notified, "message": msg}
        except Exception as e:
            log.exception("Daily run failed")
            db.job_finish(run_id, "error", str(e))
            raise


# ------------------------------------------------------------------------ history

def save_company(code: str, c: dict) -> None:
    extra = {
        "dividendHistory": c.get("dividendHistory") or [],
        "multiYearFinancials": [
            {k: f.get(k) for k in ("year", "epsBasic", "nav", "profit", "pe", "dividendPct", "dividendYield")}
            for f in (c.get("multiYearFinancials") or [])
            if isinstance(f, dict)
        ],
        "sharePattern": c.get("sharePattern") or [],
        "website": c.get("website"),
        "isin": c.get("isin"),
        "operationalStatus": c.get("operationalStatus"),
        "agmDate": c.get("agmDate"),
        "marketLot": c.get("marketLot"),
        "instrumentType": c.get("instrumentType"),
        "freeFloatMarketCap": c.get("freeFloatMarketCap"),
        "lastPriceUpdate": c.get("lastPriceUpdate"),
    }
    db.execute(
        "INSERT INTO company_info (code, name, pe, eps, nav, dividend_yield, market_cap, free_float, week_high52, "
        "week_low52, face_value, paid_up_capital, listing_year, year_end, extra_json, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) ON DUPLICATE KEY UPDATE name=VALUES(name), "
        "pe=VALUES(pe), eps=VALUES(eps), nav=VALUES(nav), dividend_yield=VALUES(dividend_yield), "
        "market_cap=VALUES(market_cap), free_float=VALUES(free_float), week_high52=VALUES(week_high52), "
        "week_low52=VALUES(week_low52), face_value=VALUES(face_value), paid_up_capital=VALUES(paid_up_capital), "
        "listing_year=VALUES(listing_year), year_end=VALUES(year_end), extra_json=VALUES(extra_json), updated_at=NOW()",
        (
            code, c.get("name"), dse.num(c.get("pe")), dse.num(c.get("eps")), dse.num(c.get("nav")),
            dse.num(c.get("dividendYield")), dse.num(c.get("marketCap")), dse.num(c.get("freeFloat")),
            dse.num(c.get("weekHigh52")), dse.num(c.get("weekLow52")), dse.num(c.get("faceValue")),
            dse.num(c.get("paidUpCapital")), _int(c.get("listingYear")), c.get("yearEnd"),
            json.dumps(extra, default=str),
        ),
    )
    if c.get("name"):
        db.execute(
            "UPDATE instruments SET name=%s, sector=COALESCE(sector, %s), category=COALESCE(category, %s) WHERE code=%s",
            (c.get("name"), c.get("sector"), c.get("category"), code),
        )


def backfill_code(code: str) -> int:
    """Download ~1 year of daily prices + fundamentals for one instrument."""
    data = dse.fetch_company(code)
    db.execute("INSERT IGNORE INTO instruments (code, updated_at) VALUES (%s, NOW())", (code,))
    bars = data["bars"]
    db.executemany(
        PRICE_UPSERT,
        [
            (code, b["trade_date"], b["open"], b["high"], b["low"], b["close"], b["ltp"], b["ycp"],
             b["volume"], b["value_mn"], b["trades"], b["change_pct"], "backfill")
            for b in bars
        ],
    )
    if data["company"]:
        save_company(code, data["company"])
    if data.get("announcements"):
        from . import news

        news.store(code, data["announcements"], "company")
    db.execute("UPDATE instruments SET history_synced_at=NOW() WHERE code=%s", (code,))
    return len(bars)


def refresh_news_companies(codes: list[str], limit: int = 80, delay: float | None = None) -> int:
    """Re-read company pages for stocks that had news today (fresh fundamentals + announcements)."""
    delay = config.getfloat("monitor", "backfill_delay_seconds") if delay is None else delay
    done = 0
    for code in codes[:limit]:
        try:
            backfill_code(code)
            done += 1
        except Exception as e:
            log.warning("Company refresh %s failed: %s", code, e)
        time.sleep(delay)
    return done


def backfill(codes: list[str] | None = None, delay: float | None = None, progress: Progress = None) -> dict:
    """Backfill history. With no codes: every instrument (takes ~15-20 minutes)."""
    full = codes is None
    if full:
        if not db.scalar("SELECT COUNT(*) FROM instruments"):
            refresh_live()
        codes = [r["code"] for r in db.query("SELECT code FROM instruments ORDER BY code")]
    delay = config.getfloat("monitor", "backfill_delay_seconds") if delay is None else delay
    run_id = db.job_start("backfill")
    ok, failed, rows = 0, [], 0
    started = time.time()
    try:
        for i, code in enumerate(codes, 1):
            try:
                rows += backfill_code(code)
                ok += 1
            except Exception as e:
                failed.append(code)
                log.warning("Backfill %s failed: %s", code, e)
            if progress:
                progress(i, len(codes), code)
            if full and (i % 5 == 0 or i == len(codes)):
                # shared with the dashboard, which may be a different process
                db.set_state("backfill_progress", {"done": i, "total": len(codes), "item": code,
                                                   "running": i < len(codes), "at": datetime.now().isoformat(timespec="seconds")})
            if i < len(codes):
                time.sleep(delay)
        if full and ok >= len(codes) * 0.8:
            # Everything in the downloaded window now counts as synced, so the daily
            # job won't backfill again until a new gap appears.
            db.execute(
                "INSERT IGNORE INTO synced_dates (trade_date, source, synced_at) "
                "SELECT DISTINCT trade_date, 'backfill', NOW() FROM daily_prices WHERE trade_date >= CURDATE() - INTERVAL 400 DAY"
            )
            db.execute(
                "INSERT IGNORE INTO synced_dates (trade_date, source, synced_at) "
                "SELECT trade_date, 'backfill', NOW() FROM market_daily WHERE trade_date >= CURDATE() - INTERVAL 400 DAY"
            )
        msg = f"{ok}/{len(codes)} instruments, {rows} price rows in {time.time() - started:.0f}s"
        if failed:
            msg += f"; failed: {', '.join(failed[:20])}" + ("..." if len(failed) > 20 else "")
        db.job_finish(run_id, "ok" if ok else "error", msg)
        log.info("Backfill done: %s", msg)
        return {"ok": ok, "failed": failed, "rows": rows, "message": msg}
    except Exception as e:
        db.job_finish(run_id, "error", str(e))
        if full:
            db.set_state("backfill_progress", {"done": 0, "total": len(codes), "running": False, "error": str(e)})
        raise


def ensure_history(code: str, min_rows: int = 60) -> bool:
    """Fetch history for one stock on demand if we have too little (e.g. new install)."""
    count = db.scalar("SELECT COUNT(*) FROM daily_prices WHERE code=%s", (code,)) or 0
    row = db.query_one("SELECT history_synced_at FROM instruments WHERE code=%s", (code,))
    synced = row and row["history_synced_at"]
    fresh = synced and synced > datetime.now() - timedelta(days=1)
    info_stale = not db.scalar(
        "SELECT COUNT(*) FROM company_info WHERE code=%s AND updated_at > NOW() - INTERVAL 7 DAY", (code,)
    )
    if (count < min_rows or info_stale) and not fresh:
        backfill_code(code)
        return True
    return False
