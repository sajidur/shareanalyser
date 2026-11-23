"""Forecasts for DSE shares and the top-10 short-term / long-term lists.

Design principle: only claim what the data supports, and always show the evidence.

1. Price ranges (the core forecast)
   For each horizon (next session, 1 week, 1 month, 3 months) the likely range comes from the
   stock's own recent volatility, scaled by the *measured* distribution of DSE moves over that
   horizon (fat tails and skew included, not a textbook bell curve). The 68% / 90% bands are
   checked out-of-sample: "the price ended inside the band X% of the time".

2. Direction model (gated)
   A logistic regression (chance up vs down) and ridge regression (expected return) are trained on
   all shares together using six groups of inputs, all point-in-time (no look-ahead):
     technical (momentum, moving averages, RSI, MACD, volatility, volume, range position),
     liquidity & DSE category, market (index momentum, breadth, turnover trend),
     sector (sector momentum, strength vs sector), fundamentals (earnings yield, book/price,
     dividend yield, EPS growth - each only from ~120 days after the fiscal year end, when it was
     published) and news (announcement count and tone, earnings surprise, dividend declared /
     no dividend, record date coming up).
   It is re-tested every day on rolling windows it has never seen, and each input group is tested
   for what it adds. Its direction is shown as a forecast only when it has beaten chance
   consistently; otherwise the page says "no reliable signal".

4. Daily accuracy check
   Every forecast is stored. Each day, forecasts whose horizon ended are compared with the real
   closing prices (inside range? direction right? error). A walk-forward history of the last ~40
   sessions is generated once so the record does not start empty.

3. Top-10 lists are transparent screens, not predictions
   * Short term: liquid shares in an uptrend that have pulled back near their 20-day average, with a
     volatility-based stop and target. Its historical record is measured and shown.
   * Long term (regular buying): category-A, profitable, liquid companies scored on valuation,
     dividends, earnings growth and price stability.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from . import analysis, db, indicators as ind, news, settings

log = logging.getLogger(__name__)

HORIZONS: dict[int, str] = {1: "Next session", 5: "1 week", 20: "1 month", 60: "3 months"}
# Long horizons cannot be trained or tested with ~1 year of DSE history: their ranges extend the
# 3-month range with sqrt(time) and are labelled as estimates.
LONG_HORIZONS: dict[int, str] = {120: "6 months", 245: "1 year"}
ALL_HORIZONS: dict[int, str] = {**HORIZONS, **LONG_HORIZONS}
SHORT_TERM = (1, 5, 20)
LONG_TERM = (60, 120, 245)

# key, plain-language label, value format, input group
FEATURES: list[tuple[str, str, str, str]] = [
    ("r1", "Last session's move", "ret", "technical"),
    ("r5", "1-week momentum", "ret", "technical"),
    ("r20", "1-month momentum", "ret", "technical"),
    ("r60", "3-month momentum", "ret", "technical"),
    ("d20", "Distance from 20-day average", "pct", "technical"),
    ("d50", "Distance from 50-day average", "pct", "technical"),
    ("rsi", "RSI (14)", "rsi", "technical"),
    ("macd", "MACD histogram", "macd", "technical"),
    ("vol20", "Recent volatility", "vol", "technical"),
    ("vratio", "Volume vs 20-day average", "ratio", "technical"),
    ("pos60", "Position in 60-day range", "frac", "technical"),
    ("liq", "Liquidity (avg daily value)", "liq", "liquidity"),
    ("idle", "Days without trades (20d)", "frac", "liquidity"),
    ("cat_a", "Category A", "flag", "liquidity"),
    ("cat_z", "Category Z", "flag", "liquidity"),
    ("mkt5", "Market 1-week momentum", "ret", "market"),
    ("mkt20", "Market 1-month momentum", "ret", "market"),
    ("breadth5", "Market breadth (5-day)", "breadth", "market"),
    ("mkt_turn", "Market turnover vs 3-month avg", "ratio", "market"),
    ("sector20", "Sector 1-month momentum", "ret", "sector"),
    ("rel_sector", "Strength vs own sector (1 month)", "ret", "sector"),
    ("ey", "Earnings yield (EPS / price)", "yield", "fundamentals"),
    ("bp", "Book value / price", "times", "fundamentals"),
    ("dy", "Dividend yield", "yield", "fundamentals"),
    ("eps_g", "Annual EPS growth", "growth", "fundamentals"),
    ("f_missing", "No published financials yet", "flag", "fundamentals"),
    ("news_cnt", "Announcements in last 7 days", "count", "news"),
    ("news_tone", "News tone (last 30 days)", "tone", "news"),
    ("eps_surprise", "Latest earnings vs last year", "growth", "news"),
    ("div_event", "Dividend news (last 30 days)", "event", "news"),
    ("record_soon", "Record date within 10 days", "flag", "news"),
    ("ai_news", "AI news score (30 days, local LLM)", "ai", "news"),
    ("clv5", "Close position in day's range (5d)", "clv", "activity"),
    ("gap", "Opening gap", "ret", "activity"),
    ("hl5", "Intraday high-low range (5d)", "pct", "activity"),
    ("tsize", "Avg trade size vs normal", "ratio", "activity"),
    ("tratio", "Number of trades vs normal", "ratio", "activity"),
    ("streak", "Up/down day streak", "streak", "activity"),
    ("circ20", "Circuit-limit days (20d)", "count", "activity"),
    ("beta60", "Beta to market (60d)", "num", "risk"),
    ("idio60", "Stock-specific volatility (60d)", "vol", "risk"),
    ("d_hi52", "Distance from 52-week high", "pct", "risk"),
    ("d_lo52", "Distance from 52-week low", "pct", "risk"),
    ("size", "Market capitalisation", "cap", "ownership"),
    ("sponsor", "Sponsor/director holding", "share", "ownership"),
    ("institution", "Institutional holding", "share", "ownership"),
    ("foreign", "Foreign holding", "share", "ownership"),
    ("free_float", "Free float", "share", "ownership"),
    ("dow_sun", "Sunday session (after weekend)", "flag", "calendar"),
    ("dow_thu", "Thursday session (before weekend)", "flag", "calendar"),
    ("bd_query", "DSE query on unusual price/volume (recent)", "decay", "bangladesh"),
    ("bd_rumour", "Rumour / media clarification (recent)", "decay", "bangladesh"),
    ("bd_sponsor", "Sponsor/director net buy declarations (30d)", "signed", "bangladesh"),
    ("bd_toz", "Downgraded to category Z (60d)", "flag", "bangladesh"),
    ("bd_margin", "Margin loans prohibited", "flag", "bangladesh"),
    ("bd_govt", "Government / regulator news (30d)", "count", "bangladesh"),
    ("nocf_g", "Operating cash flow/share vs last year (latest results)", "growth", "quality"),
    ("navps_g", "NAV per share change (latest results)", "growth", "quality"),
    ("rel_ey", "Earnings yield vs sector median", "pct", "quality"),
    ("rel_bp", "Book / price vs sector median", "times", "quality"),
    ("mf_disc", "Mutual fund price vs NAV (premium / discount)", "pct", "quality"),
    ("circ_streak", "Consecutive circuit-limit days", "signed", "activity"),
    ("spec_turn", "Market turnover in category Z & small shares", "frac", "market"),
    ("govt_hold", "Government (state) shareholding", "share", "political"),
    ("bd_state", "Central bank / regulator control of the board (180d)", "flag", "political"),
    ("bd_polhit", "Results mention political unrest (120d)", "flag", "political"),
    ("bd_court", "Court order / legal dispute (90d)", "flag", "political"),
    ("shock_beta", "Fall on market sell-off days vs market (political / macro shocks)", "num", "political"),
]
FEATURE_KEYS = [f[0] for f in FEATURES]
GROUPS: dict[str, list[int]] = defaultdict(list)
for _i, _f in enumerate(FEATURES):
    GROUPS[_f[3]].append(_i)
GROUP_LABELS = {"technical": "Price & volume (technical)", "liquidity": "Liquidity & DSE category",
                "market": "Market conditions", "sector": "Sector strength", "fundamentals": "Fundamentals",
                "news": "News & announcements", "activity": "Trading activity", "risk": "Risk (beta, 52-week)",
                "ownership": "Ownership & size", "calendar": "Day of week",
                "bangladesh": "Bangladesh factors (rumour, insiders, regulator, government)",
                "quality": "Earnings quality & relative value",
                "political": "Political & state influence (state ownership, takeovers, courts, shock sensitivity)"}
WARMUP = 60
MIN_TRAIN_ROWS = 2000
LIQUID_MN = 5.0          # Tk million average daily traded value: "liquid enough to trade"
QUANTILES = (5, 16, 25, 35, 50, 65, 75, 84, 95)
BANDS = {30: ("q35", "q65"), 50: ("q25", "q75"), 68: ("q16", "q84"), 90: ("q5", "q95")}
# inputs of the volatility (range-width) model: names for the formula card
VOL_INPUTS = ["log 5-day volatility", "log 20-day volatility", "log 60-day volatility", "log ATR %",
              "log intraday high-low range", "idle days", "liquidity", "log price (tick size)",
              "circuit-limit days", "announcements (7d)", "record date soon", "|last move|", "|1-week move|",
              "stock-specific volatility", "market cap", "log market volatility", "category Z"]
# Bangladesh / political inputs the range model may add; used only if they win on unseen data
VOL_BD_INPUTS = ["market in Stress", "market Euphoric", "DSE query (recent)", "rumour clarification (recent)",
                 "downgraded to Z", "margin-loan ban", "central bank / regulator control", "court order",
                 "sell-off sensitivity", "government holding", "pump-and-dump pattern (circuit streak)"]
V_BASE = list(range(len(VOL_INPUTS)))
V_ALL = list(range(len(VOL_INPUTS) + len(VOL_BD_INPUTS)))
RANGE_METHODS = ("baseline", "vol", "vol_binned", "vol_bd", "vol_bd_binned")
FOLDS, FOLD_DAYS = 3, 15  # rolling out-of-sample test windows


# ============================================================================ helpers

def _roll_mean(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        cs = np.cumsum(np.insert(x.astype(float), 0, 0.0))
        out[w - 1:] = (cs[w:] - cs[:-w]) / w
    return out


def _roll(x: np.ndarray, w: int, fn: str) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        win = np.lib.stride_tricks.sliding_window_view(x, w)
        out[w - 1:] = getattr(win, fn)(axis=1)
    return out


def _arr(values: list) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _ranks(x: np.ndarray) -> np.ndarray:
    r = np.empty(len(x))
    r[np.argsort(x, kind="mergesort")] = np.arange(len(x))
    return r


def _auc(y: np.ndarray, p: np.ndarray) -> float | None:
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return None
    r = _ranks(p) + 1
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else None


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


_HOLIDAYS: dict[str, Any] = {"loaded_at": 0.0, "data": None}


def _holiday_data() -> dict:
    import time as _t

    if _HOLIDAYS["data"] is None or _t.time() - _HOLIDAYS["loaded_at"] > 600:
        try:
            _HOLIDAYS["data"] = db.get_state("holidays") or {}
        except Exception:  # no database (e.g. offline tests): weekends only
            _HOLIDAYS["data"] = {}
        _HOLIDAYS["loaded_at"] = _t.time()
    return _HOLIDAYS["data"]


def holiday_dates(years: range, data: dict | None = None) -> set[date]:
    """DSE holidays for the given years. Years DSE has not published yet are projected from the
    nearest published year: fixed-date holidays keep their date; moon-based ones (Eid, Shab-e-Barat,
    ...) move ~11 days earlier per year, following the Islamic calendar."""
    data = _holiday_data() if data is None else data
    if not data:
        return set()
    known = sorted(int(y) for y in data)
    out: set[date] = set()
    for y in years:
        if str(y) in data:
            out |= {date.fromisoformat(h["date"]) for h in data[str(y)]}
            continue
        base = max(known) if y > max(known) else min(known)
        shift = (y - base) * 11
        for h in data[str(base)]:
            d0 = date.fromisoformat(h["date"])
            try:
                d = d0.replace(year=y)
            except ValueError:  # 29 Feb
                continue
            out.add(d - timedelta(days=shift) if h.get("moon") else d)
    return out


def next_trading_days(start: date, n: int, holidays: set[date] | None = None) -> list[date]:
    """DSE trades Sunday-Thursday (Friday/Saturday weekend), minus DSE holidays
    (published list; later years projected)."""
    if holidays is None:
        holidays = holiday_dates(range(start.year, start.year + 3))
    out, d = [], start
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() not in (4, 5) and d not in holidays:
            out.append(d)
    return out


def fmt_feature(kind: str, x: float) -> str:
    if x is None or not math.isfinite(x):
        return "n/a"
    if kind == "ret":
        return f"{(math.exp(x) - 1) * 100:+.1f}%"
    if kind == "pct":
        return f"{x * 100:+.1f}%"
    if kind == "rsi":
        return f"{x:.0f}"
    if kind == "macd":
        return f"{x * 100:+.2f}% of price"
    if kind == "vol":
        return f"{x * 100:.1f}% per day"
    if kind == "ratio":
        return f"{math.exp(x):.1f}× average"
    if kind == "frac":
        return f"{x * 100:.0f}%"
    if kind == "liq":
        return f"Tk {math.expm1(x):,.1f} mn/day"
    if kind == "flag":
        return "Yes" if x >= 0.5 else "No"
    if kind == "yield":
        return f"{x * 100:.1f}%"
    if kind == "ai":
        return f"{x:+.0f} (AI-rated news)" if x else "no rated news"
    if kind == "decay":
        return "recent" if x > 0.05 else "none"
    if kind == "signed":
        return f"{x:+.0f}"
    if kind == "clv":
        return "near day's high" if x > 0.3 else "near day's low" if x < -0.3 else "mid-range"
    if kind == "streak":
        return f"{abs(x):.0f} {'up' if x > 0 else 'down'} day(s)" if x else "none"
    if kind == "num":
        return f"{x:.2f}"
    if kind == "cap":
        return f"Tk {math.exp(x) / 10:,.0f} cr"
    if kind == "share":
        return f"{x:.0f}%"
    if kind == "breadth":
        return f"{x * 100:.0f}% of shares rising"
    if kind == "times":
        return f"{x:.2f}×"
    if kind == "growth":
        return f"{x * 100:+.0f}%"
    if kind == "count":
        return f"{x:.0f}"
    if kind == "tone":
        return "positive" if x > 0.2 else "negative" if x < -0.2 else "neutral"
    if kind == "event":
        return "dividend declared" if x > 0 else "no dividend" if x < 0 else "none"
    return f"{x:.3f}"


# ============================================================================ point-in-time fundamentals

_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
PUBLICATION_LAG_DAYS = 120  # BSEC: audited annual results within 120 days of year end


def _fiscal_year_end(year: int, year_end: str | None) -> date:
    month = _MONTHS.get(str(year_end or "december").strip().lower(), 12)
    nxt = date(year + (1 if month == 12 else 0), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def fundamental_points(info: dict | None) -> list[tuple]:
    """[(available_from, eps, nav, cash_dividend_tk)] from DSE's multi-year financials."""
    if not info:
        return []
    try:
        extra = json.loads(info.get("extra_json") or "{}")
    except ValueError:
        return []
    face = info.get("face_value") or 10.0
    divs = {}
    for d in extra.get("dividendHistory") or []:
        if isinstance(d, dict) and d.get("year"):
            divs[int(d["year"])] = (d.get("cash") or 0) * face / 100
    pts = []
    for f in extra.get("multiYearFinancials") or []:
        if not isinstance(f, dict) or not f.get("year") or f.get("epsBasic") is None:
            continue
        y = int(f["year"])
        avail = _fiscal_year_end(y, info.get("year_end")) + timedelta(days=PUBLICATION_LAG_DAYS)
        pts.append((avail, float(f["epsBasic"]), f.get("nav"), divs.get(y)))
    return sorted(pts)


_PATTERN_DATE = re.compile(r"([A-Za-z]{3,9}\.? \d{1,2}, ?\d{4})")
OWNERSHIP_LAG_DAYS = 30  # monthly shareholding is published a few weeks after month end


def ownership_features(dates: list[date], info: dict) -> dict[str, np.ndarray]:
    """Sponsor / institutional / foreign / government holding %, each from its publication (point-in-time)."""
    n = len(dates)
    out = {k: np.zeros(n) for k in ("sponsor", "institution", "foreign", "govt_hold")}
    try:
        extra = json.loads(info.get("extra_json") or "{}") if info else {}
    except ValueError:
        extra = {}
    pts = []
    for p in extra.get("sharePattern") or []:
        if not isinstance(p, dict) or not isinstance(p.get("pattern"), dict):
            continue
        m = _PATTERN_DATE.search(str(p.get("date") or ""))
        d = dse_date(m.group(1)) if m else None
        if d:
            pts.append((d + timedelta(days=OWNERSHIP_LAG_DAYS), p["pattern"]))
    pts.sort(key=lambda x: x[0])
    avail = [p[0] for p in pts]
    for i, t in enumerate(dates):
        k = bisect.bisect_right(avail, t) - 1
        if k < 0:
            k = 0 if pts else -1  # before the first known pattern, use the earliest one (changes slowly)
        if k >= 0:
            pat = pts[k][1]
            out["sponsor"][i] = float(pat.get("sponsor") or 0)
            out["institution"][i] = float(pat.get("institution") or 0)
            out["foreign"][i] = float(pat.get("foreign") or 0)
            out["govt_hold"][i] = float(pat.get("government") or 0)
    return out


def shock_beta_series(r1: np.ndarray, mret: np.ndarray, traded: np.ndarray, threshold: float = -0.01) -> np.ndarray:
    """Point-in-time: how hard the share fell on all earlier market sell-off days (market <= -1%),
    relative to the market. 1 = like the market. Shrunk towards 1 while there are few such days."""
    shock = (mret <= threshold) & np.isfinite(r1) & traded
    s_sum = np.cumsum(np.where(shock, np.nan_to_num(r1), 0.0))
    m_sum = np.cumsum(np.where(shock, mret, 0.0))
    cnt = np.cumsum(shock)
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = np.where(m_sum < 0, s_sum / m_sum, 1.0)
    w = cnt / (cnt + 10.0)
    return np.clip(w * raw + (1 - w), -1.0, 4.0)


def dse_date(s: str) -> date | None:
    s = s.replace(".", "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d,%Y", "%B %d,%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fundamental_features(dates: list[date], close: np.ndarray, pts: list[tuple]) -> dict[str, np.ndarray]:
    n = len(dates)
    out = {k: np.zeros(n) for k in ("ey", "bp", "dy", "eps_g")}
    out["f_missing"] = np.ones(n)
    avail = [p[0] for p in pts]
    for i, t in enumerate(dates):
        k = bisect.bisect_right(avail, t) - 1
        if k < 0:
            continue
        _, eps, nav, cash = pts[k]
        c = close[i]
        out["f_missing"][i] = 0.0
        out["ey"][i] = max(-0.5, min(0.5, eps / c))
        out["bp"][i] = max(0.0, min(5.0, nav / c)) if nav else 0.0
        out["dy"][i] = max(0.0, min(0.3, cash / c)) if cash else 0.0
        if k >= 1 and pts[k - 1][1]:
            prev = pts[k - 1][1]
            out["eps_g"][i] = max(-1.0, min(1.0, (eps - prev) / abs(prev)))
    return out


# ============================================================================ features

def _load_panel() -> tuple[dict[str, dict], list[date]]:
    types = ",".join(f"'{t}'" for t in sorted(analysis.DEBT_ASSET_TYPES))
    rows = db.query(
        "SELECT p.code, p.trade_date, p.open, p.high, p.low, p.close, p.volume, p.trades, i.category, i.asset_type, i.sector "
        "FROM daily_prices p JOIN instruments i ON i.code=p.code "
        f"WHERE (i.asset_type IS NULL OR i.asset_type NOT IN ({types})) AND p.close > 0 "
        "ORDER BY p.code, p.trade_date"
    )
    raw: dict[str, dict] = {}
    for r in rows:
        s = raw.setdefault(r["code"], {"dates": [], "o": [], "h": [], "l": [], "c": [], "v": [], "n": [],
                                       "category": r["category"], "asset_type": r["asset_type"],
                                       "sector": r["sector"] or "Other"})
        s["dates"].append(r["trade_date"])
        s["c"].append(float(r["close"]))
        s["o"].append(float(r["open"] or r["close"]))
        s["h"].append(float(r["high"] or r["close"]))
        s["l"].append(float(r["low"] or r["close"]))
        s["v"].append(float(r["volume"] or 0))
        s["n"].append(float(r["trades"] or 0))
    calendar = sorted({d for s in raw.values() for d in s["dates"]})
    return raw, calendar


def _roll_nanmean(x: np.ndarray, w: int) -> np.ndarray:
    ok = np.isfinite(x)
    s = _roll_mean(np.where(ok, x, 0.0), w)
    cnt = _roll_mean(ok.astype(float), w)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, s / cnt, np.nan)


def activity_features(c: np.ndarray, o: np.ndarray, h: np.ndarray, lo: np.ndarray, v: np.ndarray,
                      trades: np.ndarray, r1: np.ndarray) -> dict[str, np.ndarray]:
    """Trading-activity and 52-week inputs (all use data up to and including day t)."""
    n = len(c)
    traded = v > 0
    rng = h - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        clv = np.where(traded & (rng > 0), (2 * c - h - lo) / rng, 0.0)
        hl = np.where(traded, np.log(np.maximum(h, c) / np.maximum(np.minimum(lo, c), 1e-9)), 0.0)
        gap = np.zeros(n)
        gap[1:] = np.where(traded[1:] & (o[1:] > 0), np.log(o[1:] / c[:-1]), 0.0)
        lts = np.where(traded & (trades > 0), np.log(v / np.maximum(trades, 1)), np.nan)
        prior_lts = np.r_[np.nan, _roll_nanmean(lts, 20)[:-1]]
        tsize = np.nan_to_num(lts - prior_lts, nan=0.0)
        tratio = np.log1p(trades) - np.log1p(np.r_[np.nan, _roll_mean(trades, 20)[:-1]])
    streak = np.zeros(n)
    for t in range(1, n):
        x = r1[t]
        if not np.isfinite(x) or x == 0:
            streak[t] = 0
        elif x > 0:
            streak[t] = streak[t - 1] + 1 if streak[t - 1] > 0 else 1
        else:
            streak[t] = streak[t - 1] - 1 if streak[t - 1] < 0 else -1
    hi52 = np.array([h[max(0, t - 249): t + 1].max() for t in range(n)])
    lo52 = np.array([lo[max(0, t - 249): t + 1].min() for t in range(n)])
    # consecutive circuit-limit days (+ up, - down): DSE "circuit chasing" behaviour
    circ = np.zeros(n)
    lim = math.log(1.09)
    for t in range(1, n):
        x = r1[t]
        if np.isfinite(x) and x >= lim:
            circ[t] = circ[t - 1] + 1 if circ[t - 1] > 0 else 1
        elif np.isfinite(x) and x <= -lim:
            circ[t] = circ[t - 1] - 1 if circ[t - 1] < 0 else -1
    return {
        "clv5": _roll_mean(clv, 5), "gap": gap, "hl5": _roll_mean(hl, 5),
        "tsize": tsize, "tratio": tratio, "streak": np.clip(streak, -7, 7), "circ_streak": np.clip(circ, -5, 5),
        "circ20": _roll_mean((np.nan_to_num(np.abs(r1)) >= 0.09).astype(float), 20) * 20,
        "d_hi52": c / hi52 - 1, "d_lo52": c / lo52 - 1,
    }


def stock_features(c: np.ndarray, h: np.ndarray, lo: np.ndarray, v: np.ndarray,
                   o: np.ndarray | None = None, trades: np.ndarray | None = None) -> dict[str, np.ndarray]:
    n = len(c)
    lc = np.log(c)
    o = c if o is None else o
    trades = np.zeros(n) if trades is None else trades

    def back(k: int) -> np.ndarray:
        out = np.full(n, np.nan)
        if n > k:
            out[k:] = lc[k:] - lc[:-k]
        return out

    dr = np.r_[np.nan, np.diff(lc)]
    prior_avg_v = np.r_[np.nan, _roll_mean(v, 20)[:-1]]
    hi60, lo60 = _roll(c, 60, "max"), _roll(c, 60, "min")
    rng = hi60 - lo60
    with np.errstate(invalid="ignore", divide="ignore"):
        pos60 = np.where(rng > 0, (c - lo60) / rng, 0.5)
    pos60[np.isnan(hi60)] = np.nan
    liq_mn = _roll_mean(c * v / 1e6, 20)
    _, _, hist = ind.macd(c.tolist())
    sma20, sma50 = _roll_mean(c, 20), _roll_mean(c, 50)
    r1 = back(1)
    act = activity_features(c, o, h, lo, v, trades, r1)
    return {
        **act,
        "_s5": _roll(dr, 5, "std"),
        "r1": r1, "r5": back(5), "r20": back(20), "r60": back(60),
        "d20": c / sma20 - 1, "d50": c / sma50 - 1,
        "rsi": _arr(ind.rsi(c.tolist(), 14)),
        "macd": _arr(hist) / c,
        "vol20": _roll(dr, 20, "std"),
        "vratio": np.log1p(v) - np.log1p(prior_avg_v),
        "pos60": pos60,
        "liq": np.log1p(liq_mn),
        "idle": _roll_mean((v == 0).astype(float), 20),
        # not model inputs:
        "_sigma": _roll(dr, 60, "std"),
        "_atr": _arr(ind.atr(h.tolist(), lo.tolist(), c.tolist(), 14)),
        "_liq_mn": liq_mn,
        "_sma20": sma20,
        "_sma50": sma50,
    }


def build_dataset() -> dict[str, Any]:
    raw, calendar = _load_panel()
    cal_idx = {d: i for i, d in enumerate(calendar)}
    nd = len(calendar)
    infos = {r["code"]: r for r in db.query(
        "SELECT code, face_value, year_end, extra_json, paid_up_capital, free_float FROM company_info")}
    events = news.load_events()
    from . import bdfactors

    bd_events = bdfactors.load_news_events()
    stocks = {}
    mkt: dict[int, list[tuple[float, float]]] = defaultdict(list)
    sect: dict[tuple[int, str], list[float]] = defaultdict(list)
    ups, moved, value = np.zeros(nd), np.zeros(nd), np.zeros(nd)
    ret_sum, ret_n, vol_sum = np.zeros(nd), np.zeros(nd), np.zeros(nd)
    for code, s in raw.items():
        c, o, h, lo, v, tr = (np.array(s[k], dtype=float) for k in ("c", "o", "h", "l", "v", "n"))
        di = np.array([cal_idx[d] for d in s["dates"]])
        np.add.at(value, di, c * v / 1e6)
        if len(c) < WARMUP + 5:
            continue
        f = stock_features(c, h, lo, v, o, tr)
        stocks[code] = {"f": f, "c": c, "v": v, "h": h, "lo": lo, "di": di, "dates": s["dates"], "sector": s["sector"],
                        "category": s["category"], "asset_type": s["asset_type"]}
        liquid = (f["_liq_mn"] >= 1.0) & (v > 0)
        r1 = f["r1"]
        mv = liquid & np.isfinite(r1) & (r1 != 0)
        np.add.at(moved, di[mv], 1)
        np.add.at(ups, di[mv & (r1 > 0)], 1)
        lr = liquid & np.isfinite(r1) & np.isfinite(f["vol20"])
        np.add.at(ret_sum, di[lr], r1[lr])
        np.add.at(vol_sum, di[lr], f["vol20"][lr])
        np.add.at(ret_n, di[lr], 1)
        for t in np.nonzero(liquid & np.isfinite(f["r20"]))[0]:
            mkt[int(di[t])].append((f["r5"][t], f["r20"][t]))
            sect[(int(di[t]), s["sector"])].append(f["r20"][t])
    with np.errstate(invalid="ignore", divide="ignore"):
        mret = np.where(ret_n >= 20, ret_sum / np.maximum(ret_n, 1), 0.0)   # equal-weight market daily return
        mvol = np.where(ret_n >= 20, vol_sum / np.maximum(ret_n, 1), np.nan)  # average stock volatility
    # market-wide inputs per calendar day
    mkt5 = np.full(nd, np.nan)
    mkt20 = np.full(nd, np.nan)
    for k, vals in mkt.items():
        if len(vals) >= 20:
            mkt5[k] = np.mean([a for a, _ in vals])
            mkt20[k] = np.mean([b for _, b in vals])
    with np.errstate(invalid="ignore", divide="ignore"):
        breadth = np.where(moved >= 20, ups / moved, np.nan)
        breadth5 = _roll_mean(np.nan_to_num(breadth, nan=0.5), 5)
        mkt_turn = np.log((_roll_mean(value, 5) + 1) / (_roll_mean(value, 60) + 1))
    sector20 = {k: float(np.mean(v)) for k, v in sect.items() if len(v) >= 3}
    sizes = []
    for code, st in stocks.items():
        info = infos.get(code) or {}
        shares = (info.get("paid_up_capital") or 0) / (info.get("face_value") or 10)
        st["shares"] = shares if shares > 0 else None
        if st["shares"]:
            sizes.append(math.log(st["c"][-1] * shares / 1e6))
    size_fill = float(np.median(sizes)) if sizes else 7.0
    small_cut = float(np.percentile(sizes, 30)) if sizes else 0.0
    # market speculation gauge: share of turnover going to category-Z and small companies
    spec_val = np.zeros(nd)
    for st in stocks.values():
        small = st["shares"] and math.log(st["c"][-1] * st["shares"] / 1e6) <= small_cut
        if st["category"] == "Z" or small:
            np.add.at(spec_val, st["di"], st["c"] * st["v"] / 1e6)
    with np.errstate(invalid="ignore", divide="ignore"):
        spec_turn = np.nan_to_num(_roll_mean(spec_val, 5) / np.maximum(_roll_mean(value, 5), 1e-9), nan=0.0)
    # market regime per day (uses only data up to that day): the footprint of political / macro shocks
    reg_stress, reg_euph = np.zeros(nd), np.zeros(nd)
    reg_label = [""] * nd
    for r in bdfactors.regime_series(calendar, {"ret": mret, "breadth": np.nan_to_num(breadth, nan=0.5), "value": value}):
        k = cal_idx[r["date"]]
        reg_label[k] = r["regime"]
        reg_stress[k] = 1.0 if r["regime"] == "Stress" else 0.0
        reg_euph[k] = 1.0 if r["regime"] == "Euphoric" else 0.0
    for code, st in stocks.items():
        f, di = st["f"], st["di"]
        n = len(di)
        info = infos.get(code) or {}
        f["spec_turn"] = spec_turn[di]
        f["shock_beta"] = shock_beta_series(f["r1"], mret[di], st["v"] > 0)
        f["_reg_stress"], f["_reg_euph"] = reg_stress[di], reg_euph[di]
        # risk: rolling 60-day beta and stock-specific volatility against the equal-weight market
        x, yv = mret[di], np.nan_to_num(f["r1"])
        mx, my = _roll_mean(x, 60), _roll_mean(yv, 60)
        cov = _roll_mean(x * yv, 60) - mx * my
        var_x = _roll_mean(x * x, 60) - mx * mx
        var_y = _roll_mean(yv * yv, 60) - my * my
        with np.errstate(invalid="ignore", divide="ignore"):
            beta = np.where(var_x > 0, cov / var_x, 0.0)
            f["beta60"] = np.clip(beta, -2, 4)
            f["idio60"] = np.sqrt(np.maximum(var_y - beta * beta * var_x, 0.0))
        f["_mvol"] = mvol[di]
        f["size"] = np.log(st["c"] * st["shares"] / 1e6) if st["shares"] else np.full(n, size_fill)
        f.update(ownership_features(st["dates"], info))
        f["free_float"] = np.full(n, float(info.get("free_float") or 0.0))
        f["dow_sun"] = np.array([1.0 if d.weekday() == 6 else 0.0 for d in st["dates"]])
        f["dow_thu"] = np.array([1.0 if d.weekday() == 3 else 0.0 for d in st["dates"]])
        f["mkt5"], f["mkt20"] = mkt5[di], mkt20[di]
        f["breadth5"], f["mkt_turn"] = breadth5[di], mkt_turn[di]
        f["sector20"] = np.array([sector20.get((int(i), st["sector"]), mkt20[i]) for i in di])
        f["rel_sector"] = f["r20"] - f["sector20"]
        f["cat_a"] = np.full(n, 1.0 if st["category"] == "A" else 0.0)
        f["cat_z"] = np.full(n, 1.0 if st["category"] == "Z" else 0.0)
        f.update(fundamental_features(st["dates"], st["c"], fundamental_points(infos.get(code))))
        f.update({k: np.array(v) for k, v in news.features(st["dates"], events.get(code, [])).items()})
        f.update({k: np.array(v) for k, v in bdfactors.event_features(st["dates"], bd_events.get(code, [])).items()})
        with np.errstate(invalid="ignore", divide="ignore"):
            f["mf_disc"] = np.where(f["mf_nav"] > 0, np.clip(st["c"] / f["mf_nav"] - 1, -0.8, 1.0), 0.0)
    # valuation relative to sector peers on the same day (point-in-time fundamentals)
    peer: dict[tuple[int, str], list[tuple[float, float]]] = defaultdict(list)
    for st in stocks.values():
        f = st["f"]
        for t in np.nonzero(f["f_missing"] == 0)[0]:
            peer[(int(st["di"][t]), st["sector"])].append((f["ey"][t], f["bp"][t]))
    med = {k: (float(np.median([a for a, _ in v])), float(np.median([b for _, b in v]))) for k, v in peer.items() if len(v) >= 3}
    for code, st in stocks.items():
        f, di = st["f"], st["di"]
        m = [med.get((int(i), st["sector"])) for i in di]
        has = (f["f_missing"] == 0)
        f["rel_ey"] = np.array([(f["ey"][t] - m[t][0]) if m[t] and has[t] else 0.0 for t in range(len(di))])
        f["rel_bp"] = np.array([(f["bp"][t] - m[t][1]) if m[t] and has[t] else 0.0 for t in range(len(di))])
        st["X"] = np.column_stack([f[k] for k in FEATURE_KEYS])
        e = 1e-4
        st["V"] = np.column_stack([
            np.log(f["_s5"] + e), np.log(f["vol20"] + e), np.log(f["_sigma"] + e), np.log(f["_atr"] / st["c"] + e),
            np.log(f["hl5"] + e), f["idle"], f["liq"], np.log(st["c"]), f["circ20"], f["news_cnt"], f["record_soon"],
            np.abs(f["r1"]), np.abs(f["r5"]), f["idio60"], f["size"], np.log(f["_mvol"] + e), f["cat_z"],
            # Bangladesh / political inputs (VOL_BD_INPUTS)
            f["_reg_stress"], f["_reg_euph"], f["bd_query"], f["bd_rumour"], f["bd_toz"], f["bd_margin"],
            f["bd_state"], f["bd_court"], f["shock_beta"], f["govt_hold"] / 100, np.abs(f["circ_streak"]),
        ])
    return {"stocks": stocks, "calendar": calendar, "mret": mret, "regime": reg_label, "bd_events": bd_events,
            "news_coverage": sum(1 for c in stocks if events.get(c)), "fund_coverage": sum(1 for c in stocks if infos.get(c))}


def training_rows(ds: dict, horizon: int) -> dict[str, np.ndarray]:
    X, V, y, di, ti, liq, sig, ci, tt = [], [], [], [], [], [], [], [], []
    for k, code in enumerate(sorted(ds["stocks"])):
        st = ds["stocks"][code]
        n = len(st["c"])
        if n <= WARMUP + horizon:
            continue
        t = np.arange(WARMUP, n - horizon)
        ok = (np.isfinite(st["X"][t]).all(axis=1) & np.isfinite(st["V"][t]).all(axis=1) & (st["v"][t] > 0)
              & (st["f"]["idle"][t] <= 0.5) & np.isfinite(st["f"]["_sigma"][t]) & (st["f"]["_sigma"][t] > 0))
        t = t[ok]
        if not len(t):
            continue
        X.append(st["X"][t])
        V.append(st["V"][t])
        y.append(np.log(st["c"][t + horizon] / st["c"][t]))
        di.append(st["di"][t])
        ti.append(st["di"][t + horizon])
        liq.append(st["f"]["_liq_mn"][t])
        sig.append(st["f"]["_sigma"][t])
        ci.append(np.full(len(t), k))   # which share (index into sorted codes) and which session:
        tt.append(t)                    # needed to add the Bangladesh factors row by row
    if not X:
        return {}
    return {"X": np.vstack(X), "V": np.vstack(V), "y": np.concatenate(y), "di": np.concatenate(di),
            "ti": np.concatenate(ti), "liq": np.concatenate(liq), "sigma": np.concatenate(sig),
            "ci": np.concatenate(ci), "tt": np.concatenate(tt)}


class VolModel:
    """Expected size of the move: ridge regression of log(|return| + 0.2%) on volatility inputs.
    sigma_hat = exp(prediction). Quantiles of z = return / sigma_hat are then measured, either
    for all stocks together or separately for five buckets of sigma_hat (calm ... jumpy)."""

    def fit(self, V: np.ndarray, y: np.ndarray) -> "VolModel":
        self.mu, self.sd = V.mean(axis=0), V.std(axis=0)
        self.sd[self.sd == 0] = 1.0
        Z = np.hstack([np.ones((len(V), 1)), (V - self.mu) / self.sd])
        pen = np.eye(Z.shape[1])
        pen[0, 0] = 0.0
        self.b = np.linalg.solve(Z.T @ Z + 1e-3 * len(V) * pen, Z.T @ np.log(np.abs(y) + 0.002))
        s = self.sigma(V)
        z = y / s
        self.q_all = dict(zip((f"q{q}" for q in QUANTILES), np.percentile(z, QUANTILES)))
        self.edges = np.percentile(s, [20, 40, 60, 80])
        b = np.searchsorted(self.edges, s)
        self.q_bins = [dict(zip((f"q{q}" for q in QUANTILES), np.percentile(z[b == i], QUANTILES)))
                       if (b == i).sum() > 200 else self.q_all for i in range(5)]
        return self

    def sigma(self, V: np.ndarray) -> np.ndarray:
        Z = np.hstack([np.ones((len(V), 1)), (V - self.mu) / self.sd])
        return np.exp(Z @ self.b)

    def quantiles(self, s: float, binned: bool) -> dict[str, float]:
        return self.q_bins[int(np.searchsorted(self.edges, s))] if binned else self.q_all


class Ranger:
    """Produces (scale, quantiles) for a stock/day with the chosen range method:
    baseline, vol, vol_binned, and vol_bd / vol_bd_binned (adds the Bangladesh & political inputs)."""

    def __init__(self, method: str, rows: dict, horizon: int):
        self.method = method if method in RANGE_METHODS else "baseline"
        self.h = horizon
        self.cols = V_ALL if "_bd" in self.method else V_BASE
        if self.method == "baseline":
            self.q = band_quantiles(rows["y"], rows["sigma"], horizon)
        else:
            self.vm = VolModel().fit(rows["V"][:, self.cols], rows["y"])

    def at(self, st: dict, t: int) -> tuple[float, dict]:
        if self.method == "baseline":
            sigma = st["f"]["_sigma"][t]
            if not np.isfinite(sigma) or sigma <= 0:
                v20 = st["f"]["vol20"][t]
                sigma = v20 if np.isfinite(v20) and v20 > 0 else 0.02
            return float(sigma) * math.sqrt(self.h), self.q
        v = st["V"][t][self.cols]
        if not np.isfinite(v).all():  # an input is missing: fall back to the stock's own volatility
            sigma = st["f"]["_sigma"][t]
            sigma = sigma if np.isfinite(sigma) and sigma > 0 else 0.02
            return float(sigma) * math.sqrt(self.h), self.vm.q_all
        s = float(self.vm.sigma(v[None, :])[0])
        return s, self.vm.quantiles(s, self.method.endswith("binned"))


def range_evaluate(rows: dict, horizon: int) -> dict:
    """Test the range methods on the same rolling unseen windows. The winner has the lowest
    interval score for the 68% range: width + (2/0.32) x how far the price ended outside it.
    The Bangladesh / political inputs are used only if the methods with them win."""
    dates = np.unique(rows["di"])
    stats: dict[str, list] = defaultdict(list)
    for f in range(FOLDS, 0, -1):
        if len(dates) < f * FOLD_DAYS + 20:
            continue
        start = dates[len(dates) - f * FOLD_DAYS]
        end = dates[len(dates) - (f - 1) * FOLD_DAYS - 1]
        tr = rows["ti"] < start
        te = (rows["di"] >= start) & (rows["di"] <= end)
        if tr.sum() < MIN_TRAIN_ROWS or te.sum() < 200:
            continue
        y = rows["y"][te]
        base_q = band_quantiles(rows["y"][tr], rows["sigma"][tr], horizon)
        cand = {"baseline": (rows["sigma"][te] * math.sqrt(horizon), [base_q] * len(y))}
        for name, cols in (("vol", V_BASE), ("vol_bd", V_ALL)):
            vm = VolModel().fit(rows["V"][tr][:, cols], rows["y"][tr])
            s_vol = vm.sigma(rows["V"][te][:, cols])
            bins = np.searchsorted(vm.edges, s_vol)
            cand[name] = (s_vol, [vm.q_all] * len(y))
            cand[name + "_binned"] = (s_vol, [vm.q_bins[b] for b in bins])
        for name, (s, qs) in cand.items():
            m = {}
            for level, (lo_k, hi_k) in BANDS.items():
                lo = np.array([q[lo_k] for q in qs]) * s
                hi = np.array([q[hi_k] for q in qs]) * s
                m[f"in{level}"] = float(((y >= lo) & (y <= hi)).mean())
                m[f"width{level}"] = float((np.exp(hi) - np.exp(lo)).mean())
                if level == 68:
                    lo_p, hi_p, yp = np.expm1(lo), np.expm1(hi), np.expm1(y)
                    alpha = 0.32
                    m["score"] = float(np.mean((hi_p - lo_p) + (2 / alpha) * np.maximum(lo_p - yp, 0)
                                               + (2 / alpha) * np.maximum(yp - hi_p, 0)))
            stats[name].append(m)
    if not stats:
        return {"method": "baseline", "methods": {}}
    methods = {name: {k: _r(_mean([x[k] for x in ms]) * 100, 2) for k in ms[0]} for name, ms in stats.items()}
    best = min(methods, key=lambda k: methods[k]["score"])
    plain = min((v["score"] for k, v in methods.items() if "_bd" not in k), default=None)
    with_bd = min((v["score"] for k, v in methods.items() if "_bd" in k), default=None)
    # interval-score change from adding the Bangladesh / political inputs (negative = better)
    bd_gain = _r((with_bd / plain - 1) * 100, 1) if plain and with_bd else None
    return {"method": best, "methods": methods, "bd_gain_pct": bd_gain}


# ============================================================================ models

class Model:
    """Winsorise + standardise, then ridge (expected log return) and logistic (P(up))."""

    def fit(self, X: np.ndarray, y: np.ndarray) -> "Model":
        self.lo = np.nanpercentile(X, 1, axis=0)
        self.hi = np.nanpercentile(X, 99, axis=0)
        Xc = np.clip(X, self.lo, self.hi)
        self.mu = Xc.mean(axis=0)
        self.sd = Xc.std(axis=0)
        self.sd[self.sd == 0] = 1.0
        Z = (Xc - self.mu) / self.sd
        n, d = Z.shape
        Zb = np.hstack([np.ones((n, 1)), Z])
        pen = np.eye(d + 1)
        pen[0, 0] = 0.0
        self.beta = np.linalg.solve(Zb.T @ Zb + 0.1 * n * pen, Zb.T @ y)
        moved = y != 0
        Zm, ym = Zb[moved], (y[moved] > 0).astype(float)
        w = np.zeros(d + 1)
        lam = 0.05 * len(ym)
        for _ in range(30):
            p = _sigmoid(Zm @ w)
            g = Zm.T @ (p - ym) + lam * (pen @ w)
            H = (Zm * (p * (1 - p))[:, None]).T @ Zm + lam * pen
            step = np.linalg.solve(H, g)
            w -= step
            if np.abs(step).max() < 1e-7:
                break
        self.w = w
        return self

    def z(self, X: np.ndarray) -> np.ndarray:
        return (np.clip(X, self.lo, self.hi) - self.mu) / self.sd

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        Z = self.z(X)
        return _sigmoid(self.w[0] + Z @ self.w[1:]), self.beta[0] + Z @ self.beta[1:]

    def contributions(self, X_row: np.ndarray) -> np.ndarray:
        return self.w[1:] * self.z(X_row[None, :])[0]


def band_quantiles(y: np.ndarray, sigma: np.ndarray, horizon: int) -> dict[str, float]:
    """Measured distribution of h-day log returns in units of the stock's own volatility."""
    z = y / (sigma * math.sqrt(horizon))
    z = z[np.isfinite(z)]
    return {f"q{q}": float(v) for q, v in zip(QUANTILES, np.percentile(z, QUANTILES))}


def _evaluate(rows: dict, horizon: int, calendar: list[date], cols: list[int] | None = None) -> dict | None:
    """Rolling walk-forward test: for each window, fit only on data whose outcome was already
    known before the window started, then score the window. `cols` restricts the inputs."""
    X = rows["X"] if cols is None else rows["X"][:, cols]
    dates = np.unique(rows["di"])
    folds = []
    for f in range(FOLDS, 0, -1):
        if len(dates) < f * FOLD_DAYS + 20:
            continue
        start = dates[len(dates) - f * FOLD_DAYS]
        end = dates[len(dates) - (f - 1) * FOLD_DAYS - 1]
        tr = rows["ti"] < start
        te = (rows["di"] >= start) & (rows["di"] <= end)
        if tr.sum() < MIN_TRAIN_ROWS or te.sum() < 200:
            continue
        m = Model().fit(X[tr], rows["y"][tr])
        q = band_quantiles(rows["y"][tr], rows["sigma"][tr], horizon)
        prob, ret = m.predict(X[te])
        y, sig = rows["y"][te], rows["sigma"][te] * math.sqrt(horizon)
        moved = y != 0
        up = (y[moved] > 0).astype(float)
        base = float(up.mean())
        z = y / sig
        di, liq = rows["di"][te], rows["liq"][te]
        ics, top, avg = [], [], []
        for d in np.unique(di):
            s = (di == d) & (liq >= LIQUID_MN)
            if s.sum() < 30:
                continue
            if y[s].std() > 0 and ret[s].std() > 0:
                ics.append(float(np.corrcoef(_ranks(ret[s]), _ranks(y[s]))[0, 1]))
            best = np.argsort(-prob[s])[:10]
            top.append(float(np.mean(np.expm1(y[s][best]))))
            avg.append(float(np.mean(np.expm1(y[s]))))
        folds.append({
            "from": calendar[int(start)].isoformat(), "to": calendar[int(end)].isoformat(),
            "accuracy": float(((prob[moved] > 0.5) == (up == 1)).mean()),
            "naive": max(base, 1 - base), "up_share": base,
            "auc": _auc(up, prob[moved]),
            "ic": _mean(ics),
            "top10_excess": (np.mean(top) - np.mean(avg)) if top else None,
            "in68": float(((z >= q["q16"]) & (z <= q["q84"])).mean()),
            "in90": float(((z >= q["q5"]) & (z <= q["q95"])).mean()),
        })
    if not folds:
        return None
    acc, naive = _mean([f["accuracy"] for f in folds]), _mean([f["naive"] for f in folds])
    auc, ic = _mean([f["auc"] for f in folds]), _mean([f["ic"] for f in folds])
    top = _mean([f["top10_excess"] for f in folds])
    ics = [f["ic"] for f in folds if f["ic"] is not None]
    reliable = bool(ic is not None and ic > 0.03 and ics and min(ics) > 0 and auc and auc > 0.53 and top and top > 0)
    return {
        "test_from": folds[0]["from"], "test_to": folds[-1]["to"], "windows": len(folds),
        "direction_accuracy": _r(acc * 100, 1), "naive_accuracy": _r(naive * 100, 1),
        "up_share": _r(_mean([f["up_share"] for f in folds]) * 100, 1),
        "auc": _r(auc, 3), "rank_ic": _r(ic, 3), "rank_ic_windows": [_r(x, 3) for x in ics],
        "top10_excess": _r(top * 100, 2) if top is not None else None,
        "range68_hit": _r(_mean([f["in68"] for f in folds]) * 100, 1),
        "range90_hit": _r(_mean([f["in90"] for f in folds]) * 100, 1),
        "reliable": reliable,
    }


def input_group_test(rows: dict, horizon: int, calendar: list[date], base: dict | None) -> list[dict]:
    """What each input group adds: re-run the walk-forward test without that group.
    Positive 'adds' = the model ranks stocks better WITH this group on unseen data."""
    if not base or base.get("rank_ic") is None:
        return []
    out = []
    all_cols = list(range(len(FEATURE_KEYS)))
    for group, idx in GROUPS.items():
        cols = [c for c in all_cols if c not in idx]
        res = _evaluate(rows, horizon, calendar, cols)
        if not res or res.get("rank_ic") is None:
            continue
        out.append({
            "group": group, "label": GROUP_LABELS[group], "inputs": len(idx),
            "adds_rank_ic": _r(base["rank_ic"] - res["rank_ic"], 3),
            "adds_auc": _r((base["auc"] or 0.5) - (res["auc"] or 0.5), 3),
            "adds_accuracy": _r((base["direction_accuracy"] or 0) - (res["direction_accuracy"] or 0), 1),
        })
    return sorted(out, key=lambda x: -(x["adds_rank_ic"] or 0))


# ============================================================================ short-term setup rule

def is_setup(close: float, sma20: float | None, sma50: float | None, rsi: float | None) -> bool:
    """Uptrend + pullback: above the 50-day average, 20-day above 50-day, RSI 40-65,
    and no more than 5% above the 20-day average (not chasing)."""
    if not all(x is not None and math.isfinite(x) for x in (close, sma20, sma50, rsi)):
        return False
    return close > sma50 and sma20 > sma50 and 40 <= rsi <= 65 and close <= sma20 * 1.05


def rule_backtest(ds: dict) -> dict:
    """How the short-term setup did historically vs the average liquid share."""
    recs: dict[int, list] = {5: [], 20: []}
    for st in ds["stocks"].values():
        if st["asset_type"] != "EQ" or st["category"] == "Z":
            continue
        c, v, f, di = st["c"], st["v"], st["f"], st["di"]
        n = len(c)
        rule = ((c > f["_sma50"]) & (f["_sma20"] > f["_sma50"]) & (f["rsi"] >= 40) & (f["rsi"] <= 65)
                & (c <= f["_sma20"] * 1.05))
        ok = (v > 0) & (f["_liq_mn"] >= LIQUID_MN)
        for h in recs:
            t = np.arange(WARMUP, n - h)
            t = t[ok[t]]
            if len(t):
                recs[h].append(np.column_stack([di[t], rule[t], c[t + h] / c[t] - 1]))
    out = {}
    for h, parts in recs.items():
        if not parts:
            continue
        r = np.vstack(parts)
        d, rule, ret = r[:, 0], r[:, 1] > 0, r[:, 2]
        excess, days = [], []
        for day in np.unique(d):
            s = d == day
            if (s & rule).sum() >= 3:
                excess.append(ret[s & rule].mean() - ret[s].mean())
                days.append(day)
        if not excess:
            continue
        ex = np.array(excess)
        recent = ex[-60:]
        out[str(h)] = {
            "days": len(ex),
            "avg_excess": _r(float(ex.mean()) * 100, 2),
            "beat_rate": _r(float((ex > 0).mean()) * 100, 0),
            "recent_excess": _r(float(recent.mean()) * 100, 2),
            "picks_up": _r(float((ret[rule] > 0).mean()) * 100, 0),
            "all_up": _r(float((ret > 0).mean()) * 100, 0),
            "picks_avg": _r(float(ret[rule].mean()) * 100, 2),
            "all_avg": _r(float(ret.mean()) * 100, 2),
        }
    return out


# ============================================================================ daily run

PRED_INSERT = """INSERT INTO predictions (code, trade_date, horizon, close, prob_up, exp_ret, sigma,
    median, low30, high30, low50, high50, low68, high68, low90, high90, atr, liq_mn, hist_up, drivers, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def _hist_up(c: np.ndarray, h: int, window: int = 250) -> float | None:
    c = c[-(window + h):]
    if len(c) <= h + 20:
        return None
    r = c[h:] / c[:-h] - 1
    r = r[r != 0]
    return float((r > 0).mean()) if len(r) else None


def _predict_row(code: str, model: Model, ranger: Ranger, st: dict, t: int, h: int, day: date, source: str,
                 explain: bool = True) -> tuple | None:
    x = st["X"][t]
    if not np.isfinite(x).all():
        return None
    prob, ret = model.predict(x[None, :])
    s, q = ranger.at(st, t)
    sigma = s / math.sqrt(h)  # stored as a per-session figure
    close = float(st["c"][t])
    drivers = []
    if explain:
        contrib = model.contributions(x)
        drivers = [
            {"key": FEATURE_KEYS[i], "label": FEATURES[i][1], "group": FEATURES[i][3],
             "value": fmt_feature(FEATURES[i][2], float(x[i])), "impact": round(float(contrib[i]), 4)}
            for i in np.argsort(-np.abs(contrib))[:8] if abs(contrib[i]) > 1e-4
        ]
    atr, liq = st["f"]["_atr"][t], st["f"]["_liq_mn"][t]

    def px(k: str) -> float:
        return round(close * math.exp(q[k] * s), 2)

    return (
        code, day, h, close, round(float(prob[0]), 4), round(math.expm1(float(ret[0])) * 100, 3),
        float(sigma), px("q50"), px("q35"), px("q65"), px("q25"), px("q75"), px("q16"), px("q84"), px("q5"), px("q95"),
        float(atr) if np.isfinite(atr) else None, float(liq) if np.isfinite(liq) else None,
        _hist_up(st["c"][: t + 1], h) if explain else None, json.dumps(drivers), source,
    )


def _long_rows(code: str, ranger: Ranger, st: dict, t: int, base_h: int, day: date) -> list[tuple]:
    """6-month / 1-year range estimates: the base (3-month) scale grown with sqrt(time), same
    measured shape. No direction model (prob/expected return left empty)."""
    s_base, q = ranger.at(st, t)
    close = float(st["c"][t])
    atr, liq = st["f"]["_atr"][t], st["f"]["_liq_mn"][t]
    rows = []
    for lh in LONG_HORIZONS:
        s = s_base * math.sqrt(lh / base_h)

        def px(k: str) -> float:
            return round(close * math.exp(q[k] * s), 2)

        rows.append((
            code, day, lh, close, None, None, s / math.sqrt(lh),
            px("q50"), px("q35"), px("q65"), px("q25"), px("q75"), px("q16"), px("q84"), px("q5"), px("q95"),
            float(atr) if np.isfinite(atr) else None, float(liq) if np.isfinite(liq) else None,
            _hist_up(st["c"][: t + 1], lh), "[]", "live",
        ))
    return rows


def run(trade_date: date | None = None, group_test: bool = True) -> dict:
    """Daily: train + test every horizon, test each input group, store today's forecasts,
    then check all matured forecasts against the real closing prices."""
    started = datetime.now()
    ds = build_dataset()
    calendar = ds["calendar"]
    if not calendar:
        return {"status": "skipped", "message": "No price data"}
    trade_date = trade_date or calendar[-1]
    meta: dict[str, Any] = {
        "trained_at": started.isoformat(timespec="seconds"),
        "trade_date": trade_date.isoformat(),
        "history_from": calendar[0].isoformat(),
        "sessions": len(calendar),
        "stocks": len(ds["stocks"]),
        "news_coverage": ds["news_coverage"],
        "fund_coverage": ds["fund_coverage"],
        "news_items": db.scalar("SELECT COUNT(*) FROM news"),
        "inputs": [{"key": k, "label": l, "group": g} for k, l, _, g in FEATURES],
        "vol_inputs": VOL_INPUTS,
        "vol_bd_inputs": VOL_BD_INPUTS,
        "group_labels": GROUP_LABELS,
        "horizons": {},
        "setup_rule": rule_backtest(ds),
    }
    live_codes = [c for c, st in ds["stocks"].items() if st["dates"][-1] == trade_date]
    out_rows: list[tuple] = []
    for h, label in HORIZONS.items():
        rows = training_rows(ds, h)
        if not rows or len(rows["y"]) < MIN_TRAIN_ROWS:
            meta["horizons"][str(h)] = {"label": label, "available": False}
            continue
        backtest = _evaluate(rows, h, calendar)
        ranges = range_evaluate(rows, h)
        chosen = ranges["methods"].get(ranges["method"], {})
        if backtest and chosen:
            backtest.update({f"range{lv}_hit": chosen.get(f"in{lv}") for lv in BANDS})
            backtest.update({f"width{lv}": chosen.get(f"width{lv}") for lv in BANDS})
        model = Model().fit(rows["X"], rows["y"])
        ranger = Ranger(ranges["method"], rows, h)
        meta["horizons"][str(h)] = {
            "label": label, "available": True, "n_rows": int(len(rows["y"])),
            "quantiles": band_quantiles(rows["y"], rows["sigma"], h),
            "range_method": ranges["method"], "range_methods": ranges["methods"], "bd_range_gain_pct": ranges.get("bd_gain_pct"),
            "backtest": backtest, "reliable": bool(backtest and backtest["reliable"]),
            "input_groups": input_group_test(rows, h, calendar, backtest) if group_test and h in (5, 20) else [],
        }
        for code in live_codes:
            st = ds["stocks"][code]
            row = _predict_row(code, model, ranger, st, len(st["c"]) - 1, h, trade_date, "live")
            if row:
                out_rows.append(row)
                if h == max(HORIZONS):
                    out_rows.extend(_long_rows(code, ranger, st, len(st["c"]) - 1, h, trade_date))
    for lh, label in LONG_HORIZONS.items():
        meta["horizons"][str(lh)] = {"label": label, "available": True, "extrapolated": True,
                                     "from_horizon": max(HORIZONS), "reliable": False}
    db.execute("DELETE FROM predictions WHERE trade_date=%s", (trade_date,))
    db.executemany(PRED_INSERT, out_rows)
    meta["predicted"] = len({r[0] for r in out_rows})
    methods = {int(h): v.get("range_method", "baseline") for h, v in meta["horizons"].items() if v.get("available")}
    if not db.scalar("SELECT COUNT(*) FROM predictions WHERE source='backtest' AND low50 IS NOT NULL"):
        meta["simulated"] = simulate_history(ds, methods=methods)  # first run: build a walk-forward record
    meta["checks"] = run_checks()
    try:
        from . import tradeplan

        tradeplan.build_tables(ds)
    except Exception as e:  # trade tables are an add-on; never fail the forecast run
        log.warning("Trade tables failed: %s", e)
    meta["seconds"] = round((datetime.now() - started).total_seconds(), 1)
    db.set_state("predict_model", meta)
    try:
        db.set_state("predict_live", live_track())
    except Exception as e:  # informational only
        log.warning("Live track record failed: %s", e)
    log.info("Forecasts for %s: %d stocks in %ss", trade_date, meta["predicted"], meta["seconds"])
    return {"status": "ok", "trade_date": trade_date.isoformat(), "stocks": meta["predicted"], "seconds": meta["seconds"]}


def simulate_history(ds: dict | None = None, days: int = 40, horizons: tuple = (1, 5, 20),
                     methods: dict[int, str] | None = None) -> int:
    """Walk-forward forecasts for the last `days` sessions: each day's model is trained only on
    outcomes known before that day. Stored as source='backtest' (replaces earlier walk-forward
    rows, never touches live rows)."""
    ds = ds or build_dataset()
    if methods is None:
        hz = meta().get("horizons", {})
        methods = {int(h): v.get("range_method", "baseline") for h, v in hz.items() if v.get("available")}
    db.execute("DELETE FROM predictions WHERE source='backtest'")
    db.execute("DELETE FROM prediction_checks WHERE source='backtest'")
    cal = ds["calendar"]
    cal_idx = {d: i for i, d in enumerate(cal)}
    targets = cal[-(days + 1):-1]
    pos = {code: {int(d): t for t, d in enumerate(st["di"])} for code, st in ds["stocks"].items()}
    out: list[tuple] = []
    for h in horizons:
        rows = training_rows(ds, h)
        if not rows:
            continue
        for d in targets:
            di = cal_idx[d]
            tr = rows["ti"] < di
            if tr.sum() < MIN_TRAIN_ROWS:
                continue
            model = Model().fit(rows["X"][tr], rows["y"][tr])
            sub = {k: v[tr] for k, v in rows.items()}
            ranger = Ranger(methods.get(h, "baseline"), sub, h)
            for code, st in ds["stocks"].items():
                t = pos[code].get(di)
                if t is None or t < WARMUP:
                    continue
                row = _predict_row(code, model, ranger, st, t, h, d, "backtest", explain=False)
                if row:
                    out.append(row)
    db.executemany(PRED_INSERT.replace("INSERT INTO", "INSERT IGNORE INTO"), out)
    log.info("Walk-forward history: %d forecasts for %d sessions", len(out), len(targets))
    return len(out)


def run_checks(days: int = 200) -> int:
    """Compare every matured forecast with the real close; store daily summaries."""
    cal = _calendar()
    if not cal:
        return 0
    rows = db.query(
        "SELECT code, trade_date, horizon, close, prob_up, median, low50, high50, low68, high68, low90, high90, source "
        "FROM predictions WHERE trade_date >= %s",
        (cal[-1] - timedelta(days=days),),
    )
    groups: dict[tuple, list] = defaultdict(list)
    for r in _matured(rows, cal):
        groups[(r["target_date"], r["horizon"], r["source"])].append(r)
    out = []
    for (td, h, src), rs in groups.items():
        decided = [r for r in rs if r["hit"] is not None]
        with50 = [r for r in rs if r["in50"] is not None]
        out.append((
            td, h, src, rs[0]["trade_date"], len(rs),
            round(sum(r["in50"] for r in with50) / len(with50) * 100, 2) if with50 else None,
            round(sum(r["in68"] for r in rs) / len(rs) * 100, 2),
            round(sum(r["in90"] for r in rs) / len(rs) * 100, 2),
            len(decided), sum(1 for r in decided if r["hit"]),
            round(float(np.mean([abs(r["actual"] - r["median"]) / r["close"] * 100 for r in rs])), 3),
            round(float(np.mean([(r["actual"] - r["median"]) / r["close"] * 100 for r in rs])), 3),
            round(float(np.mean([(r["high50"] - r["low50"]) / r["close"] * 100 for r in with50])), 3) if with50 else None,
            round(float(np.mean([(r["high68"] - r["low68"]) / r["close"] * 100 for r in rs])), 3),
        ))
    db.executemany(
        "INSERT INTO prediction_checks (check_date, horizon, source, made_on, n, in50, in68, in90, dir_n, dir_hits, "
        "mae_pct, bias_pct, width50, width68) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE made_on=VALUES(made_on), n=VALUES(n), in50=VALUES(in50), in68=VALUES(in68), "
        "in90=VALUES(in90), dir_n=VALUES(dir_n), dir_hits=VALUES(dir_hits), mae_pct=VALUES(mae_pct), "
        "bias_pct=VALUES(bias_pct), width50=VALUES(width50), width68=VALUES(width68)",
        out,
    )
    return len(out)


# ============================================================================ live track record

def _calendar() -> list[date]:
    return [r["trade_date"] for r in db.query("SELECT DISTINCT trade_date FROM daily_prices ORDER BY trade_date")]


def _matured(rows: list[dict], cal: list[date]) -> list[dict]:
    """Attach the actual outcome to stored forecasts whose horizon has passed."""
    idx = {d: i for i, d in enumerate(cal)}
    need: set = set()
    for r in rows:
        i = idx.get(r["trade_date"])
        if i is not None and i + r["horizon"] < len(cal):
            r["target_date"] = cal[i + r["horizon"]]
            need.add(r["target_date"])
    if not need:
        return []
    ph = ",".join(["%s"] * len(need))
    closes = {(x["code"], x["trade_date"]): x["close"]
              for x in db.query(f"SELECT code, trade_date, close FROM daily_prices WHERE trade_date IN ({ph})", list(need))}
    out = []
    for r in rows:
        actual = closes.get((r["code"], r.get("target_date")))
        if actual is None:
            continue
        r["actual"] = actual
        r["actual_ret"] = round((actual / r["close"] - 1) * 100, 2)
        r["hit"] = (None if actual == r["close"] or r.get("prob_up") is None
                    else (actual > r["close"]) == (r["prob_up"] > 0.5))
        r["in68"] = r["low68"] <= actual <= r["high68"]
        r["in90"] = (r.get("low90") or r["low68"]) <= actual <= (r.get("high90") or r["high68"])
        r["in50"] = (r["low50"] <= actual <= r["high50"]) if r.get("low50") is not None else None
        out.append(r)
    return out


def live_track(days: int = 180) -> dict:
    cal = _calendar()
    if not cal:
        return {}
    rows = db.query(
        "SELECT code, trade_date, horizon, close, prob_up, low68, high68, low90, high90 FROM predictions "
        "WHERE trade_date >= %s AND source='live'",
        (cal[-1] - timedelta(days=days),),
    )
    done = _matured(rows, cal)
    out = {}
    for h in HORIZONS:
        hs = [r for r in done if r["horizon"] == h]
        decided = [r for r in hs if r["hit"] is not None]
        out[str(h)] = {
            "checked": len(hs),
            "days": len({r["trade_date"] for r in hs}),
            "direction_accuracy": round(sum(r["hit"] for r in decided) / len(decided) * 100, 1) if decided else None,
            "range68_hit": round(sum(r["in68"] for r in hs) / len(hs) * 100, 1) if hs else None,
        }
    return out


def stock_track(code: str, limit: int = 15) -> list[dict]:
    rows = db.query(
        "SELECT code, trade_date, horizon, close, prob_up, median, low50, high50, low68, high68, low90, high90, source "
        "FROM predictions WHERE code=%s AND horizon IN (1, 5) ORDER BY trade_date DESC LIMIT 160",
        (code,),
    )
    done = _matured(rows, _calendar())
    done.sort(key=lambda r: (r["target_date"], r["horizon"]), reverse=True)
    keys = ("trade_date", "horizon", "close", "median", "low50", "high50", "low68", "high68", "prob_up", "target_date",
            "actual", "actual_ret", "hit", "in50", "in68", "in90", "source")
    return [{k: r.get(k) for k in keys} for r in done[:limit]]


def accuracy_overview(days: int = 120) -> dict:
    """Daily forecast-vs-actual summaries (from prediction_checks) plus totals per horizon."""
    rows = db.query(
        "SELECT * FROM prediction_checks WHERE check_date >= CURDATE() - INTERVAL %s DAY ORDER BY check_date, horizon",
        (days,),
    )
    totals = {}
    for h in ALL_HORIZONS:
        for src in ("live", "backtest"):
            rs = [r for r in rows if r["horizon"] == h and r["source"] == src]
            if not rs:
                continue
            n = sum(r["n"] for r in rs)
            dn = sum(r["dir_n"] for r in rs)
            r50 = [r for r in rs if r.get("in50") is not None]
            n50 = sum(r["n"] for r in r50)
            totals[f"{h}_{src}"] = {
                "horizon": h, "source": src, "days": len(rs), "forecasts": n,
                "in50": round(sum(r["in50"] * r["n"] for r in r50) / n50, 1) if n50 else None,
                "width50": round(sum(r["width50"] * r["n"] for r in r50) / n50, 2) if n50 else None,
                "width68": round(sum((r.get("width68") or 0) * r["n"] for r in rs) / n, 2) if n else None,
                "in68": round(sum(r["in68"] * r["n"] for r in rs) / n, 1) if n else None,
                "in90": round(sum(r["in90"] * r["n"] for r in rs) / n, 1) if n else None,
                "direction": round(sum(r["dir_hits"] for r in rs) / dn * 100, 1) if dn else None,
                "mae": round(sum(r["mae_pct"] * r["n"] for r in rs) / n, 2) if n else None,
            }
    return {"checks": rows, "totals": totals,
            "latest": max((r["check_date"] for r in rows), default=None)}


def accuracy_details(check_date: date, horizon: int) -> dict:
    """Every stock's forecast for `check_date` (made `horizon` sessions earlier) vs the real close."""
    cal = _calendar()
    idx = {d: i for i, d in enumerate(cal)}
    if check_date not in idx or idx[check_date] - horizon < 0:
        return {"made_on": None, "rows": []}
    made_on = cal[idx[check_date] - horizon]
    preds = db.query(
        "SELECT p.code, i.name, i.sector, i.category, p.close, p.median, p.low50, p.high50, p.low68, p.high68, "
        "p.low90, p.high90, p.prob_up, p.source FROM predictions p JOIN instruments i ON i.code=p.code "
        "WHERE p.trade_date=%s AND p.horizon=%s",
        (made_on, horizon),
    )
    actual = {r["code"]: r for r in db.query(
        "SELECT code, close, volume FROM daily_prices WHERE trade_date=%s", (check_date,))}
    out = []
    for p in preds:
        a = actual.get(p["code"])
        if not a:
            continue
        act = a["close"]
        out.append({
            **p, "actual": act, "traded": bool(a["volume"]),
            "actual_ret": round((act / p["close"] - 1) * 100, 2),
            "error_pct": round((act - p["median"]) / p["close"] * 100, 2),
            "in50": (p["low50"] <= act <= p["high50"]) if p["low50"] is not None else None,
            "in68": p["low68"] <= act <= p["high68"], "in90": p["low90"] <= act <= p["high90"],
            "dir_hit": None if act == p["close"] or p["prob_up"] is None else (act > p["close"]) == (p["prob_up"] > 0.5),
        })
    return {"made_on": made_on, "check_date": check_date, "horizon": horizon, "rows": out}


# ============================================================================ API helpers

def latest_date() -> date | None:
    return db.scalar("SELECT MAX(trade_date) FROM predictions")


def meta() -> dict:
    return db.get_state("predict_model") or {}


def forecast(code: str) -> dict | None:
    d = latest_date()
    if not d:
        return None
    rows = db.query("SELECT * FROM predictions WHERE code=%s AND trade_date=%s ORDER BY horizon", (code, d))
    if not rows:
        return None
    m = meta()
    hz = m.get("horizons", {})
    by_h = {r["horizon"]: r for r in rows}
    close, sigma = rows[0]["close"], rows[0]["sigma"] or 0.02
    edges = ("median", "low30", "high30", "low50", "high50", "low68", "high68", "low90", "high90")
    # cone: interpolate each band edge (as a log move) between the forecast horizons, in sqrt(time)
    knots = [(0, {e: 0.0 for e in edges})]
    for h in sorted(by_h):
        r = by_h[h]
        knots.append((h, {e: math.log(r[e] / close) if r.get(e) else math.log(r["low68" if "low" in e else "high68"] / close)
                          for e in edges}))
    days = next_trading_days(d, max(by_h))
    cone = []
    for k, day in enumerate(days, 1):
        (k0, a), (k1, b) = next((knots[i], knots[i + 1]) for i in range(len(knots) - 1) if knots[i + 1][0] >= k)
        w = (math.sqrt(k) - math.sqrt(k0)) / (math.sqrt(k1) - math.sqrt(k0)) if k1 > k0 else 1.0
        cone.append({"time": day, **{e: round(close * math.exp(a[e] + (b[e] - a[e]) * w), 2) for e in edges}})
    horizons = []
    for h, label in ALL_HORIZONS.items():
        r = by_h.get(h)
        if not r:
            continue
        info = hz.get(str(h), {})
        horizons.append({
            "h": h, "label": label, "target_date": days[h - 1] if h <= len(days) else None,
            "term": "short" if h in SHORT_TERM else "long", "extrapolated": h in LONG_HORIZONS,
            "median": r["median"], "low30": r.get("low30"), "high30": r.get("high30"),
            "low50": r.get("low50"), "high50": r.get("high50"),
            "low68": r["low68"], "high68": r["high68"], "low90": r["low90"], "high90": r["high90"],
            "prob_up": r["prob_up"], "exp_ret": r["exp_ret"], "hist_up": r["hist_up"],
            "reliable": bool(info.get("reliable")), "backtest": info.get("backtest"),
            "range_method": info.get("range_method"),
            "drivers": json.loads(r["drivers"] or "[]"),
        })
    def span(hs: tuple) -> dict | None:
        ks = [h for h in hs if h in by_h]
        if not ks:
            return None
        return {"from": days[0], "to": days[max(ks) - 1], "horizons": [ALL_HORIZONS[h] for h in ks]}

    short_span, long_span = span(SHORT_TERM), span(LONG_TERM)
    if long_span:  # long term starts where short term ends
        long_span["from"] = days[min(len(days), max(SHORT_TERM) + 1) - 1]
    return {"trade_date": d, "close": close, "sigma": sigma, "atr": rows[0]["atr"], "liq_mn": rows[0]["liq_mn"],
            "horizons": horizons, "cone": cone, "date_ranges": {"short": short_span, "long": long_span}}


def _dividend_years(div_history: list[dict]) -> tuple[int, int]:
    recent = sorted((x for x in div_history if isinstance(x, dict) and x.get("year")), key=lambda x: -int(x["year"]))[:5]
    paid = sum(1 for x in recent if (x.get("cash") or 0) > 0 or (x.get("stock") or 0) > 0)
    return paid, len(recent)


def _eps_growth(fin: list[dict]) -> tuple[float | None, bool]:
    eps = sorted((int(x["year"]), float(x["epsBasic"])) for x in fin
                 if isinstance(x, dict) and x.get("year") and x.get("epsBasic") is not None)[-4:]
    if len(eps) < 3:
        return None, False
    all_positive = all(e > 0 for _, e in eps)
    first, last = eps[0][1], eps[-1][1]
    years = (eps[-1][0] - eps[0][0]) or 1
    if first > 0 and last > 0:
        return (last / first) ** (1 / years) - 1, all_positive
    return (0.15 if last > first else -0.15), all_positive


def _pct_rank(values: list[float | None], higher_better: bool = True) -> list[float | None]:
    """Percentile rank 0..1 among the candidates (ties share the average rank)."""
    a = np.array([np.nan if v is None else (v if higher_better else -v) for v in values], dtype=float)
    ok = np.isfinite(a)
    out: list[float | None] = [None] * len(values)
    v = a[ok]
    if not len(v):
        return out
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(len(v))
    sv = v[order]
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2
        i = j + 1
    ranks /= max(1, len(v) - 1)
    for pos, r in zip(np.nonzero(ok)[0], ranks):
        out[pos] = float(r)
    return out


def _long_term(preds: dict, sigs: dict, inst: dict, info: dict, model_reliable: bool) -> list[dict]:
    """Regular-buying candidates: category-A, profitable, liquid. Each factor is scored by its
    percentile rank among these candidates, so the score means 'better than X% of peers'."""
    cands = []
    for code, ph in preds.items():
        i, s, ci = inst.get(code) or {}, sigs.get(code) or {}, info.get(code)
        p = ph.get(60) or ph.get(20)
        if not ci or not p or i.get("asset_type") != "EQ" or i.get("category") != "A":
            continue
        if (p["liq_mn"] or 0) < 2.0 or not ci.get("eps") or ci["eps"] <= 0:
            continue
        extra = json.loads(ci.get("extra_json") or "{}")
        close = p["close"]
        pe = ci.get("pe") or i.get("trailing_pe")
        paid, years = _dividend_years(extra.get("dividendHistory") or [])
        growth, all_pos = _eps_growth(extra.get("multiYearFinancials") or [])
        sma50, sma200 = s.get("sma50"), s.get("sma200")
        cands.append({
            "code": code, "i": i, "s": s, "ph": ph, "close": close,
            "pe": pe if pe and pe > 0 else None,
            "pb": close / ci["nav"] if ci.get("nav") and ci["nav"] > 0 else None,
            "dy": ci.get("dividend_yield") or 0.0,
            "paid": paid, "years": years,
            "consistency": (paid / years if years else 0.0) * (1.0 if years >= 3 else 0.7),
            "growth": growth, "all_pos": all_pos,
            "vol": (p["sigma"] or 0.02) * math.sqrt(245),
            "trend": (1.0 if sma50 > sma200 else (0.6 if close > sma200 else 0.25)) if sma50 and sma200 else 0.5,
            "liq": p["liq_mn"],
        })
    if not cands:
        return []
    r_pe = _pct_rank([c["pe"] for c in cands], higher_better=False)
    r_pb = _pct_rank([c["pb"] for c in cands], higher_better=False)
    r_dy = _pct_rank([c["dy"] for c in cands])
    r_gr = _pct_rank([c["growth"] for c in cands])
    r_vol = _pct_rank([c["vol"] for c in cands], higher_better=False)
    out = []
    for k, c in enumerate(cands):
        d = lambda x: 0.3 if x is None else x  # missing data counts as below average
        valuation = 0.6 * d(r_pe[k]) + 0.4 * d(r_pb[k])
        dividends = 0.5 * d(r_dy[k]) + 0.5 * c["consistency"]
        growth = d(r_gr[k]) * (1.0 if c["growth"] is None or c["all_pos"] else 0.5)
        stability = 0.5 * d(r_vol[k]) + 0.5 * c["trend"]
        ph = c["ph"]
        if model_reliable and 60 in ph:
            model_s = _clip01((ph[60]["prob_up"] - 0.4) / 0.3)
            total = 100 * (0.25 * valuation + 0.25 * dividends + 0.20 * growth + 0.20 * stability + 0.10 * model_s)
        else:
            total = 100 * (0.25 * valuation + 0.25 * dividends + 0.20 * growth + 0.30 * stability)
        why = []
        if c["pe"]:
            why.append(f"P/E {c['pe']:.1f}")
        if c["dy"]:
            why.append(f"Yield {c['dy']:.1f}%")
        if c["years"]:
            why.append(f"Dividend {c['paid']}/{c['years']} yrs")
        if c["growth"] is not None:
            why.append(f"EPS {c['growth'] * 100:+.0f}%/yr")
        if c["trend"] >= 1:
            why.append("Uptrend")
        i, s = c["i"], c["s"]
        out.append({
            "code": c["code"], "name": i.get("name"), "sector": i.get("sector"), "category": i.get("category"),
            "close": c["close"], "score": round(total, 1), "pe": _r(c["pe"], 1), "pb": _r(c["pb"], 2),
            "dividend_yield": c["dy"], "dividend_years": f"{c['paid']}/{c['years']}" if c["years"] else None,
            "eps_growth": _r(c["growth"] * 100, 1) if c["growth"] is not None else None,
            "volatility": _r(c["vol"] * 100, 1), "trend": c["trend"] >= 1,
            "range_3m": [ph[60]["low68"], ph[60]["high68"]] if 60 in ph else None,
            "range_1y": [ph[245]["low50"], ph[245]["high50"]] if 245 in ph and ph[245].get("low50") else None,
            "components": {"valuation": round(valuation * 100), "dividends": round(dividends * 100),
                           "growth": round(growth * 100), "stability": round(stability * 100)},
            "liq_mn": _r(c["liq"], 1), "label": s.get("label"), "signal_score": s.get("score"),
            "why": why,
        })
    out.sort(key=lambda x: -x["score"])
    return out


def picks(limit: int = 10) -> dict:
    """Short-term setups (1-4 weeks) and long-term regular-buying candidates."""
    d = latest_date()
    if not d:
        return {"short": [], "long": [], "trade_date": None}
    m = meta()
    reliable20 = bool(m.get("horizons", {}).get("20", {}).get("reliable"))
    reliable60 = bool(m.get("horizons", {}).get("60", {}).get("reliable"))
    preds: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in db.query("SELECT * FROM predictions WHERE trade_date=%s", (d,)):
        preds[r["code"]][r["horizon"]] = r
    sd = analysis.latest_signal_date()
    sigs = {s["code"]: s for s in db.query("SELECT * FROM signals WHERE trade_date=%s", (sd,))} if sd else {}
    inst = {r["code"]: r for r in db.query("SELECT code, name, sector, category, asset_type, trailing_pe FROM instruments")}
    info = {r["code"]: r for r in db.query("SELECT * FROM company_info")}
    circuit = settings.get_float("circuit_pct")
    days_ahead = next_trading_days(d, 245)

    short = []
    for code, ph in preds.items():
        i, s, p20 = inst.get(code) or {}, sigs.get(code) or {}, ph.get(20)
        if not p20 or i.get("asset_type") != "EQ" or i.get("category") == "Z" or (p20["liq_mn"] or 0) < LIQUID_MN:
            continue
        if (s.get("change_pct") or 0) >= circuit or not is_setup(s.get("close"), s.get("sma20"), s.get("sma50"), s.get("rsi")):
            continue
        close = p20["close"]
        atr = p20["atr"] or close * 0.03
        stop, target = close - 1.5 * atr, p20["high68"]
        rr = (target - close) / (close - stop) if close > stop else None
        short.append({
            "code": code, "name": i.get("name"), "sector": i.get("sector"), "category": i.get("category"),
            "close": close, "score": s.get("score"), "label": s.get("label"), "rsi": _r(s.get("rsi"), 1),
            "above_sma20_pct": _r((close / s["sma20"] - 1) * 100, 1) if s.get("sma20") else None,
            "ret_1m": s.get("ret_1m"), "liq_mn": _r(p20["liq_mn"], 1),
            "stop": round(stop, 2), "target": round(target, 2), "reward_risk": _r(rr, 2),
            "range_1w": [ph[5]["low68"], ph[5]["high68"]] if 5 in ph else None,
            "range_1m": [p20["low68"], p20["high68"]],
            "hold_until": days_ahead[19] if days_ahead else None,
            "prob_1m": p20["prob_up"], "model_reliable": reliable20,
        })
    if reliable20:
        short.sort(key=lambda x: -x["prob_1m"])
    else:
        short.sort(key=lambda x: (-(x["score"] or 0), -(x["liq_mn"] or 0)))

    long_ = _long_term(preds, sigs, inst, info, reliable60)
    chosen, per_sector = [], defaultdict(int)
    for x in long_:  # diversify: at most 3 per sector
        if per_sector[x["sector"]] < 3:
            chosen.append(x)
            per_sector[x["sector"]] += 1
        if len(chosen) == limit:
            break
    return {
        "trade_date": d, "short": short[:limit], "long": chosen,
        "date_ranges": {
            "short": {"from": days_ahead[0], "to": days_ahead[19]},
            "long": {"accumulate_to": days_ahead[59], "review": days_ahead[119], "to": days_ahead[244]},
        },
        "short_candidates": len(short), "long_candidates": len(long_),
        "liquid_mn": LIQUID_MN, "short_ranked_by": "model" if reliable20 else "trend strength",
    }
