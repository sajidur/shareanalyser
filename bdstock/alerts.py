"""Price / indicator alerts. Price and %-change alerts are checked intraday (every
refresh while the market is open); all alerts are checked again at end of day.
Each alert fires at most once per trading day."""
from __future__ import annotations

import logging
from datetime import date, datetime

from . import analysis, db, notifier

log = logging.getLogger(__name__)

KINDS: dict[str, dict] = {
    "price_above": {"label": "Price rises to or above", "unit": "Tk", "intraday": True, "needs": True},
    "price_below": {"label": "Price falls to or below", "unit": "Tk", "intraday": True, "needs": True},
    "pct_up": {"label": "Day gain reaches", "unit": "%", "intraday": True, "needs": True},
    "pct_down": {"label": "Day loss reaches", "unit": "%", "intraday": True, "needs": True},
    "rsi_below": {"label": "RSI(14) falls below", "unit": "", "intraday": False, "needs": True},
    "rsi_above": {"label": "RSI(14) rises above", "unit": "", "intraday": False, "needs": True},
    "volume_spike": {"label": "Volume reaches N × 20-day average", "unit": "×", "intraday": False, "needs": True},
    "score_above": {"label": "Technical score reaches", "unit": "", "intraday": False, "needs": True},
    "score_below": {"label": "Technical score drops to", "unit": "", "intraday": False, "needs": True},
    "new_52w_high": {"label": "Makes a new 52-week high", "unit": "", "intraday": False, "needs": False},
    "new_52w_low": {"label": "Makes a new 52-week low", "unit": "", "intraday": False, "needs": False},
    "golden_cross": {"label": "Golden cross (50 DMA crosses above 200 DMA)", "unit": "", "intraday": False, "needs": False},
    "macd_bull_cross": {"label": "MACD bullish crossover", "unit": "", "intraday": False, "needs": False},
}


def describe(kind: str, threshold: float | None) -> str:
    k = KINDS.get(kind, {"label": kind, "unit": ""})
    if not k.get("needs"):
        return k["label"]
    unit = k["unit"]
    t = f"{threshold:g}" if threshold is not None else "?"
    if unit == "Tk":
        return f"{k['label']} Tk {t}"
    if unit == "%":
        return f"{k['label']} {t}%"
    if unit == "×":
        return f"Volume reaches {t}× the 20-day average"
    return f"{k['label']} {t}"


def evaluate(kind: str, threshold: float | None, ctx: dict) -> tuple[bool, float | None, str]:
    """Return (triggered, observed value, message)."""
    price, pct = ctx.get("price"), ctx.get("change_pct")
    flags = ctx.get("flags") or set()
    t = threshold
    if kind == "price_above" and price is not None and t is not None and price >= t:
        return True, price, f"Price Tk {price:,.2f} is at/above Tk {t:,.2f}"
    if kind == "price_below" and price is not None and t is not None and price <= t:
        return True, price, f"Price Tk {price:,.2f} is at/below Tk {t:,.2f}"
    if kind == "pct_up" and pct is not None and t is not None and pct >= abs(t):
        return True, pct, f"Up {pct:+.2f}% today (alert at +{abs(t):g}%)"
    if kind == "pct_down" and pct is not None and t is not None and pct <= -abs(t):
        return True, pct, f"Down {pct:+.2f}% today (alert at -{abs(t):g}%)"
    rsi, vr, score = ctx.get("rsi"), ctx.get("vol_ratio"), ctx.get("score")
    if kind == "rsi_below" and rsi is not None and t is not None and rsi < t:
        return True, rsi, f"RSI {rsi:.1f} is below {t:g}"
    if kind == "rsi_above" and rsi is not None and t is not None and rsi > t:
        return True, rsi, f"RSI {rsi:.1f} is above {t:g}"
    if kind == "volume_spike" and vr is not None and t is not None and vr >= t:
        return True, vr, f"Volume {vr:.1f}× the 20-day average"
    if kind == "score_above" and score is not None and t is not None and score >= t:
        return True, score, f"Technical score {score:+d} (alert at {t:+g})"
    if kind == "score_below" and score is not None and t is not None and score <= t:
        return True, score, f"Technical score {score:+d} (alert at {t:+g})"
    if kind in ("new_52w_high", "new_52w_low", "golden_cross", "macd_bull_cross") and kind in flags:
        return True, price, KINDS[kind]["label"] + (f" at Tk {price:,.2f}" if price else "")
    return False, None, ""


def _pending_alerts(trade_date: date, intraday_only: bool) -> list[dict]:
    rows = db.query(
        "SELECT * FROM alerts WHERE active=1 AND (last_triggered_on IS NULL OR last_triggered_on < %s)",
        (trade_date,),
    )
    if intraday_only:
        rows = [a for a in rows if KINDS.get(a["kind"], {}).get("intraday")]
    return rows


def _fire(alert: dict, value: float | None, message: str, trade_date: date) -> dict:
    db.execute(
        "INSERT INTO alert_events (alert_id, code, kind, message, value, trade_date, triggered_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (alert["id"], alert["code"], alert["kind"], message[:500], value, trade_date, datetime.now()),
    )
    db.execute(
        "UPDATE alerts SET last_triggered_on=%s, active=IF(repeat_daily=1, active, 0) WHERE id=%s",
        (trade_date, alert["id"]),
    )
    return {"alert_id": alert["id"], "code": alert["code"], "kind": alert["kind"], "message": message, "value": value}


def check_intraday(trade_date: date, notify: bool = True) -> list[dict]:
    alerts = _pending_alerts(trade_date, intraday_only=True)
    if not alerts:
        return []
    codes = sorted({a["code"] for a in alerts})
    ph = ",".join(["%s"] * len(codes))
    quotes = {
        q["code"]: q
        for q in db.query(f"SELECT code, ltp, close, change_pct, volume FROM live_quotes WHERE code IN ({ph})", codes)
    }
    events = []
    for a in alerts:
        q = quotes.get(a["code"])
        if not q or not q["volume"]:
            continue  # not traded yet today
        ctx = {"price": q["ltp"] or q["close"], "change_pct": q["change_pct"]}
        hit, value, msg = evaluate(a["kind"], a["threshold"], ctx)
        if hit:
            ev = _fire(a, value, msg, trade_date)
            events.append(ev)
            if notify:
                notifier.notify(f"{a['code']} alert", msg + (f"\n{a['note']}" if a.get("note") else ""), f"stock/{a['code']}")
    return events


def check_eod(trade_date: date) -> list[dict]:
    alerts = _pending_alerts(trade_date, intraday_only=False)
    if not alerts:
        return []
    codes = sorted({a["code"] for a in alerts})
    ph = ",".join(["%s"] * len(codes))
    prices = {
        r["code"]: r
        for r in db.query(
            f"SELECT code, close, change_pct, volume FROM daily_prices WHERE trade_date=%s AND code IN ({ph})",
            [trade_date, *codes],
        )
    }
    sigs = {
        r["code"]: r
        for r in db.query(
            f"SELECT code, score, rsi, vol_ratio, flags FROM signals WHERE trade_date=%s AND code IN ({ph})",
            [trade_date, *codes],
        )
    }
    events = []
    for a in alerts:
        p = prices.get(a["code"])
        if not p:
            continue
        s = sigs.get(a["code"]) or {}
        is_price_kind = a["kind"] in ("price_above", "price_below", "pct_up", "pct_down")
        if is_price_kind and not p["volume"]:
            continue
        ctx = {
            "price": p["close"],
            "change_pct": p["change_pct"],
            "rsi": s.get("rsi"),
            "vol_ratio": s.get("vol_ratio"),
            "score": s.get("score"),
            "flags": set(analysis.parse_flags(s.get("flags"))),
        }
        hit, value, msg = evaluate(a["kind"], a["threshold"], ctx)
        if hit:
            events.append(_fire(a, value, msg, trade_date))
    log.info("End-of-day alerts for %s: %d triggered", trade_date, len(events))
    return events
