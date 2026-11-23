"""DSE company announcements: collection, transparent rule-based classification, and
point-in-time news features for the forecast model.

Sources
  * company pages (/company/<CODE>): the ~35 most recent announcements per company (history)
  * the news page (/news): the latest ~50 disclosures across all companies (polled through the day)

Each announcement gets tags and a tone score in [-1, +1]:
  * earnings  - "EPS was Tk. X for <period> as against Tk. Y" -> growth (X - Y) / |Y|, the main signal
  * dividend  - a cash/stock dividend declared (+0.5); "no dividend" (-1)
  * record date, spot market, price limit, AGM, board meeting, credit rating (informational)
  * keyword cues - auditor emphasis of matter / qualified opinion / going concern / downgrade /
    production stoppage / penalty (-0.5); upgrade / commercial operation / expansion (+0.3)
"""
from __future__ import annotations

import bisect
import hashlib
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from . import db, dse_client as dse

log = logging.getLogger(__name__)

_NUM = r"\(?-?[\d,]*\.?\d+\)?"
EPS_RE = re.compile(rf"EPS\s+(?:was|were|is)\s+Tk\.?\s*({_NUM})\s+for\s+(.{{3,80}}?)\s+as\s+against\s+Tk\.?\s*({_NUM})", re.I)
RECORD_RE = re.compile(
    r"record\s+date[^.;]{0,80}?(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{1,2}[\- ][A-Za-z]{3,9}[\- ,]+\d{4}|[A-Za-z]{3,9}\.? \d{1,2}, ?\d{4})",
    re.I,
)
NOCF_RE = re.compile(rf"NOCFPS\s+(?:was|were|is)\s+Tk\.?\s*({_NUM})\s+for\s+(.{{3,80}}?)\s+as\s+against\s+(?:Tk\.?\s*)?({_NUM})", re.I)
NAVPS_RE = re.compile(rf"NAV per share(?:\s+[\w()-]+){{0,4}}?\s+(?:was|were|is)\s+Tk\.?\s*({_NUM})\s+as\s+on\s+(.{{3,40}}?)\s+and\s+(?:Tk\.?\s*)?({_NUM})", re.I)
MFNAV_RE = re.compile(rf"NAV\)?\s+of\s+Tk\.?\s*({_NUM})\s+per\s+unit\s+on\s+the\s+basis\s+of\s+current\s+market\s+price", re.I)
CASH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:final\s+|interim\s+)?cash", re.I)
STOCK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:final\s+|interim\s+)?stock", re.I)
RATE_RE = re.compile(r"rate of (\d+(?:\.\d+)?)\s*%", re.I)
NO_DIV_RE = re.compile(r"\bno dividend\b|not recommended any dividend|did not recommend any dividend|no cash or stock dividend", re.I)

NEGATIVE = ["emphasis of matter", "qualified opinion", "going concern", "downgrad", "non-operational",
            "suspension of production", "production has been suspended", "production remain suspended",
            "fire broke", "default", "penalty", "fined", "z category"]
POSITIVE = ["upgrad", "commercial operation", "expansion", "new product", "capacity enhancement", "export order"]
INFO_TAGS = [("spot", "spot market"), ("price_limit", "price limit"), ("agm", "annual general meeting"),
             ("agm", " agm"), ("board_meeting", "board meeting"), ("rating", "credit rating"),
             ("category", " category")]

TAG_LABELS = {
    "earnings": "Earnings", "dividend": "Dividend", "no_dividend": "No dividend", "record_date": "Record date",
    "spot": "Spot market", "price_limit": "Price limit", "agm": "AGM", "board_meeting": "Board meeting",
    "rating": "Rating", "category": "Category", "negative": "Negative", "positive": "Positive",
}


def _tk(s: str) -> float | None:
    s = s.strip().replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def _parse_date(s: str) -> date | None:
    s = re.sub(r"\s+", " ", s.strip().rstrip(".").replace(".,", ","))
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%y", "%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y",
                "%b %d, %Y", "%B %d, %Y", "%b %d,%Y", "%B %d,%Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def classify(type_: str | None, summary: str, body: str) -> dict:
    text = f"{summary} {body}"
    low = text.lower()
    tags: list[str] = []
    tone = 0.0
    eps_growth = None
    m = EPS_RE.search(text)
    if m:
        cur, prev = _tk(m.group(1)), _tk(m.group(3))
        if cur is not None and prev is not None:
            if prev != 0:
                eps_growth = max(-1.0, min(1.0, (cur - prev) / abs(prev)))
            else:
                eps_growth = 1.0 if cur > 0 else (-1.0 if cur < 0 else 0.0)
            tags.append("earnings")
            tone += eps_growth
    def growth(m) -> float | None:
        if not m:
            return None
        cur, prev = _tk(m.group(1)), _tk(m.group(3))
        if cur is None or prev is None:
            return None
        if prev == 0:
            return 1.0 if cur > 0 else (-1.0 if cur < 0 else 0.0)
        return round(max(-1.0, min(1.0, (cur - prev) / abs(prev))), 4)

    nocf_growth = growth(NOCF_RE.search(text))
    nav_growth = growth(NAVPS_RE.search(text))
    mf = MFNAV_RE.search(text)
    mf_nav = _tk(mf.group(1)) if mf else None
    cash = CASH_RE.search(text)
    stock = STOCK_RE.search(text)
    if NO_DIV_RE.search(text):
        tags.append("no_dividend")
        tone -= 1.0
    elif ("dividend" in low and ("declared" in low or "recommended" in low)
          and (cash or stock or RATE_RE.search(text))):
        tags.append("dividend")
        tone += 0.5
    record = None
    # only the body, with "i.e." flattened; "resumption" notices carry the resume date, not the record date
    rm = None if "resum" in low else RECORD_RE.search(re.sub(r"\bi\.\s?e\.,?", "ie", body, flags=re.I))
    if rm:
        record = _parse_date(rm.group(1))
    if record or "record date" in low:
        tags.append("record_date")
    if any(k in low for k in NEGATIVE):
        tags.append("negative")
        tone -= 0.5
    if any(k in low for k in POSITIVE):
        tags.append("positive")
        tone += 0.3
    for tag, key in INFO_TAGS:
        if key in low and tag not in tags:
            tags.append(tag)
    return {
        "tags": tags,
        "tone": round(max(-1.0, min(1.0, tone)), 3),
        "eps_growth": None if eps_growth is None else round(eps_growth, 4),
        "record_date": record,
        "cash_pct": float((cash or RATE_RE.search(text)).group(1)) if "dividend" in tags and (cash or RATE_RE.search(text)) else None,
        "nocf_growth": nocf_growth,
        "nav_growth": nav_growth,
        "mf_nav": mf_nav,
    }


def _strip_code(code: str, summary: str) -> str:
    s = summary.strip()
    prefix = f"{code}:"
    return s[len(prefix):].strip() if s.upper().startswith(prefix.upper()) else s


NEWS_UPSERT = """INSERT INTO news (uid, code, news_date, news_time, type, summary, body, tags, tone, eps_growth,
    record_date, source, nocf_growth, nav_growth, mf_nav, fetched_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
ON DUPLICATE KEY UPDATE
    news_time=COALESCE(news_time, VALUES(news_time)),
    nocf_growth=COALESCE(VALUES(nocf_growth), nocf_growth), nav_growth=COALESCE(VALUES(nav_growth), nav_growth),
    mf_nav=COALESCE(VALUES(mf_nav), mf_nav),
    tags=IF(LENGTH(VALUES(body)) >= LENGTH(COALESCE(body, '')), VALUES(tags), tags),
    tone=IF(LENGTH(VALUES(body)) >= LENGTH(COALESCE(body, '')), VALUES(tone), tone),
    eps_growth=IF(LENGTH(VALUES(body)) >= LENGTH(COALESCE(body, '')), VALUES(eps_growth), eps_growth),
    record_date=IF(LENGTH(VALUES(body)) >= LENGTH(COALESCE(body, '')), VALUES(record_date), record_date),
    body=IF(LENGTH(VALUES(body)) >= LENGTH(COALESCE(body, '')), VALUES(body), body)"""


def store(code: str, items: Iterable[dict], source: str) -> int:
    rows = []
    for it in items:
        d = it.get("date")
        if not d:
            continue
        summary = _strip_code(code, it.get("summary") or "")
        body = it.get("body") or ""
        uid = hashlib.sha1(f"{code}|{d.isoformat()}|{summary.lower()}".encode("utf-8")).hexdigest()
        c = classify(it.get("type"), summary, body)
        rows.append((uid, code, d, it.get("time"), (it.get("type") or "")[:32], summary[:500], body,
                     ",".join(c["tags"])[:255], c["tone"], c["eps_growth"], c["record_date"], source,
                     c["nocf_growth"], c["nav_growth"], c["mf_nav"]))
    return db.executemany(NEWS_UPSERT, rows)


def reparse_all() -> int:
    """Re-run the rule-based parser over stored announcements (after the parser gains new fields)."""
    rows = db.query("SELECT uid, type, summary, body FROM news")
    upd = []
    for r in rows:
        c = classify(r["type"], r["summary"] or "", r["body"] or "")
        upd.append((",".join(c["tags"])[:255], c["tone"], c["eps_growth"], c["record_date"],
                    c["nocf_growth"], c["nav_growth"], c["mf_nav"], r["uid"]))
    db.executemany("UPDATE news SET tags=%s, tone=%s, eps_growth=%s, record_date=%s, nocf_growth=%s, nav_growth=%s, "
                   "mf_nav=%s WHERE uid=%s", upd)
    return len(upd)


def fetch_latest() -> list[str]:
    """Store the newest disclosures from the DSE news page; returns codes that have news."""
    items = dse.fetch_news()
    by_code: dict[str, list] = defaultdict(list)
    for it in items:
        by_code[it["code"]].append(it)
    known = {r["code"] for r in db.query("SELECT code FROM instruments")}
    for code, rows in by_code.items():
        if code in known:
            store(code, rows, "news")
    db.set_state("news_fetched_at", datetime.now().isoformat(timespec="seconds"))
    log.info("News: %d disclosures for %d companies", len(items), len(by_code))
    return sorted(c for c in by_code if c in known)


def latest(limit: int = 30, code: str | None = None, codes: list[str] | None = None) -> list[dict]:
    sql = ("SELECT n.code, i.name, n.news_date, n.news_time, n.type, n.summary, n.body, n.tags, n.tone, n.eps_growth, "
           "n.record_date, n.ai_impact, n.ai_reason FROM news n LEFT JOIN instruments i ON i.code=n.code")
    params: list = []
    if code:
        sql += " WHERE n.code=%s"
        params.append(code)
    elif codes:
        sql += f" WHERE n.code IN ({','.join(['%s'] * len(codes))})"
        params.extend(codes)
    sql += " ORDER BY n.news_date DESC, n.news_time DESC LIMIT %s"
    params.append(limit)
    rows = db.query(sql, params)
    for r in rows:
        r["tags"] = [t for t in (r["tags"] or "").split(",") if t]
    return rows


# ---------------------------------------------------------------------------- features

def load_events() -> dict[str, list[tuple]]:
    """code -> [(news_date, tone, eps_growth, has_dividend, has_no_dividend, record_date, ai_impact,
    nocf_growth, nav_growth, mf_nav)] sorted by date."""
    out: dict[str, list[tuple]] = defaultdict(list)
    for r in db.query("SELECT code, news_date, tone, eps_growth, tags, record_date, ai_impact, nocf_growth, nav_growth, "
                      "mf_nav FROM news ORDER BY code, news_date"):
        tags = (r["tags"] or "").split(",")
        out[r["code"]].append((r["news_date"], r["tone"] or 0.0, r["eps_growth"], "dividend" in tags,
                               "no_dividend" in tags, r["record_date"], r["ai_impact"],
                               r["nocf_growth"], r["nav_growth"], r["mf_nav"]))
    return out


def features(dates: list[date], events: list[tuple]) -> dict[str, list[float]]:
    """Point-in-time news features for each date (uses only announcements dated on/before it)."""
    n = len(dates)
    out = {k: [0.0] * n for k in ("news_cnt", "news_tone", "eps_surprise", "div_event", "record_soon", "ai_news",
                                  "nocf_g", "navps_g", "mf_nav")}
    if not events:
        return out
    ev_dates = [e[0] for e in events]
    for i, t in enumerate(dates):
        hi = bisect.bisect_right(ev_dates, t)
        if hi == 0:
            continue
        lo7 = bisect.bisect_right(ev_dates, t - timedelta(days=7))
        lo30 = bisect.bisect_right(ev_dates, t - timedelta(days=30))
        lo90 = bisect.bisect_right(ev_dates, t - timedelta(days=90))
        out["news_cnt"][i] = float(hi - lo7)
        out["news_tone"][i] = max(-2.0, min(2.0, sum(e[1] for e in events[lo30:hi])))
        for e in reversed(events[lo90:hi]):
            if e[2] is not None:
                out["eps_surprise"][i] = e[2]
                break
        lo120 = bisect.bisect_right(ev_dates, t - timedelta(days=120))
        lo14 = bisect.bisect_right(ev_dates, t - timedelta(days=14))
        for e in reversed(events[lo120:hi]):  # latest results: cash flow and NAV per share vs last year
            if len(e) > 8 and (e[7] is not None or e[8] is not None):
                out["nocf_g"][i] = e[7] or 0.0
                out["navps_g"][i] = e[8] or 0.0
                break
        for e in reversed(events[lo14:hi]):   # mutual funds publish NAV at market price weekly/daily
            if len(e) > 9 and e[9]:
                out["mf_nav"][i] = e[9]
                break
        recent = events[lo30:hi]
        out["ai_news"][i] = max(-4.0, min(4.0, float(sum(e[6] or 0 for e in recent if len(e) > 6))))
        if any(e[4] for e in recent):
            out["div_event"][i] = -1.0
        elif any(e[3] for e in recent):
            out["div_event"][i] = 1.0
        horizon = t + timedelta(days=10)
        if any(e[5] and t < e[5] <= horizon for e in events[lo90:hi]):
            out["record_soon"][i] = 1.0
    return out
