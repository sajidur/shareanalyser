"""Bangladesh-specific market factors, each measured on DSE history before it is used.

1. Event studies - what shares did after Bangladesh-specific events, compared with the market:
     DSE query on unusual price/volume (the rumour / manipulation footprint), company clarification
     of media/online news (rumours), sponsor/director buy or sell declarations (insiders),
     downgrade to / upgrade from category Z, margin-loan prohibition, regulator action (inspection,
     penalty, board reconstitution), government / state / central-bank involvement in company news
     (political footprint), upper / lower circuit days, and record dates (ex-dividend).
2. Market regime - Calm / Nervous / Stress / Euphoric from breadth, turnover, drawdown and
   dispersion of the equal-weight market. Political and macro shocks show up here.
3. Political / regulatory events - elections from DSE's holiday list plus events the user records;
   the market's reaction around each is measured.
4. Speculation (pump-and-dump) risk score per share.
5. Political & state influence: central bank / regulator taking control of a board (dissolution,
   administrator, observer), court orders, companies reporting political disruption, government
   shareholding, each share's sensitivity to market sell-off days, and political exposure the
   USER records (with a source). The tool never guesses anyone's political affiliation.

Political influence cannot be measured directly; only its footprint in prices and disclosures.
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

from . import analysis, db

log = logging.getLogger(__name__)

# type -> (label, regex on summary + body, dedupe window in days)
EVENT_TYPES: dict[str, tuple[str, str, int]] = {
    "dse_query": ("DSE query on unusual price/volume", r"query response|in response to (?:the |a )?dse query|unusual price|unusual movement", 5),
    "rumour": ("Company clarified media / online news (rumour)", r"clarification on .{0,40}news|news published in", 5),
    "sponsor_buy": ("Sponsor / director BUY declaration", r"buy declaration|intention to buy", 10),
    "sponsor_sell": ("Sponsor / director SALE declaration", r"sale declaration|sell declaration|intention to sell", 10),
    "to_z": ("Downgraded to category Z", r"placed in ['‘’\"]?z['‘’\"]? category", 30),
    "from_z": ("Upgraded from category Z", r"from existing ['‘’\"]?z['‘’\"]? category", 30),
    "margin_ban": ("Margin loans prohibited", r"abstain from providing loan facilities|loan facilities to purchase", 30),
    "regulator": ("Regulator action (inspection / penalty)", r"inspection|penalt|\bfined\b|investigat", 10),
    "govt": ("Government / state / central bank in company news", r"\bgovernment\b|\bministry\b|state-owned|\bstate may\b|bangladesh bank|nationali[sz]", 10),
    "state_control": ("Central bank / regulator took control (board dissolved, administrator, observer)",
                      r"dissolv\w*\s+(?:the\s+)?(?:previous\s+|existing\s+)?board|board of directors[^.]{0,60}dissolv"
                      r"|dissolution of the board|\badministrators?\b|as an observer|observer on the board|bank resolution"
                      r"|reconstitut\w*[^.]{0,40}board[^.]{0,100}(?:bangladesh bank|commission|bsec)"
                      r"|(?:bangladesh bank|bsec|the commission)[^.]{0,100}reconstitut", 60),
    "political_hit": ("Results mention political unrest / disruption",
                      r"politic\w*\s+(?:unrest|upheaval|situation|instability|turmoil|crisis|change|transition|violence|disturbance)"
                      r"|(?:unrest|upheaval|instability|turmoil)[^.]{0,30}politic|due to[^.]{0,40}political"
                      r"|july[- ]august (?:2024 )?(?:movement|uprising|revolution)|mass uprising", 60),
    "court_hold": ("Court order / legal dispute (AGM, dividend or control)",
                   r"high court|court order|court division|writ petition|appellate division|order of the hon", 30),
    "wipeout": ("Declared non-viable / shareholders' capital written off",
                r"reduced to zero|reduction of (?:the )?entire paid-up capital|non-viable|okarjokar", 3650),
}
# political events stay relevant longer than a rumour: days an event counts as "active"
ACTIVE_DAYS = {"state_control": 180, "political_hit": 120, "court_hold": 90, "wipeout": 3650}
POLITICAL_KINDS = tuple(ACTIVE_DAYS)
SHOCK_DAY = -0.01   # equal-weight market down 1% or more in a session: a sell-off day

# political exposure the user records (from news they trust); never guessed by the tool
EXPOSURES = {
    "former_govt": "Owner / sponsor linked to the former government",
    "ruling": "Owner / sponsor linked to the current government",
    "opposition": "Owner / sponsor linked to an opposition party",
    "state_appointed": "Board appointed or controlled by the state / regulator",
    "under_action": "Owners face investigation, asset freeze, arrest or travel ban",
    "other": "Other political exposure",
}
RISKY_EXPOSURES = ("former_govt", "under_action")
PRICE_EVENTS = {
    "upper_circuit": "Upper circuit day (+9.5% or more)",
    "lower_circuit": "Lower circuit day (−9.5% or less)",
    "record_date": "Record date (ex-dividend)",
}
HORIZONS = (1, 5, 20)
SPEC_BUCKETS = ((0, 30, "Low"), (30, 60, "Elevated"), (60, 101, "High"))


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


def active_days(kind: str, default: int = 28) -> int:
    return ACTIVE_DAYS.get(kind, default)


def classify_news(summary: str, body: str) -> list[str]:
    # long bodies: results announcements explain deviations (e.g. political unrest) near the end
    text = f"{summary} {body[:3000]}".lower()
    found = [k for k, (_, rx, _) in EVENT_TYPES.items() if re.search(rx, text)]
    if "sponsor_buy" in found and "sponsor_sell" in found:  # 'buy declaration ... sell' edge cases
        found.remove("sponsor_sell" if "buy declaration" in text else "sponsor_buy")
    if "to_z" in found and "from_z" in found:  # "placed in 'A' category from existing 'Z'" also matches neither twice
        found.remove("to_z")
    return found


def load_news_events() -> dict[str, list[tuple[date, str]]]:
    """code -> [(date, event_type)] sorted, de-duplicated per type within its window."""
    raw: dict[str, list[tuple[date, str]]] = defaultdict(list)
    for r in db.query("SELECT code, news_date, summary, LEFT(body, 3000) AS body FROM news ORDER BY code, news_date"):
        for t in classify_news(r["summary"] or "", r["body"] or ""):
            raw[r["code"]].append((r["news_date"], t))
    out: dict[str, list[tuple[date, str]]] = {}
    for code, evs in raw.items():
        last: dict[str, date] = {}
        keep = []
        for d, t in sorted(evs):
            if t in last and (d - last[t]).days < EVENT_TYPES[t][2]:
                continue
            last[t] = d
            keep.append((d, t))
        out[code] = keep
    return out


# ============================================================================ price panel

def _panel() -> tuple[dict[str, dict], list[date], np.ndarray, dict]:
    types = ",".join(f"'{t}'" for t in sorted(analysis.DEBT_ASSET_TYPES))
    rows = db.query(
        "SELECT p.code, p.trade_date, p.close, p.high, p.low, p.volume, p.change_pct, i.category, i.asset_type "
        "FROM daily_prices p JOIN instruments i ON i.code=p.code "
        f"WHERE (i.asset_type IS NULL OR i.asset_type NOT IN ({types})) AND p.close > 0 ORDER BY p.code, p.trade_date"
    )
    panel: dict[str, dict] = {}
    for r in rows:
        s = panel.setdefault(r["code"], {"d": [], "c": [], "v": [], "chg": [], "cat": r["category"], "type": r["asset_type"]})
        s["d"].append(r["trade_date"])
        s["c"].append(float(r["close"]))
        s["v"].append(float(r["volume"] or 0))
        s["chg"].append(float(r["change_pct"]) if r["change_pct"] is not None else np.nan)
    cal = sorted({d for s in panel.values() for d in s["d"]})
    ci = {d: i for i, d in enumerate(cal)}
    n = len(cal)
    ret_sum, ret_n, ups, moved, value = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n)
    for s in panel.values():
        c, v = np.array(s["c"]), np.array(s["v"])
        di = np.array([ci[d] for d in s["d"]])
        s["c"], s["v"], s["di"], s["chg"] = c, v, di, np.array(s["chg"])
        lr = np.r_[np.nan, np.diff(np.log(c))]
        np.add.at(value, di, c * v / 1e6)
        ok = np.isfinite(lr) & (v > 0)  # every traded share counts in the equal-weight market
        np.add.at(ret_sum, di[ok], lr[ok])
        np.add.at(ret_n, di[ok], 1)
        mv = ok & (lr != 0)
        np.add.at(moved, di[mv], 1)
        np.add.at(ups, di[mv & (lr > 0)], 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        mret = np.where(ret_n >= 20, ret_sum / np.maximum(ret_n, 1), 0.0)
        breadth = np.where(moved >= 20, ups / np.maximum(moved, 1), 0.5)
    market = {"ret": mret, "cum": np.cumsum(mret), "breadth": breadth, "value": value}
    return panel, cal, mret, market


def _fwd(s: dict, t: int, k: int, market: dict) -> tuple[float | None, float | None]:
    """(stock return, excess over equal-weight market) from session t to t+k."""
    if t + k >= len(s["c"]) or t < 0:
        return None, None
    r = s["c"][t + k] / s["c"][t] - 1
    m = math.exp(market["cum"][s["di"][t + k]] - market["cum"][s["di"][t]]) - 1
    return r, r - m


def _stats(rows: list[dict]) -> dict:
    out: dict[str, Any] = {"n": len(rows)}
    if not rows:
        return out
    pre = [x["pre20"] for x in rows if x["pre20"] is not None]
    out["pre20"] = _r(float(np.mean(pre)) * 100, 2) if pre else None
    for k in HORIZONS:
        ex = np.array([x[f"ex{k}"] for x in rows if x.get(f"ex{k}") is not None])
        if len(ex) < 5:
            continue
        # DSE returns are fat-tailed (a few shares multiply): judge on the median and on a
        # 5%/95% winsorised mean, not the raw mean
        lo, hi = np.percentile(ex, [5, 95])
        w = np.clip(ex, lo, hi)
        mean_w, sd_w = float(w.mean()), float(w.std(ddof=1)) if len(w) > 1 else 0.0
        t = mean_w / (sd_w / math.sqrt(len(w))) if sd_w > 0 else 0.0
        out[f"ex{k}"] = _r(float(np.median(ex)) * 100, 2)        # headline: median excess
        out[f"mean{k}"] = _r(float(ex.mean()) * 100, 2)          # raw mean (outlier-driven)
        out[f"wmean{k}"] = _r(mean_w * 100, 2)
        out[f"pos{k}"] = _r(float((ex > 0).mean()) * 100, 0)
        out[f"t{k}"] = _r(t, 2)
        out[f"n{k}"] = int(len(ex))
    t20 = out.get("t20") if out.get("t20") is not None else (out.get("t5") or 0)
    med = out.get("ex20") or 0
    # an effect counts only if median and trimmed mean agree and the typical (median) move is >= 1%
    agree = med * (out.get("wmean20") or 0) > 0 and abs(med) >= 1.0
    out["evidence"] = ("strong" if abs(t20) >= 2.5 and agree else "moderate" if abs(t20) >= 1.5 and agree else "weak")
    out["direction"] = ("negative" if (out.get("wmean20") or 0) < 0 else "positive") if out["evidence"] != "weak" else "unclear"
    return out


def _measure(code: str, d: date, s: dict, market: dict, before: bool = False) -> dict | None:
    """Event on date d: base = close of the last session on/before d (strictly before if `before`)."""
    t = bisect.bisect_left(s["d"], d) - 1 if before else bisect.bisect_right(s["d"], d) - 1
    if t < 20:
        return None
    row = {"code": code, "date": d, "pre20": s["c"][t] / s["c"][t - 20] - 1}
    for k in HORIZONS:
        _, ex = _fwd(s, t, k, market)
        row[f"ex{k}"] = ex
    return row


# ============================================================================ regime

def regime_series(cal: list[date], market: dict) -> list[dict]:
    mret, breadth, value = market["ret"], market["breadth"], market["value"]
    n = len(cal)
    idx = np.exp(np.cumsum(mret))
    out = []
    for i in range(n):
        if i < 60:
            continue
        m20 = idx[i] / idx[i - 20] - 1
        dd = idx[i] / idx[max(0, i - 59): i + 1].max() - 1
        b5 = float(breadth[i - 4: i + 1].mean())
        turn = float(value[i - 4: i + 1].mean() / max(value[i - 59: i + 1].mean(), 1e-9))
        vol20 = float(np.std(mret[i - 19: i + 1]))
        vol60 = float(np.std(mret[i - 59: i + 1])) or 1e-9
        if dd <= -0.08 or (m20 <= -0.05 and b5 < 0.40) or (vol20 / vol60 >= 1.6 and m20 < 0):
            reg = "Stress"
        elif m20 >= 0.08 and turn >= 1.3 and b5 >= 0.55:
            reg = "Euphoric"
        elif (m20 < 0 and b5 < 0.47) or turn < 0.7:
            reg = "Nervous"
        else:
            reg = "Calm"
        out.append({"date": cal[i], "index": float(idx[i]), "regime": reg, "m20": m20, "drawdown": dd,
                    "breadth5": b5, "turnover_ratio": turn, "vol_ratio": vol20 / vol60})
    return out


# ============================================================================ speculation risk

def spec_score(size_pct: float | None, free_float: float | None, category: str | None, r20: float | None,
               circ20: float, vol_ratio: float | None, query_recent: bool) -> tuple[int, list[str]]:
    pts, why = 0, []
    if size_pct is not None and size_pct < 0.30:
        pts += 20; why.append("small company (bottom 30% by market cap)")
    if free_float is not None and 0 < free_float < 30:
        pts += 15; why.append(f"low free float ({free_float:.0f}%)")
    if category == "Z":
        pts += 15; why.append("category Z")
    elif category == "B":
        pts += 5
    if r20 is not None and r20 > 0.30:
        pts += 20; why.append(f"up {r20 * 100:.0f}% in a month")
    elif r20 is not None and r20 > 0.15:
        pts += 10; why.append(f"up {r20 * 100:.0f}% in a month")
    if circ20 >= 2:
        pts += 15; why.append(f"{int(circ20)} circuit-limit days in 20 sessions")
    if vol_ratio is not None and vol_ratio >= 3:
        pts += 10; why.append(f"volume {vol_ratio:.1f}× normal")
    if query_recent:
        pts += 15; why.append("DSE queried unusual movement recently")
    return min(100, pts), why


# ============================================================================ political & state influence

def govt_holdings() -> dict[str, float]:
    """Latest published government shareholding % per company (DSE share pattern), where > 0."""
    from .predict import _PATTERN_DATE, dse_date

    out = {}
    for r in db.query("SELECT code, extra_json FROM company_info"):
        try:
            pats = json.loads(r["extra_json"] or "{}").get("sharePattern") or []
        except ValueError:
            continue
        best = None
        for p in pats:
            if not isinstance(p, dict) or not isinstance(p.get("pattern"), dict):
                continue
            m = _PATTERN_DATE.search(str(p.get("date") or ""))
            d = (dse_date(m.group(1)) if m else None) or date.min
            if best is None or d >= best[0]:
                best = (d, p["pattern"])
        g = float((best[1].get("government") or 0) if best else 0)
        if g > 0:
            out[r["code"]] = round(g, 2)
    return out


def shock_betas(panel: dict, market: dict, min_days: int = 5) -> dict[str, dict]:
    """How hard each share fell on market sell-off days (equal-weight market <= -1%), relative to
    the market: 1.0 = like the market, 2.0 = twice as much. Shrunk towards 1 when there are few days."""
    shock = market["ret"] <= SHOCK_DAY
    out = {}
    for code, s in panel.items():
        if s["type"] != "EQ" or len(s["c"]) < 120:
            continue
        lr = np.r_[np.nan, np.diff(np.log(s["c"]))]
        di = s["di"]
        m = shock[di] & np.isfinite(lr) & (s["v"] > 0)
        n = int(m.sum())
        msum = float(market["ret"][di[m]].sum()) if n else 0.0
        if n < min_days or msum >= 0:
            continue
        raw = float(lr[m].sum()) / msum
        w = n / (n + 10)
        out[code] = {"beta": _r(w * raw + (1 - w), 2), "days": n, "avg": _r(float(lr[m].mean()) * 100, 2)}
    return out


def _group_stats(codes: list[str], panel: dict, market: dict, uneasy: set[int]) -> dict:
    """20-session excess return vs the market for a group of shares, sampled every 5 sessions,
    overall and on days when the market regime was Nervous or Stress."""
    all_ex, bad_ex, n = [], [], 0
    for code in codes:
        s = panel.get(code)
        if not s or s["type"] != "EQ" or len(s["c"]) < 40:
            continue
        n += 1
        for t in range(20, len(s["c"]) - 20, 5):
            _, ex = _fwd(s, t, 20, market)
            if ex is None:
                continue
            all_ex.append(ex)
            if int(s["di"][t]) in uneasy:
                bad_ex.append(ex)

    def summ(v: list[float]) -> dict:
        if len(v) < 5:
            return {"samples": len(v)}
        a = np.array(v)
        return {"samples": len(v), "ex20": _r(float(np.median(a)) * 100, 2), "pos20": _r(float((a > 0).mean()) * 100, 0),
                "p10": _r(float(np.percentile(a, 10)) * 100, 1)}

    return {"companies": n, "all": summ(all_ex), "uneasy": summ(bad_ex)}


def political_links(code: str | None = None) -> list[dict]:
    sql = "SELECT * FROM political_links"
    rows = db.query(sql + (" WHERE code=%s" if code else "") + " ORDER BY code, id", (code,) if code else ())
    for r in rows:
        r["exposure_label"] = EXPOSURES.get(r["exposure"], r["exposure"])
    return rows


def add_link(data: dict) -> int:
    code = str(data.get("code") or "").strip().upper()
    exposure = str(data.get("exposure") or "")
    if exposure not in EXPOSURES:
        raise ValueError("Choose the kind of exposure")
    source = str(data.get("source") or "").strip()
    if not source:
        raise ValueError("Add a source (newspaper, link or filing) so the record can be checked later")
    since = data.get("since") or None
    if since:
        try:
            since = date.fromisoformat(str(since))
        except ValueError:
            raise ValueError("Invalid date")
    if not code or not db.scalar("SELECT COUNT(*) FROM instruments WHERE code=%s", (code,)):
        raise ValueError("Unknown trading code")
    return db.execute(
        "INSERT INTO political_links (code, person, role, exposure, since, source, note, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
        (code, (str(data.get("person") or "").strip() or None), (str(data.get("role") or "").strip()[:60] or None),
         exposure, since, source[:300], (str(data.get("note") or "").strip()[:500] or None)),
    )


def delete_link(link_id: int) -> None:
    db.execute("DELETE FROM political_links WHERE id=%s", (link_id,))


def _political(panel: dict, market: dict, regimes: list[dict], cal: list[date],
               news_events: dict[str, list[tuple[date, str]]]) -> dict:
    ci = {d: i for i, d in enumerate(cal)}
    uneasy = {ci[r["date"]] for r in regimes if r["regime"] in ("Nervous", "Stress")}
    govt = govt_holdings()
    shock = shock_betas(panel, market)
    groups: dict[str, dict] = {
        "all": {"label": "All shares (for comparison)", "hindsight": False,
                **_group_stats([c for c, s in panel.items() if s["type"] == "EQ"], panel, market, uneasy)},
        "state_owned": {"label": "Government holds shares (state-owned / state-backed)", "hindsight": False,
                        "codes": sorted(govt), **_group_stats(sorted(govt), panel, market, uneasy)},
    }
    betas = sorted((v["beta"] for v in shock.values()), reverse=True)
    if len(betas) >= 20:
        cut = betas[len(betas) // 5]
        hi = [c for c, v in shock.items() if v["beta"] >= cut]
        groups["shock_high"] = {"label": f"Fell hardest on sell-off days (top 20%, ≥ {cut:.2f}× market)", "hindsight": True,
                                "codes": sorted(hi), **_group_stats(hi, panel, market, uneasy)}
    by_exp: dict[str, set] = defaultdict(set)
    for r in political_links():
        by_exp[r["exposure"]].add(r["code"])
    for k, codes in by_exp.items():
        groups["link_" + k] = {"label": "Your records: " + EXPOSURES[k], "hindsight": True,
                               "codes": sorted(codes), **_group_stats(sorted(codes), panel, market, uneasy)}
    last = cal[-1] if cal else date.today()
    events = {c: [(d.isoformat(), t) for d, t in evs if t in POLITICAL_KINDS and (last - d).days <= max(365, active_days(t))]
              for c, evs in news_events.items()}
    return {"shock_day_pct": SHOCK_DAY * 100, "shock_days": int((market["ret"] <= SHOCK_DAY).sum()),
            "shock": shock, "govt_hold": govt, "groups": groups, "events": {c: v for c, v in events.items() if v}}


def _company_static() -> tuple[dict, dict]:
    rows = db.query("SELECT c.code, c.free_float, c.paid_up_capital, c.face_value FROM company_info c")
    ff = {r["code"]: r["free_float"] for r in rows}
    shares = {r["code"]: (r["paid_up_capital"] or 0) / (r["face_value"] or 10) for r in rows}
    return ff, shares


# ============================================================================ build everything

def build() -> dict:
    started = datetime.now()
    panel, cal, mret, market = _panel()
    news_events = load_news_events()
    ff, shares = _company_static()

    samples: dict[str, list] = defaultdict(list)
    for code, evs in news_events.items():
        s = panel.get(code)
        if not s:
            continue
        for d, t in evs:
            row = _measure(code, d, s, market)
            if row:
                samples[t].append(row)
    for code, s in panel.items():
        if s["type"] != "EQ":
            continue
        chg = s["chg"]
        for t in range(20, len(s["c"])):
            if s["v"][t] <= 0 or not np.isfinite(chg[t]):
                continue
            if chg[t] >= 9.5 or chg[t] <= -9.5:
                kind = "upper_circuit" if chg[t] >= 9.5 else "lower_circuit"
                row = _measure(code, s["d"][t], s, market)
                if row:
                    samples[kind].append(row)
    for r in db.query("SELECT DISTINCT code, record_date FROM news WHERE record_date IS NOT NULL"):
        s = panel.get(r["code"])
        if s:
            row = _measure(r["code"], r["record_date"], s, market, before=True)
            if row:
                samples["record_date"].append(row)

    labels = {**{k: v[0] for k, v in EVENT_TYPES.items()}, **PRICE_EVENTS}
    studies = {k: {"label": labels[k], **_stats(samples.get(k, []))} for k in labels}

    # market regime + forward market return per regime
    regimes = regime_series(cal, market)
    cum = market["cum"]
    ci = {d: i for i, d in enumerate(cal)}
    by_reg: dict[str, list] = defaultdict(list)
    for r in regimes:
        i = ci[r["date"]]
        if i + 20 < len(cal):
            by_reg[r["regime"]].append(math.exp(cum[i + 20] - cum[i]) - 1)
    regime_stats = {k: {"days": len(v), "fwd20": _r(float(np.mean(v)) * 100, 2), "pos20": _r(float((np.array(v) > 0).mean()) * 100, 0)}
                    for k, v in by_reg.items()}

    # political / regulatory events: elections from the holiday list + user events
    events = market_events()
    for ev in events:
        i = bisect.bisect_left(cal, ev["date"])
        ev["reaction"] = {}
        if 5 <= i < len(cal):
            ev["reaction"]["pre5"] = _r((math.exp(cum[i - 1] - cum[i - 6]) - 1) * 100, 2)
            for k in (1, 5, 20):
                if i - 1 + k < len(cal):
                    ev["reaction"][f"post{k}"] = _r((math.exp(cum[i - 1 + k] - cum[i - 1]) - 1) * 100, 2)

    # speculation risk: today's score per share, and history by bucket (20-session forward excess)
    sizes = {c: s["c"][-1] * shares[c] for c, s in panel.items() if shares.get(c)}
    ranked = sorted(sizes, key=lambda c: sizes[c])
    size_pct = {c: i / max(1, len(ranked) - 1) for i, c in enumerate(ranked)}
    q_dates = {c: [d for d, t in evs if t == "dse_query"] for c, evs in news_events.items()}
    bucket_rows: dict[str, list] = defaultdict(list)
    today_scores = {}
    for code, s in panel.items():
        if s["type"] != "EQ" or len(s["c"]) < 40:
            continue
        c, v, chg = s["c"], s["v"], s["chg"]
        qd = q_dates.get(code, [])
        for t in list(range(40, len(c) - 20, 5)) + [len(c) - 1]:
            r20 = c[t] / c[t - 20] - 1
            circ = float(np.sum(np.abs(np.nan_to_num(chg[t - 19: t + 1])) >= 9.5))
            avg_v = v[t - 20: t].mean()
            vr = v[t] / avg_v if avg_v > 0 else None
            k = bisect.bisect_right(qd, s["d"][t])
            recent_q = k > 0 and (s["d"][t] - qd[k - 1]).days <= 14
            score, why = spec_score(size_pct.get(code), ff.get(code), s["cat"], r20, circ, vr, recent_q)
            if t == len(c) - 1:
                today_scores[code] = {"score": score, "why": why, "r20": _r(r20 * 100, 1)}
                continue
            _, ex = _fwd(s, t, 20, market)
            if ex is not None:
                name = next(b[2] for b in SPEC_BUCKETS if b[0] <= score < b[1])
                bucket_rows[name].append(ex)
    spec_stats = {name: {"samples": len(v), "ex20": _r(float(np.median(v)) * 100, 2), "mean20": _r(float(np.mean(v)) * 100, 2),
                         "pos20": _r(float((np.array(v) > 0).mean()) * 100, 0),
                         "p10": _r(float(np.percentile(v, 10)) * 100, 1), "p90": _r(float(np.percentile(v, 90)) * 100, 1)}
                  for name, v in bucket_rows.items()}

    out = {
        "built_at": started.isoformat(timespec="seconds"),
        "history_from": cal[0].isoformat() if cal else None, "history_to": cal[-1].isoformat() if cal else None,
        "studies": studies,
        "regime_now": {k: (v if not isinstance(v, float) else _r(v, 4)) for k, v in (regimes[-1] if regimes else {}).items()},
        "regime_stats": regime_stats,
        "regime_history": [{"date": r["date"], "index": _r(r["index"], 4), "regime": r["regime"]} for r in regimes],
        "events": events,
        "spec_stats": spec_stats,
        "spec_today": today_scores,
        # political events stay active longer; consumers filter again with active_days()
        "recent_events": {c: [(d.isoformat(), t) for d, t in evs if (cal[-1] - d).days <= max(45, active_days(t, 45))]
                          for c, evs in news_events.items() if evs and cal},
        "political": _political(panel, market, regimes, cal, news_events),
    }
    db.set_state("bd_factors", out)
    log.info("Bangladesh factors built in %ss", round((datetime.now() - started).total_seconds(), 1))
    return out


# ============================================================================ political / regulatory event calendar

def market_events() -> list[dict]:
    """Elections from DSE's holiday list (published by DSE) + events the user recorded."""
    out = []
    hol = db.get_state("holidays") or {}
    seen = set()
    for year, items in hol.items():
        for h in items:
            name = str(h.get("name") or "")
            if re.search(r"election|referendum", name, re.I):
                d = date.fromisoformat(h["date"])
                if (name, d.year) in seen:
                    continue
                seen.add((name, d.year))
                out.append({"id": None, "date": d, "kind": "political", "title": name, "source": "DSE holiday list",
                            "impact": None, "note": "Market closed for the national election"})
    for r in db.query("SELECT * FROM market_events ORDER BY event_date"):
        out.append({"id": r["id"], "date": r["event_date"], "kind": r["kind"], "title": r["title"], "source": "you",
                    "impact": r["impact"], "note": r["note"]})
    return sorted(out, key=lambda e: e["date"])


def add_event(data: dict) -> int:
    try:
        d = date.fromisoformat(str(data.get("date")))
    except ValueError:
        raise ValueError("Invalid date")
    kind = str(data.get("kind") or "political")
    if kind not in ("political", "regulatory", "economic", "other"):
        raise ValueError("Kind must be political, regulatory, economic or other")
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("Title is required")
    impact = data.get("impact")
    impact = None if impact in (None, "") else max(-2, min(2, int(impact)))
    return db.execute(
        "INSERT INTO market_events (event_date, kind, title, impact, note, created_at) VALUES (%s,%s,%s,%s,%s,NOW())",
        (d, kind, title[:200], impact, (data.get("note") or "")[:500]),
    )


def delete_event(event_id: int) -> None:
    db.execute("DELETE FROM market_events WHERE id=%s", (event_id,))


# ============================================================================ per-stock view + model inputs

def stock_view(code: str, state: dict | None = None) -> dict:
    st = state or db.get_state("bd_factors") or {}
    studies = st.get("studies", {})
    last = date.fromisoformat(st["history_to"]) if st.get("history_to") else date.today()

    def ev(d: str, t: str) -> dict:
        s = studies.get(t, {})
        return {"date": d, "type": t, "label": s.get("label", t), "ex20": s.get("ex20"), "ex5": s.get("ex5"),
                "n": s.get("n"), "evidence": s.get("evidence"), "direction": s.get("direction")}

    recent = [ev(d, t) for d, t in (st.get("recent_events", {}).get(code) or [])
              if (last - date.fromisoformat(d)).days <= active_days(t, 45)]
    pol = st.get("political") or {}
    shock = (pol.get("shock") or {}).get(code)
    if shock:
        betas = sorted(v["beta"] for v in pol["shock"].values())
        shock = {**shock, "pct_rank": _r(bisect.bisect_left(betas, shock["beta"]) / max(1, len(betas) - 1) * 100, 0)}
    links = political_links(code)
    groups = pol.get("groups") or {}
    mine = [{"key": k, **{x: g.get(x) for x in ("label", "companies", "all", "uneasy", "hindsight")}}
            for k, g in groups.items()
            if code in (g.get("codes") or []) and (k.startswith("link_") or k == "state_owned")]
    return {"recent_events": recent[::-1], "speculation": (st.get("spec_today") or {}).get(code),
            "spec_stats": st.get("spec_stats"), "regime": st.get("regime_now"), "regime_stats": st.get("regime_stats"),
            "political": {"govt_hold": (pol.get("govt_hold") or {}).get(code), "shock": shock,
                          "shock_day_pct": pol.get("shock_day_pct"), "links": links, "groups": mine,
                          "baseline": groups.get("all"),
                          "events": [ev(d, t) for d, t in (pol.get("events") or {}).get(code, [])][::-1]}}


def event_features(dates: list[date], events: list[tuple[date, str]]) -> dict[str, list[float]]:
    """Point-in-time Bangladesh inputs per date (only events dated on/before it)."""
    n = len(dates)
    out = {k: [0.0] * n for k in ("bd_query", "bd_rumour", "bd_sponsor", "bd_toz", "bd_margin", "bd_govt",
                                  "bd_state", "bd_polhit", "bd_court")}
    if not events:
        return out
    ev_d = [e[0] for e in events]
    window = max(ACTIVE_DAYS.values())
    for i, t in enumerate(dates):
        hi = bisect.bisect_right(ev_d, t)
        if hi == 0:
            continue
        lo = bisect.bisect_right(ev_d, t - timedelta(days=window))
        net, govt = 0, 0
        for d, kind in events[lo:hi]:
            age = (t - d).days
            if kind == "dse_query":
                out["bd_query"][i] = max(out["bd_query"][i], math.exp(-age / 10))
            elif kind == "rumour":
                out["bd_rumour"][i] = max(out["bd_rumour"][i], math.exp(-age / 10))
            elif kind in ("sponsor_buy", "sponsor_sell") and age <= 30:
                net += 1 if kind == "sponsor_buy" else -1
            elif kind == "to_z" and age <= 60:
                out["bd_toz"][i] = 1.0
            elif kind == "margin_ban" and age <= 90:
                out["bd_margin"][i] = 1.0
            elif kind in ("govt", "regulator") and age <= 30:
                govt += 1
            elif kind in ("state_control", "wipeout") and age <= ACTIVE_DAYS[kind]:
                out["bd_state"][i] = 1.0
            elif kind == "political_hit" and age <= ACTIVE_DAYS[kind]:
                out["bd_polhit"][i] = 1.0
            elif kind == "court_hold" and age <= ACTIVE_DAYS[kind]:
                out["bd_court"][i] = 1.0
        out["bd_sponsor"][i] = float(max(-3, min(3, net)))
        out["bd_govt"][i] = float(min(3, govt))
    return out
