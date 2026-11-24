"""Combined forecast: the base range model + the measured Bangladesh factors.

For a share at session t and a horizon of h sessions (all moves as log returns):

    shift_h  = model_h + s_h * sum_k part_k(h)
    centre_h = q50 * sigma_h + shift_h          (every band edge moves by the same shift)

    part_k(h)  what factor k adds over the next h sessions, from its MEASURED effect:
               company event   E(t + h - b) - E(t - b), b = the event's session, E = cumulative median
                               effect vs the market 1, 5 and 20 sessions after past events of that kind
                               (linear in between, flat after 20). Only the strongest active event counts,
                               because events overlap (a Z downgrade usually brings a margin ban).
               record date     the same curve, measured from the session before past record dates
               market regime   beta x (market's 20-session move after days in this regime - average)
               pump risk       the risk bucket's 20-session result minus all shares' result
               state ownership state-owned shares' 20-session result minus all shares' result
               (regime, pump risk and state ownership scaled by min(h, 20) / 20)
    weight     every measured effect is multiplied by its evidence weight
               w = n / (n + 20) x clip((|t| - 1) / 1.5, 0, 1)
               = 0 when the t-statistic is below 1 or the median and trimmed mean disagree in sign,
               full when |t| >= 2.5 and there are many cases
    model_h    the statistical model's expected move minus the base median, only while it passes its
               reliability test (otherwise 0)
    s_h        overall strength 0, 0.5 or 1, chosen daily on 3 unseen 15-session windows: the one with the
               lowest interval score of the shifted 68% range on the share-days the factors moved

Every effect is measured only from cases whose outcome was known before the forecast date, so the
test windows never see their own future.
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np

from . import bdfactors, db, settings

KNOTS = (1, 5, 20)
CIRCUIT = 0.095        # a +/- 9.5% session counts as a circuit-limit day
SCALES = (0.0, 0.5, 1.0)
MIN_AFFECTED = 100     # share-days the factors must move before their strength is trusted
MAX_SHIFT = 0.30
QUERY_RECENT = math.exp(-14 / 10)   # bd_query decays exp(-days/10): >= this = within 14 days

LABELS = {
    "model": "Statistical model (68 inputs)",
    "event": "Company event (Bangladesh factors)",
    "record": "Record date (price drops by about the dividend)",
    "regime": "Market regime (political / macro mood)",
    "spec": "Pump-and-dump risk",
    "state": "State ownership",
    "politics": "Political link (your records)",
}
BUCKETS = bdfactors.SPEC_BUCKETS


def evidence(t: float | None) -> str:
    a = abs(t or 0)
    return "strong" if a >= 2.5 else "moderate" if a >= 1.5 else "weak"


def weight(n: int, t: float | None, agree: bool) -> float:
    if not agree or n < 5 or t is None:
        return 0.0
    return n / (n + 20) * min(1.0, max(0.0, (abs(t) - 1.0) / 1.5))


def summarize(x: np.ndarray) -> dict:
    """Median (headline), winsorised t-statistic and evidence weight of a set of measured effects."""
    n = int(len(x))
    if n < 5:
        return {"n": n, "med": None, "t": None, "w": 0.0, "eff": 0.0}
    med = float(np.median(x))
    lo, hi = np.percentile(x, [5, 95])
    xw = np.clip(x, lo, hi)
    m, sd = float(xw.mean()), float(xw.std(ddof=1))
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
    w = weight(n, t, med * m > 0)
    return {"n": n, "med": med, "t": t, "w": w, "eff": med * w}


def curve(e: dict[int, float], x: float) -> float:
    """Cumulative effect x sessions after the event's base session (0 before, flat after 20)."""
    if x <= 0:
        return 0.0
    pts = [(0, 0.0)] + [(k, e.get(k) or 0.0) for k in KNOTS]
    if x >= KNOTS[-1]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def _label(kind: str) -> str:
    if kind in bdfactors.EVENT_TYPES:
        return bdfactors.EVENT_TYPES[kind][0]
    return bdfactors.PRICE_EVENTS.get(kind, kind)


class Context:
    """Everything needed to measure the factors at any cut-off date and apply them to a share."""

    def __init__(self, ds: dict):
        self.cal: list[date] = ds["calendar"]
        self.cum = np.cumsum(ds["mret"])
        self.regime: list[str] = ds["regime"]
        self.stocks: dict[str, dict] = ds["stocks"]
        self.ff, _ = bdfactors._company_static()
        # political links you recorded: their meaning changes with who governs (see bdfactors)
        self.timeline = bdfactors.timeline()
        self.links: dict[str, list] = defaultdict(list)
        for lk in bdfactors.political_links():
            self.links[lk["code"]].append(lk)
        self.use_politics = settings.get_all().get("use_political_records") == "1"
        records: dict[str, list] = defaultdict(list)
        for r in db.query("SELECT code, record_date, MIN(news_date) AS ann FROM news WHERE record_date IS NOT NULL "
                          "GROUP BY code, record_date"):
            records[r["code"]].append((r["ann"], r["record_date"]))
        # per share: events [(base session, announced date, kind)] sorted by base; record dates separately
        self.events: dict[str, list[tuple[int, date, str]]] = {}
        self.event_b: dict[str, list[int]] = {}
        self._spec: dict[tuple[str, int], int] = {}
        self.records: dict[str, list[tuple[date, date, int | None]]] = {}
        samples: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
        # daily size ranks for the pump-and-dump score
        by_day: dict[int, list[float]] = defaultdict(list)
        for st in self.stocks.values():
            for t, d in enumerate(st["di"]):
                by_day[int(d)].append(float(st["f"]["size"][t]))
        self.day_sizes = {k: np.sort(np.array(v)) for k, v in by_day.items()}
        ss_known, ss_ex, ss_bucket, ss_state, ss_align = [], [], [], [], []
        for code, st in self.stocks.items():
            dates, v, di = st["dates"], st["v"], st["di"]
            lc = np.log(st["c"])
            n = len(dates)
            evs = []
            for d, kind in ds["bd_events"].get(code, []):
                b = bisect.bisect_right(dates, d) - 1
                if b >= 0:
                    evs.append((b, d, kind))
            r1 = np.nan_to_num(st["f"]["r1"])
            for t in np.nonzero(v > 0)[0]:
                if abs(r1[t]) >= math.log1p(CIRCUIT) - 1e-9 and t > 0:
                    evs.append((int(t), dates[t], "upper_circuit" if r1[t] > 0 else "lower_circuit"))
            evs.sort(key=lambda e: e[0])
            self.events[code] = evs
            self.event_b[code] = [e[0] for e in evs]
            recs = []
            for ann, rec in sorted(records.get(code, []), key=lambda x: x[1]):
                b = bisect.bisect_left(dates, rec) - 1 if rec <= dates[-1] else None
                recs.append((ann, rec, b))
                if b is not None:
                    evs_b = (b, ann, "record_date")
                    self._sample(samples, evs_b, lc, v, di, n)
            self.records[code] = recs
            for e in evs:
                self._sample(samples, e, lc, v, di, n)
            # every 5th session: 20-session result vs market, with the pump-risk bucket and state flag then
            for t in range(40, n - 20, 5):
                if v[t] <= 0 or v[t + 20] <= 0:
                    continue
                ss_known.append(int(di[t + 20]))
                ss_ex.append((lc[t + 20] - lc[t]) - (self.cum[di[t + 20]] - self.cum[di[t]]))
                ss_bucket.append(self.bucket(code, t))
                ss_state.append(bool(st["f"]["govt_hold"][t] > 0))
                ss_align.append(bdfactors.company_alignment(self.links[code], dates[t], self.timeline) or ""
                                if code in self.links else "")
        self.samples = {k: {h: (np.array([a for a, _ in xs], dtype=int), np.array([b for _, b in xs]))
                            for h, xs in hs.items()} for k, hs in samples.items()}
        self.ss_known, self.ss_ex = np.array(ss_known, dtype=int), np.array(ss_ex)
        self.ss_bucket, self.ss_state = np.array(ss_bucket, dtype=object), np.array(ss_state, dtype=bool)
        self.ss_align = np.array(ss_align, dtype=object)
        reg_known, reg_fwd, reg_lab = [], [], []
        for i, lab in enumerate(self.regime):
            if lab and i + 20 < len(self.cal):
                reg_known.append(i + 20)
                reg_fwd.append(self.cum[i + 20] - self.cum[i])
                reg_lab.append(lab)
        self.reg_known, self.reg_fwd, self.reg_lab = np.array(reg_known, dtype=int), np.array(reg_fwd), np.array(reg_lab, dtype=object)
        self._tables: dict[int, dict] = {}

    def _sample(self, samples, e, lc, v, di, n) -> None:
        b, _, kind = e
        if b < 20 or v[b] <= 0:
            return
        for h in KNOTS:
            if b + h < n and v[b + h] > 0:
                samples[kind][h].append((int(di[b + h]), (lc[b + h] - lc[b]) - (self.cum[di[b + h]] - self.cum[di[b]])))

    # ------------------------------------------------------------------ pump-and-dump bucket
    def spec(self, code: str, t: int) -> int:
        key = (code, t)
        if key not in self._spec:
            self._spec[key] = self._spec_score(code, t)
        return self._spec[key]

    def _spec_score(self, code: str, t: int) -> int:
        st = self.stocks[code]
        f = st["f"]
        sizes = self.day_sizes.get(int(st["di"][t]))
        pct = bisect.bisect_left(sizes, f["size"][t]) / max(1, len(sizes) - 1) if sizes is not None and len(sizes) else None
        r20, vr, circ = float(f["r20"][t]), float(f["vratio"][t]), float(f["circ20"][t])
        score, _ = bdfactors.spec_score(pct, self.ff.get(code), st["category"],
                                        math.expm1(r20) if math.isfinite(r20) else None, circ if math.isfinite(circ) else 0.0,
                                        math.exp(vr) if math.isfinite(vr) else None, bool(f["bd_query"][t] >= QUERY_RECENT))
        return score

    def bucket(self, code: str, t: int) -> str:
        s = self.spec(code, t)
        return next(b[2] for b in BUCKETS if b[0] <= s < b[1])

    # ------------------------------------------------------------------ effects known before a cut-off
    def table(self, cutoff: int) -> dict:
        """Measured effects using only outcomes known before calendar index `cutoff`."""
        if cutoff in self._tables:
            return self._tables[cutoff]
        ev = {}
        for kind, hs in self.samples.items():
            ev[kind] = {h: summarize(vals[known < cutoff]) for h, (known, vals) in hs.items()}
        k = self.reg_known < cutoff
        regime = {}
        if k.sum() >= 40:
            allf = self.reg_fwd[k]
            base, sd = float(allf.mean()), float(allf.std()) or 1e-9
            for lab in ("Calm", "Nervous", "Stress", "Euphoric"):
                x = self.reg_fwd[k & (self.reg_lab == lab)]
                if not len(x):
                    continue
                n_eff = len(x) / 20  # overlapping 20-session windows: about one independent case per 20 days
                diff = float(x.mean()) - base
                t = diff / (sd / math.sqrt(max(n_eff, 1.0)))
                w = n_eff / (n_eff + 5) * min(1.0, max(0.0, (abs(t) - 1.0) / 1.5))
                regime[lab] = {"n": int(len(x)), "med": diff, "t": t, "w": w, "eff": diff * w}
        k = self.ss_known < cutoff
        spec, state, politics = {}, {"n": 0, "med": None, "t": None, "w": 0.0, "eff": 0.0}, {}
        if k.sum() >= 100:
            x_all = self.ss_ex[k]
            med_all = float(np.median(x_all))
            for b in BUCKETS:
                spec[b[2]] = summarize(x_all[self.ss_bucket[k] == b[2]] - med_all)
            state = summarize(x_all[self.ss_state[k]] - med_all)
            for cat in bdfactors.ALIGNMENTS:
                politics[cat] = summarize(x_all[self.ss_align[k] == cat] - med_all)
        out = {"events": ev, "regime": regime, "spec": spec, "state": state, "politics": politics}
        self._tables[cutoff] = out
        return out

    # ------------------------------------------------------------------ apply to one share
    def parts(self, tab: dict, code: str, t: int, h: int, future: list[date] | None = None) -> list[dict]:
        """What each Bangladesh factor adds to this share's move over the next h sessions."""
        st = self.stocks[code]
        f = st["f"]
        day = st["dates"][t]
        scale = min(h, 20) / 20
        out: list[dict] = []

        # company events: the latest of each kind still inside its measured 20 sessions
        evs = self.events.get(code, [])
        lo = bisect.bisect_left(self.event_b.get(code, []), t - 19)
        latest: dict[str, tuple[int, date]] = {}
        for b, d, kind in evs[lo:]:
            if b > t:
                break
            if d > day:  # dated after this session (e.g. a weekend notice): not known yet
                continue
            latest[kind] = (b, d)
        cand = []
        for kind, (b, d) in latest.items():
            m = tab["events"].get(kind, {})
            if not m:
                continue
            eff = {k: (m.get(k) or {}).get("eff", 0.0) for k in KNOTS}
            raw = {k: (m.get(k) or {}).get("med") or 0.0 for k in KNOTS}
            m20 = m.get(20) or {}
            cand.append({"key": "event", "kind": kind, "label": LABELS["event"],
                         "state": f"{_label(kind)} on {d:%d %b %Y} ({t - b} sessions ago)",
                         "value": curve(eff, t + h - b) - curve(eff, t - b), "raw": curve(raw, t + h - b) - curve(raw, t - b),
                         "n": m20.get("n"), "t": m20.get("t"), "w": m20.get("w"), "med20": m20.get("med")})
        cand.sort(key=lambda p: -abs(p["value"]))
        for i, p in enumerate(cand):
            p["used"] = i == 0 and abs(p["value"]) > 0
            if i > 0:
                p["note"] = "overlaps with the stronger event, not added"
            out.append(p)

        # record date: the nearest one announced by now, from 20 sessions ago to ahead
        m = tab["events"].get("record_date", {})
        best = None
        for ann, rec, b in self.records.get(code, []):
            if ann > day:
                continue
            if b is None:  # record date after the last session: only for today's live forecast
                if future is None or t != len(st["dates"]) - 1:
                    continue
                r = bisect.bisect_left(future, rec)  # sessions after today until the record date (0-based)
                if r >= len(future):
                    continue
                b = t + r      # base = the session before the record date
            if t - b >= 20 or b - t > 60:
                continue
            if best is None or abs(b - t) < abs(best[0] - t):
                best = (b, rec)
        if best and m:
            b, rec = best
            eff = {k: (m.get(k) or {}).get("eff", 0.0) for k in KNOTS}
            raw = {k: (m.get(k) or {}).get("med") or 0.0 for k in KNOTS}
            m20 = m.get(20) or {}
            when = f"in {b - t + 1} sessions" if b >= t else f"{t - b} sessions ago"
            val = curve(eff, t + h - b) - curve(eff, t - b)
            out.append({"key": "record", "label": LABELS["record"], "state": f"Record date {rec:%d %b %Y} ({when})",
                        "value": val, "raw": curve(raw, t + h - b) - curve(raw, t - b), "used": abs(val) > 0,
                        "n": m20.get("n"), "t": m20.get("t"), "w": m20.get("w"), "med20": m20.get("med"),
                        "note": None if b - t < h else "after this horizon"})

        # market regime
        lab = self.regime[int(st["di"][t])]
        rg = tab["regime"].get(lab)
        if rg:
            b60 = float(f["beta60"][t])
            beta = min(2.0, max(0.0, b60 if math.isfinite(b60) else 1.0))
            val = beta * rg["eff"] * scale
            out.append({"key": "regime", "label": LABELS["regime"], "state": f"{lab} market; this share's beta {beta:.2f}",
                        "value": val, "raw": beta * rg["med"] * scale, "used": abs(val) > 0,
                        "n": rg["n"], "t": rg["t"], "w": rg["w"], "med20": rg["med"]})

        # pump-and-dump risk bucket
        score = self.spec(code, t)
        bname = next(b[2] for b in BUCKETS if b[0] <= score < b[1])
        sp = tab["spec"].get(bname)
        if sp and sp.get("med") is not None:
            val = sp["eff"] * scale
            out.append({"key": "spec", "label": LABELS["spec"], "state": f"Risk {score}/100 ({bname})",
                        "value": val, "raw": sp["med"] * scale, "used": abs(val) > 0,
                        "n": sp["n"], "t": sp["t"], "w": sp["w"], "med20": sp["med"]})

        # state ownership
        g = float(f["govt_hold"][t])
        sg = tab["state"]
        if g > 0 and sg.get("med") is not None:
            val = sg["eff"] * scale
            out.append({"key": "state", "label": LABELS["state"], "state": f"Government holds {g:.1f}%",
                        "value": val, "raw": sg["med"] * scale, "used": abs(val) > 0,
                        "n": sg["n"], "t": sg["t"], "w": sg["w"], "med20": sg["med"]})
        # political link you recorded: what that position measured (hindsight: you chose the companies),
        # added only when switched on in Settings, and never part of the strength test
        if code in self.links:
            cat = bdfactors.company_alignment(self.links[code], day, self.timeline)
            pp = (tab.get("politics") or {}).get(cat or "")
            if cat and pp and pp.get("med") is not None:
                val = pp["eff"] * scale
                out.append({"key": "politics", "label": LABELS["politics"],
                            "state": f"{bdfactors.ALIGNMENTS[cat]} ({'; '.join(bdfactors.describe_link(lk) for lk in self.links[code])})",
                            "value": val, "raw": pp["med"] * scale, "used": self.use_politics and abs(val) > 0,
                            "n": pp["n"], "t": pp["t"], "w": pp["w"], "med20": pp["med"],
                            "note": None if self.use_politics else "not added: switch on in Settings (hindsight)"})
        for p in out:
            p["evidence"] = evidence(p.get("t"))
        return out

    def total(self, tab: dict, code: str, t: int, h: int, future: list[date] | None = None) -> float:
        """Bangladesh factors that go through the strength test (not your political records)."""
        return float(sum(p["value"] for p in self.parts(tab, code, t, h, future) if p.get("used") and p["key"] != "politics"))


def round_parts(parts: list[dict]) -> list[dict]:
    """Compact, JSON-friendly copy (percent values) for storage and the page."""
    out = []
    for p in parts:
        q: dict[str, Any] = {k: p.get(k) for k in ("key", "kind", "label", "state", "used", "note", "n", "evidence")}
        q["pct"] = round(math.expm1(p["value"]) * 100, 2)
        q["raw_pct"] = round(math.expm1(p.get("raw") or 0.0) * 100, 2)
        q["med20_pct"] = round(math.expm1(p["med20"]) * 100, 2) if p.get("med20") is not None else None
        q["t"] = round(p["t"], 2) if p.get("t") is not None else None
        q["w"] = round(p["w"], 2) if p.get("w") is not None else None
        out.append(q)
    return out
