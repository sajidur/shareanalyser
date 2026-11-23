"""Client for the public Dhaka Stock Exchange website (https://www.dse.com.bd).

Data sources (all public, the same data the website shows to visitors):
  * /api/live/snapshot            - live prices for every instrument, indices, breadth, session
  * /company/<CODE>               - company page; embeds ~1 year of daily OHLCV + fundamentals
  * /pe                           - P/E table for all listed companies
  * /recent-market-information    - last 30 sessions of DSEX/DS30/DSES, turnover, volume

The website is a Next.js app: page data is embedded as React Server Component (RSC)
payload chunks (`self.__next_f.push([1,"..."])`). We decode those chunks and pull out
the JSON objects we need.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import quote

import requests

from . import config

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 BDStockTracker/1.0"
)

_local = threading.local()


class DSEError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
        _local.session = s
    return s


def _get(path: str, *, as_json: bool = True, retries: int = 3) -> Any:
    url = config.get("dse", "base_url").rstrip("/") + path
    timeout = config.getfloat("dse", "timeout_seconds")
    err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = _session().get(url, timeout=timeout)
            if r.status_code == 404:
                raise DSEError(f"Not found on DSE: {path}")
            r.raise_for_status()
            return r.json() if as_json else r.text
        except DSEError:
            raise
        except (requests.RequestException, ValueError) as e:
            err = e
            log.warning("GET %s failed (attempt %d/%d): %s", path, attempt, retries, e)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise DSEError(f"Could not fetch {path}: {err}")


def num(v: Any) -> float | None:
    """Parse DSE numbers: accepts numbers or strings like '1,234.5', 'n/a', '-'."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v else None  # drop NaN
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "n/a", "N/A", "NA", "$undefined"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------- snapshot

@dataclass
class Snapshot:
    trade_date: date
    trade_time: str | None
    session: dict
    indices: list[dict]
    totals: dict
    breadth: dict
    daily_totals: list[dict]
    quotes: list[dict]
    fetched_at: datetime = field(default_factory=datetime.now)

    @property
    def is_open(self) -> bool:
        return bool(self.session.get("isOpen"))

    @property
    def any_traded(self) -> bool:
        return any(q["volume"] > 0 for q in self.quotes)

    def market_state(self) -> dict:
        return {
            "trade_date": self.trade_date.isoformat(),
            "trade_time": self.trade_time,
            "is_open": self.is_open,
            "phase": self.session.get("phase"),
            "session": self.session,
            "indices": self.indices,
            "totals": self.totals,
            "breadth": self.breadth,
            "fetched_at": self.fetched_at.isoformat(timespec="seconds"),
        }


_TRADE_TIME_RE = re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})")


def _parse_trade_date(trade_time: str | None, session: dict) -> date:
    """The date of the prices in the snapshot. `tradeTime` ("Nov 22, 2026 at 2:40 PM")
    is the most reliable; before the market opens it still shows the previous session."""
    if trade_time:
        m = _TRADE_TIME_RE.search(trade_time)
        if m:
            text = f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}"
            try:
                return datetime.strptime(text, "%b %d %Y").date()
            except ValueError:
                pass
    for key in ("sessionDate", "date"):
        v = session.get(key)
        if v:
            try:
                return date.fromisoformat(str(v)[:10])
            except ValueError:
                pass
    return date.today()


def _normalize_quote(r: dict) -> dict | None:
    code = str(r.get("code") or "").strip()
    if not code:
        return None
    ltp, ycp = num(r.get("ltp")), num(r.get("ycp"))
    o, h, lo, c = num(r.get("open")), num(r.get("high")), num(r.get("low")), num(r.get("close"))
    volume = int(num(r.get("volume")) or 0)
    trades = int(num(r.get("trades")) or 0)
    value = num(r.get("value")) or 0.0
    traded = volume > 0 or trades > 0
    close = c or ltp or ycp
    if not traded:
        o = h = lo = close
    else:
        o = o or close
        h = h or max(x for x in (o, close) if x)
        lo = lo or min(x for x in (o, close) if x)
    pct = num(r.get("percent"))
    if pct is None:
        pct = ((ltp - ycp) * 100 / ycp) if (traded and ltp and ycp) else 0.0
    return {
        "code": code,
        "ltp": ltp if ltp else close,
        "ycp": ycp,
        "open": o,
        "high": h,
        "low": lo,
        "close": close,
        "volume": volume,
        "value_mn": value,
        "trades": trades,
        "change_pct": round(pct, 4),
        "category": r.get("category") or None,
        "board": r.get("board") or None,
        "sector": r.get("sector") or None,
        "asset_type": r.get("assetType") or None,
    }


def parse_snapshot(d: dict) -> Snapshot:
    market = d.get("market") or {}
    prices = d.get("prices") or {}
    cols = prices.get("cols") or []
    raw_rows = prices.get("rows") or []
    rows = [r if isinstance(r, dict) else dict(zip(cols, r)) for r in raw_rows]
    session = market.get("session") or prices.get("session") or {}
    totals = market.get("totals") or {}
    quotes = [q for q in (_normalize_quote(r) for r in rows) if q]
    if not quotes:
        raise DSEError("Snapshot contained no price rows (DSE format may have changed)")
    return Snapshot(
        trade_date=_parse_trade_date(totals.get("tradeTime"), session),
        trade_time=totals.get("tradeTime"),
        session=session,
        indices=market.get("indices") or [],
        totals=totals,
        breadth=market.get("breadth") or {},
        daily_totals=market.get("dailyTotals") or [],
        quotes=quotes,
    )


def fetch_snapshot() -> Snapshot:
    return parse_snapshot(_get("/api/live/snapshot"))


# --------------------------------------------------------------------------- page data

_RSC_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)


def rsc_text(html: str) -> str:
    """Join the decoded Next.js RSC payload chunks embedded in a page."""
    parts = []
    for chunk in _RSC_RE.findall(html):
        try:
            parts.append(json.loads('"' + chunk + '"'))
        except ValueError:
            continue
    return "".join(parts)


def extract_json(text: str, key: str, opener: str, validate: Callable[[Any], bool] | None = None) -> Any:
    """Find `"key":{` / `"key":[` in text and decode the JSON value that starts there."""
    needle = f'"{key}":{opener}'
    decoder = json.JSONDecoder()
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return None
        try:
            value, _ = decoder.raw_decode(text, i + len(key) + 3)
            if validate is None or validate(value):
                return value
        except ValueError:
            pass
        start = i + 1


_REF_RE = re.compile(r"\$([0-9a-fA-F]+)")


def resolve_text(text: str, value: Any) -> Any:
    """Long strings in the RSC payload are stored as separate length-prefixed rows
    ('28:T<hex byte length>,<text>') and referenced as "$28". Return the referenced text."""
    if not isinstance(value, str):
        return value
    m = _REF_RE.fullmatch(value.strip())
    if not m:
        return value
    rid = m.group(1)
    row = re.search(r"(?<![0-9a-fA-F])" + re.escape(rid) + r":T([0-9a-fA-F]+),", text)
    if not row:
        return ""
    n = int(row.group(1), 16)
    return text[row.end(): row.end() + n].encode("utf-8")[:n].decode("utf-8", "ignore")


def parse_any_date(s: Any) -> date | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s).strip())
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%d-%b-%Y", "%b %d, %Y", "%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s[:30], fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _announcements(text: str, items: list) -> list[dict]:
    out = []
    for a in items or []:
        if not isinstance(a, dict):
            continue
        d = parse_any_date(a.get("date"))
        if not d:
            continue
        out.append({
            "date": d,
            "time": a.get("time"),
            "type": a.get("type"),
            "summary": str(resolve_text(text, a.get("summary")) or ""),
            "body": str(resolve_text(text, a.get("body")) or ""),
        })
    return out


def _is_price_series(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and isinstance(v[0], dict) and "date" in v[0] and "price" in v[0]


def parse_company_page(html: str) -> dict:
    text = rsc_text(html)
    company = extract_json(text, "company", "{", lambda v: isinstance(v, dict) and "code" in v) or {}
    series = extract_json(text, "series", "[", _is_price_series) or []
    bars: list[dict] = []
    prev_close: float | None = None
    for p in sorted(series, key=lambda x: str(x.get("date"))):
        try:
            d = date.fromisoformat(str(p.get("date"))[:10])
        except ValueError:
            continue
        close = num(p.get("price")) or num(p.get("close"))
        if not close:
            continue
        o = num(p.get("open")) or close
        h = num(p.get("high")) or max(o, close)
        lo = num(p.get("low")) or min(o, close)
        bars.append(
            {
                "trade_date": d,
                "open": o,
                "high": max(h, o, close),
                "low": min(lo, o, close),
                "close": close,
                "ltp": close,
                "ycp": prev_close,
                "volume": int(num(p.get("volume")) or 0),
                "value_mn": None,
                "trades": int(num(p.get("trades")) or 0),
                "change_pct": round((close - prev_close) * 100 / prev_close, 4) if prev_close else None,
            }
        )
        prev_close = close
    ann = extract_json(text, "recentAnnouncements", "[", lambda v: isinstance(v, list)) or []
    return {"company": company, "bars": bars, "announcements": _announcements(text, ann)}


def fetch_company(code: str) -> dict:
    html = _get(f"/company/{quote(code, safe='()&-._')}", as_json=False)
    return parse_company_page(html)


def parse_news_page(html: str) -> list[dict]:
    """Latest disclosures for all companies from https://www.dse.com.bd/news (newest ~50)."""
    text = rsc_text(html)
    items = extract_json(
        text, "disclosures", "[", lambda v: isinstance(v, list) and (not v or (isinstance(v[0], dict) and "code" in v[0]))
    ) or []
    out = []
    for item, parsed in zip(items, _announcements(text, items)):
        code = str(item.get("code") or "").strip()
        if code:
            parsed["code"] = code
            out.append(parsed)
    return out


def fetch_news() -> list[dict]:
    return parse_news_page(_get("/news", as_json=False))


def parse_pe_page(html: str) -> dict[str, float | None]:
    text = rsc_text(html)
    rows = extract_json(
        text, "rows", "[", lambda v: isinstance(v, list) and bool(v) and isinstance(v[0], dict) and "trailingPe" in v[0]
    ) or []
    return {str(r["code"]): num(r.get("trailingPe")) for r in rows if r.get("code")}


def fetch_pe() -> dict[str, float | None]:
    return parse_pe_page(_get("/pe", as_json=False))


def parse_recent_market_page(html: str) -> list[dict]:
    text = rsc_text(html)
    rows = extract_json(
        text, "initialRows", "[", lambda v: isinstance(v, list) and bool(v) and isinstance(v[0], dict) and "dsex" in v[0]
    ) or []
    out = []
    for r in rows:
        try:
            d = date.fromisoformat(str(r.get("date"))[:10])
        except ValueError:
            continue
        out.append(
            {
                "trade_date": d,
                "DSEX": num(r.get("dsex")),
                "DS30": num(r.get("ds30")),
                "DSES": num(r.get("dses")),
                "trades": num(r.get("trades")),
                "volume": num(r.get("volume")),
                "turnover_mn": num(r.get("value")),
                "market_cap": num(r.get("marketCap")),
            }
        )
    return sorted(out, key=lambda x: x["trade_date"])


def fetch_recent_market() -> list[dict]:
    return parse_recent_market_page(_get("/recent-market-information", as_json=False))
