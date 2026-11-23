"""End-of-day market report: market summary, movers, sectors, signals, watchlist,
portfolio and triggered alerts. Stored as JSON in `daily_reports`."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from . import analysis, db, news, portfolio, predict

log = logging.getLogger(__name__)

HIGHLIGHT_FLAGS = [
    "new_52w_high", "golden_cross", "macd_bull_cross", "vol_spike", "oversold",
    "overbought", "new_52w_low", "death_cross", "upper_circuit",
]


def sector_performance(rows: list[dict]) -> list[dict]:
    """rows need: sector, change_pct, value_mn, volume."""
    agg: dict[str, dict] = defaultdict(lambda: {"count": 0, "traded": 0, "advanced": 0, "declined": 0, "unchanged": 0, "sum_change": 0.0, "turnover_mn": 0.0})
    for r in rows:
        s = agg[r.get("sector") or "Other"]
        s["count"] += 1
        if not r.get("volume"):
            continue
        pct = r.get("change_pct") or 0.0
        s["traded"] += 1
        s["sum_change"] += pct
        s["turnover_mn"] += r.get("value_mn") or 0.0
        s["advanced" if pct > 0 else "declined" if pct < 0 else "unchanged"] += 1
    total_turnover = sum(s["turnover_mn"] for s in agg.values()) or 1.0
    out = []
    for name, s in agg.items():
        if not s["traded"]:
            continue
        out.append(
            {
                "sector": name,
                "count": s["count"],
                "traded": s["traded"],
                "advanced": s["advanced"],
                "declined": s["declined"],
                "unchanged": s["unchanged"],
                "avg_change": round(s["sum_change"] / s["traded"], 2),
                "turnover_mn": round(s["turnover_mn"], 2),
                "turnover_share": round(s["turnover_mn"] / total_turnover * 100, 2),
            }
        )
    return sorted(out, key=lambda x: -x["avg_change"])


def _slim(r: dict, sig: dict | None = None) -> dict:
    d = {
        "code": r["code"],
        "name": r.get("name"),
        "close": r.get("close"),
        "change_pct": r.get("change_pct"),
        "value_mn": r.get("value_mn"),
        "volume": r.get("volume"),
        "sector": r.get("sector"),
        "category": r.get("category"),
    }
    if sig:
        d.update({"score": sig.get("score"), "label": sig.get("label"), "rsi": sig.get("rsi")})
    return d


def build(trade_date: date) -> dict[str, Any]:
    indices = {
        r["code"]: {"value": r["value"], "change": r["change_abs"], "pct": r["change_pct"]}
        for r in db.query("SELECT * FROM index_daily WHERE trade_date=%s", (trade_date,))
    }
    md = db.query_one("SELECT * FROM market_daily WHERE trade_date=%s", (trade_date,)) or {}
    prev = db.query_one("SELECT * FROM market_daily WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 1", (trade_date,)) or {}
    all_rows = db.query(
        "SELECT p.code, p.close, p.ycp, p.change_pct, p.volume, p.value_mn, p.trades, "
        "i.name, i.sector, i.category, i.asset_type FROM daily_prices p JOIN instruments i ON i.code=p.code "
        "WHERE p.trade_date=%s",
        (trade_date,),
    )
    rows = [r for r in all_rows if not analysis.is_debt(r["asset_type"])]  # shares + mutual funds
    sigs = {s["code"]: s for s in db.query("SELECT * FROM signals WHERE trade_date=%s", (trade_date,))}
    traded = [r for r in rows if r["volume"]]
    by_code = {r["code"]: r for r in all_rows}

    highlights: dict[str, list] = {}
    for flag in HIGHLIGHT_FLAGS:
        hits = [c for c, s in sigs.items() if flag in analysis.parse_flags(s.get("flags"))
                and c in by_code and by_code[c]["volume"]]
        hits.sort(key=lambda c: -(by_code[c].get("value_mn") or 0))
        if hits:
            highlights[flag] = [_slim(by_code[c], sigs.get(c)) for c in hits[:15]]

    watch = []
    for w in db.query("SELECT code FROM watchlist ORDER BY code"):
        r = by_code.get(w["code"])
        if r:
            s = sigs.get(w["code"]) or {}
            item = _slim(r, s)
            item["flags"] = analysis.parse_flags(s.get("flags"))
            watch.append(item)

    events = db.query(
        "SELECT code, kind, message, value, triggered_at FROM alert_events WHERE trade_date=%s ORDER BY triggered_at",
        (trade_date,),
    )
    pf = portfolio.summary()
    label_counts: dict[str, int] = defaultdict(int)
    for s in sigs.values():
        label_counts[s["label"]] += 1

    turnover = md.get("turnover_mn")
    prev_turnover = prev.get("turnover_mn")
    checks = []
    for c in db.query("SELECT * FROM prediction_checks WHERE check_date=%s ORDER BY horizon, source", (trade_date,)):
        checks.append({"horizon": c["horizon"], "source": c["source"], "made_on": c["made_on"], "n": c["n"],
                       "in50": c.get("in50"), "width50": c.get("width50"),
                       "in68": c["in68"], "in90": c["in90"], "mae_pct": c["mae_pct"],
                       "direction": round(c["dir_hits"] / c["dir_n"] * 100, 1) if c["dir_n"] else None})
    my_codes = sorted({w["code"] for w in watch} | {h["code"] for h in pf["holdings"]})
    news_today = [n for n in news.latest(200, codes=my_codes) if n["news_date"] == trade_date] if my_codes else []
    news_count = db.scalar("SELECT COUNT(*) FROM news WHERE news_date=%s", (trade_date,))
    picks_short, picks_long = [], []
    try:
        if predict.latest_date() == trade_date:
            pk = predict.picks(10)
            picks_short = [{k: x.get(k) for k in ("code", "name", "close", "score", "label", "stop", "target", "reward_risk")}
                           for x in pk.get("short", [])]
            picks_long = [{k: x.get(k) for k in ("code", "name", "close", "score", "pe", "dividend_yield", "eps_growth")}
                          for x in pk.get("long", [])]
    except Exception:  # the report must never fail because of the model
        log.exception("Could not add picks to the report")
    return {
        "forecast_check": checks,
        "news_mine": [{k: n[k] for k in ("code", "news_time", "type", "summary", "tags", "tone")} for n in news_today[:20]],
        "news_count": news_count,
        "picks_short": picks_short,
        "picks_long": picks_long,
        "date": trade_date.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "indices": indices,
        "market": {
            "turnover_mn": turnover,
            "turnover_change_pct": round((turnover / prev_turnover - 1) * 100, 2) if turnover and prev_turnover else None,
            "volume": md.get("volume"),
            "trades": md.get("trades"),
            "market_cap": md.get("market_cap"),
            "advanced": md.get("advanced"),
            "declined": md.get("declined"),
            "unchanged": md.get("unchanged"),
            "traded": sum(1 for r in all_rows if r["volume"]),
        },
        "top_gainers": [_slim(r, sigs.get(r["code"])) for r in sorted(traded, key=lambda r: -(r["change_pct"] or 0))[:10]],
        "top_losers": [_slim(r, sigs.get(r["code"])) for r in sorted(traded, key=lambda r: (r["change_pct"] or 0))[:10]],
        "top_turnover": [_slim(r, sigs.get(r["code"])) for r in sorted(traded, key=lambda r: -(r["value_mn"] or 0))[:10]],
        "sectors": sector_performance(rows),
        "highlights": highlights,
        "signal_breadth": dict(label_counts),
        "watchlist": watch,
        "portfolio": pf["totals"],
        "portfolio_holdings": [
            {k: h[k] for k in ("code", "qty", "price", "change_pct", "market_value", "unrealized", "unrealized_pct", "day_change")}
            for h in pf["holdings"]
        ],
        "alerts": events,
    }


def headline(rep: dict) -> tuple[str, str]:
    d = date.fromisoformat(rep["date"])
    dsex = rep["indices"].get("DSEX") or {}
    if dsex.get("value"):
        title = f"DSE {d:%d %b}: DSEX {dsex['value']:,.0f} ({(dsex.get('pct') or 0):+.2f}%)"
    else:
        title = f"DSE market report {d:%d %b %Y}"
    m = rep["market"]
    lines = []
    if m.get("turnover_mn"):
        chg = m.get("turnover_change_pct")
        lines.append(
            f"Turnover Tk {m['turnover_mn'] / 10:,.0f} cr"
            + (f" ({chg:+.0f}% vs prev)" if chg is not None else "")
            + (f" · ▲{m['advanced']} ▼{m['declined']} ={m['unchanged']}" if m.get("advanced") is not None else "")
        )
    pf = rep.get("portfolio") or {}
    if pf.get("positions"):
        lines.append(
            f"Portfolio Tk {pf['market_value']:,.0f} ({pf['day_change']:+,.0f} today"
            + (f", {pf['day_change_pct']:+.2f}%" if pf.get("day_change_pct") is not None else "")
            + ")"
        )
    if rep.get("watchlist"):
        up = sum(1 for w in rep["watchlist"] if (w.get("change_pct") or 0) > 0)
        down = sum(1 for w in rep["watchlist"] if (w.get("change_pct") or 0) < 0)
        lines.append(f"Watchlist: {up} up, {down} down")
    fc = next((c for c in rep.get("forecast_check") or [] if c["horizon"] == 1 and c["source"] == "live"), None) \
        or next((c for c in rep.get("forecast_check") or [] if c["horizon"] == 1), None)
    if fc and fc.get("in50") is not None:
        lines.append(f"Forecast check: {fc['in50']:.0f}% inside the 50% range (target 50%), {fc['in68']:.0f}% inside 68%")
    elif fc:
        lines.append(f"Forecast check: {fc['in68']:.0f}% of next-session ranges held (target 68%)")
    if rep.get("news_mine"):
        lines.append("News: " + ", ".join(sorted({n["code"] for n in rep["news_mine"]}))[:120])
    if rep.get("picks_short"):
        lines.append("Short-term ideas: " + ", ".join(x["code"] for x in rep["picks_short"][:5]))
    if rep.get("alerts"):
        lines.append(
            f"{len(rep['alerts'])} alert(s): " + "; ".join(f"{e['code']} {e['message']}" for e in rep["alerts"][:3])
        )
    return title, "\n".join(lines)


def save(trade_date: date, rep: dict) -> None:
    db.execute(
        "INSERT INTO daily_reports (report_date, data, created_at) VALUES (%s, %s, NOW()) "
        "ON DUPLICATE KEY UPDATE data=VALUES(data)",
        (trade_date, json.dumps(rep, default=str)),
    )


def was_notified(trade_date: date) -> bool:
    return bool(db.scalar("SELECT notified FROM daily_reports WHERE report_date=%s", (trade_date,)))


def mark_notified(trade_date: date) -> None:
    db.execute("UPDATE daily_reports SET notified=1 WHERE report_date=%s", (trade_date,))


def exists(trade_date: date) -> bool:
    return db.scalar("SELECT COUNT(*) FROM daily_reports WHERE report_date=%s", (trade_date,)) > 0


def get(trade_date: date) -> dict | None:
    row = db.query_one("SELECT data FROM daily_reports WHERE report_date=%s", (trade_date,))
    return json.loads(row["data"]) if row and row["data"] else None


def list_reports(limit: int = 120) -> list[dict]:
    out = []
    for r in db.query("SELECT report_date, data FROM daily_reports ORDER BY report_date DESC LIMIT %s", (limit,)):
        try:
            rep = json.loads(r["data"])
        except (TypeError, ValueError):
            continue
        title, _ = headline(rep)
        dsex = rep.get("indices", {}).get("DSEX") or {}
        out.append(
            {
                "date": r["report_date"],
                "title": title,
                "dsex": dsex.get("value"),
                "dsex_pct": dsex.get("pct"),
                "turnover_mn": rep.get("market", {}).get("turnover_mn"),
                "alerts": len(rep.get("alerts") or []),
            }
        )
    return out
