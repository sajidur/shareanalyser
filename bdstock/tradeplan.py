"""Trade plans: concrete buy/sell parameters for every share, each backed by measured history.

For each share the plan gives
  * signal          BUY / ACCUMULATE / HOLD / TAKE PROFIT / SELL / AVOID / WAIT, with the reasons
  * buy zone        limit prices with a measured chance of being filled next session
  * stop-loss       price - 1.5 x ATR(14)   (holders: trailing stop = highest close since buying - 2.5 x ATR)
  * targets         T1 = top of the 1-week 50% range, T2 = top of the 1-month 68% range
  * reward : risk   (target - price) / (price - stop)
  * win rate / EV   how often, in the past year of DSE prices, a move of that size up was reached
                    BEFORE a move of that size down, and the average result per trade
  * position size   shares so that hitting the stop loses at most your risk % of capital

The win-rate table is built from daily highs and lows of all liquid shares, with distances measured
in each share's own daily volatility (sigma), so one table fits calm and volatile shares alike.
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from . import analysis, db, settings

log = logging.getLogger(__name__)

LEVELS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0]   # distances in daily-sigma units
WINDOWS = (5, 20)
NEXT_PCTS = [10, 20, 30, 50, 70, 80, 90]
TICK = 0.10   # DSE price step (Tk)
SIGNAL_ORDER = ["BUY", "ACCUMULATE", "TAKE PROFIT", "SELL", "REDUCE", "HOLD", "AVOID", "WAIT"]


def floor_tick(x: float) -> float:
    return round(math.floor(x / TICK + 1e-9) * TICK, 2)


def ceil_tick(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-9) * TICK, 2)


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


def _first_hit(mask: np.ndarray, n: int) -> np.ndarray:
    """Index of the first True along axis 1 (n if never)."""
    return np.where(mask.any(axis=1), mask.argmax(axis=1), n)


# ============================================================================ tables from history

def build_tables(ds: dict) -> dict:
    from . import predict as P

    up_parts: dict[int, list] = defaultdict(list)
    dn_parts: dict[int, list] = defaultdict(list)
    end_parts: dict[int, list] = defaultdict(list)
    low1, high1 = [], []
    buy_out, base_out, buy_days = [], [], []
    sell_fwd, all_fwd = defaultdict(list), defaultdict(list)
    for st in ds["stocks"].values():
        if st.get("asset_type") not in ("EQ", "MF", None):
            continue
        c, v, h, lo, f = st["c"], st["v"], st["h"], st["lo"], st["f"]
        n = len(c)
        sig, liq = f["_sigma"], f["_liq_mn"]
        ok = (v > 0) & (liq >= 1.0) & np.isfinite(sig) & (sig > 0)
        # next-session low/high in sigma units
        t = np.arange(P.WARMUP, n - 1)
        t = t[ok[t]]
        if len(t):
            low1.append(np.log(lo[t + 1] / c[t]) / sig[t])
            high1.append(np.log(h[t + 1] / c[t]) / sig[t])
        for N in WINDOWS:
            if n <= P.WARMUP + N:
                continue
            t = np.arange(P.WARMUP, n - N)
            t = t[ok[t]]
            if not len(t):
                continue
            Hw = np.lib.stride_tricks.sliding_window_view(h[1:], N)[t]
            Lw = np.lib.stride_tricks.sliding_window_view(lo[1:], N)[t]
            s = sig[t][:, None]
            up_parts[N].append(np.maximum.accumulate(np.log(Hw / c[t][:, None]) / s, axis=1))
            dn_parts[N].append(np.maximum.accumulate(np.log(c[t][:, None] / Lw) / s, axis=1))
            end_parts[N].append(np.log(c[t + N] / c[t]) / sig[t])
        # ---- rule track record (shares only, not Z): BUY setup traded with its stop/target over 20 sessions
        if st.get("asset_type") != "EQ" or st.get("category") == "Z" or n <= P.WARMUP + 20:
            continue
        s20, s50, rsi, atr = f["_sma20"], f["_sma50"], f["rsi"], f["_atr"]
        t = np.arange(P.WARMUP, n - 20)
        good = ok[t] & np.isfinite(atr[t]) & (liq[t] >= P.LIQUID_MN)
        t = t[good]
        if not len(t):
            continue
        stop = c[t] - 1.5 * atr[t]
        target = c[t] * np.exp(sig[t] * math.sqrt(20))
        valid = stop > 0
        t, stop, target = t[valid], stop[valid], target[valid]
        rr = (target - c[t]) / (c[t] - stop)
        buy = ((c[t] > s50[t]) & (s20[t] > s50[t]) & (rsi[t] >= 40) & (rsi[t] <= 65)
               & (c[t] <= s20[t] * 1.05) & (rr >= 1.5))
        Hw = np.lib.stride_tricks.sliding_window_view(h[1:], 20)[t]
        Lw = np.lib.stride_tricks.sliding_window_view(lo[1:], 20)[t]
        ht = _first_hit(Hw >= target[:, None], 20)
        lt = _first_hit(Lw <= stop[:, None], 20)
        res = np.where(ht < lt, np.log(target / c[t]), np.where(lt < 20, np.log(stop / c[t]), np.log(c[t + 20] / c[t])))
        days = np.minimum(np.minimum(ht, lt) + 1, 20)
        outcome = np.where(ht < lt, 1, np.where(lt < 20, -1, 0))
        buy_out.append(np.column_stack([res[buy], outcome[buy]]))
        buy_days.append(days[buy])
        base_out.append(res)
        sell = (c[t] < s50[t]) & (s20[t] < s50[t])
        for N in (5, 20):
            fwd = c[t + N] / c[t] - 1 if N == 20 else c[np.minimum(t + N, n - 1)] / c[t] - 1
            sell_fwd[N].append(fwd[sell])
            all_fwd[N].append(fwd)

    tables: dict[str, Any] = {"levels": LEVELS, "windows": {}}
    for N in WINDOWS:
        if not up_parts[N]:
            continue
        UP, DN, END = np.vstack(up_parts[N]), np.vstack(dn_parts[N]), np.concatenate(end_parts[N])
        tu = np.array([_first_hit(UP >= L, N) for L in LEVELS])
        td = np.array([_first_hit(DN >= L, N) for L in LEVELS])
        K = len(LEVELS)
        p_t, p_s, ev = np.zeros((K, K)), np.zeros((K, K)), np.zeros((K, K))
        for i, Lu in enumerate(LEVELS):
            for j, Ld in enumerate(LEVELS):
                tf = tu[i] < td[j]
                sf = (td[j] <= tu[i]) & (td[j] < N)   # same-day ties count as a stop (conservative)
                p_t[i, j], p_s[i, j] = tf.mean(), sf.mean()
                ev[i, j] = np.where(tf, Lu, np.where(sf, -Ld, END)).mean()
        tables["windows"][str(N)] = {"samples": int(len(END)), "p_target": p_t.round(4).tolist(),
                                     "p_stop": p_s.round(4).tolist(), "ev_sigma": ev.round(4).tolist()}
    if low1:
        lw, hg = np.concatenate(low1), np.concatenate(high1)
        tables["next_low"] = dict(zip(map(str, NEXT_PCTS), np.percentile(lw, NEXT_PCTS).round(4).tolist()))
        tables["next_high"] = dict(zip(map(str, NEXT_PCTS), np.percentile(hg, NEXT_PCTS).round(4).tolist()))
    if buy_out:
        B, base = np.vstack(buy_out), np.concatenate(base_out)
        tables["buy_record"] = {
            "trades": int(len(B)),
            "win_rate": _r(float((B[:, 1] == 1).mean()) * 100, 1),
            "stop_rate": _r(float((B[:, 1] == -1).mean()) * 100, 1),
            "timeout_rate": _r(float((B[:, 1] == 0).mean()) * 100, 1),
            "avg_trade": _r(float(np.expm1(B[:, 0]).mean()) * 100, 2),
            "baseline_trade": _r(float(np.expm1(base).mean()) * 100, 2),
            "avg_days": _r(float(np.concatenate(buy_days).mean()), 1),
        }
    if sell_fwd[5]:
        tables["sell_record"] = {
            "signals": int(sum(len(x) for x in sell_fwd[20])),
            **{f"fwd{N}": _r(float(np.concatenate(sell_fwd[N]).mean()) * 100, 2) for N in (5, 20)},
            **{f"all{N}": _r(float(np.concatenate(all_fwd[N]).mean()) * 100, 2) for N in (5, 20)},
        }
    tables["built_at"] = datetime.now().isoformat(timespec="seconds")
    db.set_state("trade_tables", tables)
    log.info("Trade tables built: %s", {k: v.get("samples") for k, v in tables["windows"].items()})
    return tables


def lookup(tables: dict, N: int, up_sigma: float, down_sigma: float) -> dict | None:
    """Bilinear interpolation of the win-rate / EV table at the given distances (sigma units)."""
    w = (tables or {}).get("windows", {}).get(str(N))
    if not w or not (up_sigma > 0 and down_sigma > 0):
        return None
    L = np.array(tables["levels"])
    u = float(np.clip(up_sigma, L[0], L[-1]))
    d = float(np.clip(down_sigma, L[0], L[-1]))

    def interp(grid: list) -> float:
        g = np.array(grid)
        rows = np.array([np.interp(d, L, g[i]) for i in range(len(L))])
        return float(np.interp(u, L, rows))

    return {"win_rate": interp(w["p_target"]), "stop_rate": interp(w["p_stop"]), "ev_sigma": interp(w["ev_sigma"]),
            "extrapolated": up_sigma > L[-1] or down_sigma > L[-1] or up_sigma < L[0] or down_sigma < L[0]}


# ============================================================================ plans

def _load_context(codes: list[str] | None = None) -> dict:
    from . import portfolio, predict

    d = predict.latest_date()
    where, params = "", []
    if codes:
        where = f" AND code IN ({','.join(['%s'] * len(codes))})"
        params = list(codes)
    bars: dict[str, list] = defaultdict(list)
    for r in db.query(
        "SELECT code, trade_date, high, low, close, volume FROM daily_prices "
        f"WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_prices) - INTERVAL 130 DAY{where} ORDER BY code, trade_date",
        params,
    ):
        bars[r["code"]].append(r)
    preds: dict[str, dict] = defaultdict(dict)
    if d:
        for r in db.query(f"SELECT * FROM predictions WHERE trade_date=%s{where}", [d, *params]):
            preds[r["code"]][r["horizon"]] = r
    sd = analysis.latest_signal_date()
    sigs = {s["code"]: s for s in db.query(f"SELECT * FROM signals WHERE trade_date=%s{where}", [sd, *params])} if sd else {}
    inst = {r["code"]: r for r in db.query("SELECT code, name, sector, category, asset_type, trailing_pe FROM instruments")}
    info = {r["code"]: r for r in db.query("SELECT * FROM company_info")}
    bad_news: dict[str, list] = defaultdict(list)
    for r in db.query(
        "SELECT code, news_date, summary, ai_impact, ai_reason, tags FROM news WHERE news_date >= CURDATE() - INTERVAL 14 DAY "
        "AND (ai_impact <= -1 OR tags LIKE %s OR tags LIKE %s) ORDER BY news_date DESC", ("%negative%", "%no_dividend%"),
    ):
        bad_news[r["code"]].append(r)
    all_preds = preds
    if codes:  # long-term scores need the whole universe for percentile ranks
        all_preds = defaultdict(dict)
        if d:
            for r in db.query("SELECT * FROM predictions WHERE trade_date=%s", (d,)):
                all_preds[r["code"]][r["horizon"]] = r
        all_sigs = {s["code"]: s for s in db.query("SELECT * FROM signals WHERE trade_date=%s", (sd,))} if sd else {}
    else:
        all_sigs = sigs
    long_scores = {x["code"]: x["score"] for x in predict._long_term(all_preds, all_sigs, inst, info, False)}
    pf = portfolio.summary()
    s = settings.get_all()
    upcoming_record = {r["code"]: r["d"] for r in db.query(
        "SELECT code, MIN(record_date) AS d FROM news WHERE record_date > CURDATE() GROUP BY code")}
    return {
        "bd": db.get_state("bd_factors") or {}, "upcoming_record": upcoming_record,
        "date": d, "days": predict.next_trading_days(d, 245) if d else [],
        "bars": bars, "preds": preds, "sigs": sigs, "inst": inst, "bad_news": bad_news,
        "long_scores": long_scores, "holdings": {h["code"]: h for h in pf["holdings"]},
        "tables": db.get_state("trade_tables") or {},
        "capital": float(s.get("capital") or 0), "risk_pct": float(s.get("risk_pct") or 1),
        "max_position_pct": float(s.get("max_position_pct") or 20), "commission": float(s.get("commission_rate") or 0.4),
        "circuit": float(s.get("circuit_pct") or 9.5),
    }


def _plan(code: str, ctx: dict) -> dict | None:
    from . import predict

    bars, ph, sig, inst = ctx["bars"].get(code), ctx["preds"].get(code, {}), ctx["sigs"].get(code), ctx["inst"].get(code, {})
    if not bars or len(bars) < 25 or not ph:
        return None
    c = float(bars[-1]["close"])
    closes = np.array([float(b["close"]) for b in bars])
    rets = np.diff(np.log(closes[-61:]))
    sigma = float(rets.std()) if len(rets) > 10 else 0.02
    sigma = sigma if sigma > 0 else 0.02
    atr = ph.get(1, ph.get(5, {})).get("atr") or c * 0.02
    low20 = min(float(b["low"] or b["close"]) for b in bars[-20:])
    high20 = max(float(b["high"] or b["close"]) for b in bars[-20:])
    liq = ph.get(20, ph.get(1, {})).get("liq_mn") or 0.0
    p5, p20 = ph.get(5), ph.get(20)
    tables = ctx["tables"]

    stop = floor_tick(c - 1.5 * atr)
    t1 = ceil_tick(p5["high50"]) if p5 and p5.get("high50") else None
    t2 = ceil_tick(p20["high68"]) if p20 else None
    risk = c - stop if stop > 0 else None
    rr1 = (t1 - c) / risk if t1 and risk else None
    rr2 = (t2 - c) / risk if t2 and risk else None
    odds = lookup(tables, 20, math.log(t2 / c) / sigma, math.log(c / stop) / sigma) if t2 and stop > 0 and t2 > c else None
    odds5 = lookup(tables, 5, math.log(t1 / c) / sigma, math.log(c / stop) / sigma) if t1 and stop > 0 and t1 > c else None
    ev_pct = math.expm1(odds["ev_sigma"] * sigma) * 100 if odds else None

    nl, nh = tables.get("next_low", {}), tables.get("next_high", {})
    buy_deep = floor_tick(c * math.exp(nl["50"] * sigma)) if "50" in nl else None    # ~50% chance of a fill
    buy_easy = floor_tick(c * math.exp(nl["70"] * sigma)) if "70" in nl else None    # ~70% chance of a fill
    # P(next high >= y) = p  <=>  y = (100 - p)th percentile of the next-session high
    sell_easy = ceil_tick(c * math.exp(nh["50"] * sigma)) if "50" in nh else None     # ~50% chance price reaches it
    sell_stretch = ceil_tick(c * math.exp(nh["70"] * sigma)) if "70" in nh else None  # ~30% chance

    # position size: risk at most risk% of capital if the stop is hit; cap at max position %
    entry = buy_easy or c
    shares = None
    if ctx["capital"] > 0 and entry > stop > 0:
        by_risk = ctx["capital"] * ctx["risk_pct"] / 100 / (entry - stop)
        by_cap = ctx["capital"] * ctx["max_position_pct"] / 100 / entry
        shares = max(0, int(min(by_risk, by_cap)))

    s = sig or {}
    flags = set(analysis.parse_flags(s.get("flags")))
    sma20, sma50, sma200, rsi = s.get("sma20"), s.get("sma50"), s.get("sma200"), s.get("rsi")
    score = s.get("score")
    setup = predict.is_setup(c, sma20, sma50, rsi)
    downtrend = bool(sma20 and sma50 and c < sma50 and sma20 < sma50)
    bearish = downtrend or (score is not None and score <= -40) or "death_cross" in flags or "new_52w_low" in flags
    bad = ctx["bad_news"].get(code, [])
    very_bad = any((n["ai_impact"] or 0) <= -2 or "no_dividend" in (n["tags"] or "") for n in bad)
    liquid = liq >= 1.0
    at_circuit = (s.get("change_pct") or 0) >= ctx["circuit"]
    long_score = ctx["long_scores"].get(code)
    holding = ctx["holdings"].get(code)

    # ---- Bangladesh factors: only effects with moderate/strong evidence change the signal
    bd = ctx.get("bd") or {}
    studies = bd.get("studies", {})
    bd_notes, bd_block = [], None
    for iso, kind in (bd.get("recent_events") or {}).get(code, []):
        d0 = date.fromisoformat(iso)
        age = (ctx["date"] - d0).days if ctx["date"] else 99
        stt = studies.get(kind, {})
        if age > 28 or not stt.get("n"):
            continue
        line = (f"{stt['label']} on {d0:%d %b}: afterwards shares typically did {stt.get('ex20', 0):+.1f}% vs the market "
                f"over 20 sessions (median of {stt['n']} cases, {stt.get('evidence')} evidence)")
        bd_notes.append(line)
        if stt.get("direction") == "negative" and bd_block is None:
            bd_block = (kind, line, stt.get("evidence"))
    spec = (bd.get("spec_today") or {}).get(code)
    spec_hi = bool(spec and spec["score"] >= 60)
    regime = (bd.get("regime_now") or {}).get("regime")
    rstat = (bd.get("regime_stats") or {}).get(regime or "", {})
    market_stress = regime == "Stress" and (rstat.get("fwd20") or 0) < 0
    rec_date = ctx.get("upcoming_record", {}).get(code)
    rec_neg = (studies.get("record_date") or {}).get("direction") == "negative"
    record_soon = bool(rec_date and ctx["date"] and 0 < (rec_date - ctx["date"]).days <= 14 and rec_neg)

    reasons: list[str] = []
    trail = None
    if holding:
        # Decide on the outlook, not on the purchase price (positions are often entered later,
        # with an old cost). Trend stop = highest close of the last 60 sessions - 2.5 x ATR.
        recent = [float(b["close"]) for b in bars[-60:]] or [c]
        trail = floor_tick(max(recent) - 2.5 * atr)
        quality = long_score is not None and long_score >= 60
        srec = ctx["tables"].get("sell_record") or {}
        lag = (srec.get("all20") or 0) - (srec.get("fwd20") or 0)
        if bd_block and bd_block[2] == "strong":
            signal = "SELL"
            reasons.append(bd_block[1])
        elif c <= trail and not quality:
            signal = "SELL"
            reasons.append(f"Closed at {c:.2f}, below the trend stop {trail:.2f} (60-session high − 2.5 × ATR)")
        elif bearish and very_bad:
            signal = "SELL"
            reasons.append("Downtrend plus seriously negative news")
        elif (t1 and c >= t1) or (rsi and rsi > 75) or "bb_above" in flags:
            signal = "TAKE PROFIT"
            reasons.append("Stretched: at the upper target / RSI above 75 / above the upper Bollinger band")
        elif (bearish or c <= trail) and quality:
            signal = "HOLD"
            reasons.append(f"Trend is down, but long-term quality/value score is {long_score:.0f}/100: "
                           f"hold only if you invest for the long term (trend stop {trail:.2f})")
        elif bearish:
            signal = "REDUCE"
            reasons.append("Trend is down (price below a falling 50-day average)" if downtrend else "Strongly bearish technicals")
            if lag > 0:
                reasons.append(f"Historically, downtrend shares lagged the market by about {lag:.1f}% over the next "
                               "20 sessions: consider selling part, not panic-selling")
        else:
            signal = "HOLD"
            reasons.append(f"Trend intact; trend stop {trail:.2f}")
        if (holding.get("unrealized_pct") or 0) <= -15:
            reasons.append(f"You are {abs(holding['unrealized_pct']):.0f}% below your cost. That loss is already "
                           "made; judge the share on its outlook from here, not on getting back to cost")
    elif not liquid:
        signal = "AVOID"
        reasons.append(f"Illiquid: about Tk {liq:.1f} mn traded per day, hard to buy and sell")
    elif bad and very_bad:
        signal = "AVOID"
        reasons.append("Seriously negative news in the last 2 weeks: " + bad[0]["summary"])
    elif bd_block:
        signal = "AVOID"
        reasons.append(bd_block[1])
    elif spec_hi and (bd.get("spec_stats", {}).get("High", {}).get("ex20") or 0) < 0:
        signal = "AVOID"
        hs = bd["spec_stats"]["High"]
        reasons.append(f"Speculative pump-and-dump pattern (risk {spec['score']}/100: {', '.join(spec['why'][:3])}). "
                       f"Such shares typically lagged the market by {abs(hs['ex20']):.1f}% over 20 sessions; "
                       f"1 in 10 fell more than {abs(hs['p10']):.0f}%")
    elif bearish:
        signal = "AVOID"
        reasons.append("Downtrend: price below a falling 50-day average" if downtrend else "Bearish technicals")
    elif (setup and not at_circuit and inst.get("category") != "Z" and inst.get("asset_type") == "EQ" and liq >= 5
          and rr2 is not None and rr2 >= 1.5 and (ev_pct or 0) > 0):
        if market_stress:
            signal = "WAIT"
            reasons.append(f"Setup is there, but the market is in a Stress phase: after Stress days the market moved "
                           f"{rstat.get('fwd20'):+.1f}% over 20 sessions and rose only {rstat.get('pos20'):.0f}% of the time")
        elif record_soon:
            signal = "WAIT"
            reasons.append(f"Record date {rec_date:%d %b} is close: prices typically drop "
                           f"{abs(studies['record_date'].get('ex20') or 0):.1f}% vs the market after it; buy after the record date")
        else:
            signal = "BUY"
            reasons.append("Uptrend pullback setup (above 50-day avg, RSI 40-65, near 20-day avg)")
            reasons.append(f"Reward:risk 1:{rr2:.1f} to T2 with a positive historical expected value")
    elif long_score is not None and long_score >= 70 and not at_circuit:
        signal = "ACCUMULATE"
        reasons.append(f"Long-term quality/value score {long_score:.0f}/100: suited to regular buying")
    else:
        signal = "WAIT"
        reasons.append("No clear setup right now")
    if inst.get("category") == "Z":
        reasons.append("Category Z: higher risk")
    if at_circuit:
        reasons.append("At the daily circuit limit: hard to buy at a fair price")
    if bad and not very_bad:
        reasons.append("Negative news in the last 2 weeks: " + bad[0]["summary"])
    for note in bd_notes:
        if not bd_block or note != bd_block[1]:
            reasons.append(note)
    if spec and spec["score"] >= 30 and signal not in ("AVOID",):
        reasons.append(f"Speculation risk {spec['score']}/100: {', '.join(spec['why'][:3])}")

    days = ctx["days"]

    def day(n: int):
        return days[min(n, len(days)) - 1] if days else None

    def rng(h: int, lv: int = 50):
        r = ph.get(h)
        return [r.get(f"low{lv}"), r.get(f"high{lv}")] if r and r.get(f"low{lv}") is not None else None

    rec = (ctx["tables"].get("buy_record") or {})
    dates = {
        "short": {  # a swing trade: enter next session, targets within about a month
            "from": day(1), "to": day(20), "entry": day(1), "t1_by": day(5), "t2_by": day(20), "exit_by": day(20),
            "typical_hold_sessions": rec.get("avg_days"),
            "typical_exit": day(int(round(rec["avg_days"]))) if rec.get("avg_days") else None,
            "range_1w": rng(5), "range_1m": rng(20),
        },
        "long": {  # regular buying: accumulate for ~3 months, review at 6 months, hold ~1 year
            "from": day(21), "to": day(245), "accumulate_from": day(1), "accumulate_to": day(60),
            "review": day(120), "horizon": day(245),
            "range_3m": rng(60), "range_6m": rng(120), "range_1y": rng(245), "range_1y_68": rng(245, 68),
        },
    }

    return {
        "code": code, "name": inst.get("name"), "sector": inst.get("sector"), "category": inst.get("category"),
        "date": ctx["date"], "close": c, "signal": signal, "reasons": reasons, "dates": dates,
        "bd": {"spec": spec, "regime": regime, "regime_fwd20": rstat.get("fwd20"), "record_date": rec_date,
               "events": bd_notes, "blocked_by": bd_block[0] if bd_block else None},
        "score": score, "label": s.get("label"), "rsi": _r(rsi, 1), "long_score": long_score,
        "sigma_pct": _r(sigma * 100, 2), "atr": _r(atr), "liq_mn": _r(liq, 1),
        "buy_zone": [buy_deep, buy_easy] if buy_deep and buy_easy else None,
        "sell_zone": [sell_easy, sell_stretch] if sell_easy and sell_stretch else None,
        "stop": stop, "stop_pct": _r((stop / c - 1) * 100, 1), "support": floor_tick(low20), "resistance": ceil_tick(high20),
        "t1": t1, "t1_pct": _r((t1 / c - 1) * 100, 1) if t1 else None,
        "t2": t2, "t2_pct": _r((t2 / c - 1) * 100, 1) if t2 else None,
        "rr1": _r(rr1, 2), "rr2": _r(rr2, 2),
        "win_rate": _r(odds["win_rate"] * 100, 0) if odds else None,
        "stop_rate": _r(odds["stop_rate"] * 100, 0) if odds else None,
        "ev_pct": _r(ev_pct, 2), "win_rate_1w": _r(odds5["win_rate"] * 100, 0) if odds5 else None,
        "shares": shares, "position_value": _r(shares * entry, 0) if shares else None,
        "risk_amount": _r(shares * (entry - stop), 0) if shares else None, "entry": entry,
        "holding": {"qty": holding["qty"], "avg_cost": holding["avg_cost"], "unrealized_pct": holding.get("unrealized_pct"),
                    "trailing_stop": trail} if holding else None,
    }


def plan_for(code: str) -> dict | None:
    return _plan(code, _load_context([code]))


def all_plans() -> dict:
    ctx = _load_context()
    plans = [p for p in (_plan(code, ctx) for code in ctx["preds"]) if p]
    plans = [p for p in plans if (ctx["inst"].get(p["code"]) or {}).get("asset_type") in ("EQ", "MF")]
    by = defaultdict(list)
    for p in plans:
        by[p["signal"]].append(p)
    by["BUY"].sort(key=lambda p: -(p["ev_pct"] or -99))
    by["ACCUMULATE"].sort(key=lambda p: -(p["long_score"] or 0))
    watch = {w["code"] for w in db.query("SELECT code FROM watchlist")}
    return {"date": ctx["date"], "counts": {k: len(v) for k, v in by.items()},
            "buy": by["BUY"][:15], "buy_all": by["BUY"], "accumulate": by["ACCUMULATE"][:15],
            "avoid_bd": [p for p in by["AVOID"] if p["bd"]["blocked_by"] or (p["bd"]["spec"] or {}).get("score", 0) >= 60][:20],
            "regime": (ctx["bd"].get("regime_now") or {}).get("regime"),
            "holdings": [p for p in plans if p["holding"]],
            "watch": [p for p in plans if p["code"] in watch],
            "tables": {k: v for k, v in ctx["tables"].items() if k in ("buy_record", "sell_record", "built_at")},
            "settings": {"capital": ctx["capital"], "risk_pct": ctx["risk_pct"], "max_position_pct": ctx["max_position_pct"]}}


# ============================================================================ paper trading (live signal record)

FINAL = ("target", "stopped", "expired", "missed")


def _follow(s: dict, bars: list[dict], commission_pct: float) -> dict | None:
    """Follow one logged BUY signal with real daily prices.
    Fill: next session, if its low reaches the entry limit (price = min(open, limit)).
    Then the first of: stop (low <= stop), Target 2 (high >= T2), or the exit date (close).
    A day that touches both stop and target counts as stopped (conservative)."""
    status, entry, entry_date = s["status"], s["entry_price"], s["entry_date"]
    if status == "pending":
        b = bars[0]
        low = b["low"] or b["close"]
        if low is None or low > s["entry_limit"]:
            return {"status": "missed", "exit_date": b["trade_date"]}
        op = b["open"] or b["close"]
        entry = min(op, s["entry_limit"]) if op else s["entry_limit"]
        entry_date, status = b["trade_date"], "open"
    start = next((i for i, b in enumerate(bars) if b["trade_date"] >= entry_date), None)
    if start is None:
        return None
    hi_max = lo_min = entry
    for j in range(start, len(bars)):
        b = bars[j]
        high, low = b["high"] or b["close"], b["low"] or b["close"]
        op = b["open"] or b["close"]
        hi_max, lo_min = max(hi_max, high), min(lo_min, low)
        exit_price, final = None, None
        if low <= s["stop"]:
            exit_price, final = (min(op, s["stop"]) if j > start else s["stop"]), "stopped"
        elif high >= s["t2"]:
            exit_price, final = (max(op, s["t2"]) if j > start else s["t2"]), "target"
        elif s["exit_by"] and b["trade_date"] >= s["exit_by"]:
            exit_price, final = b["close"], "expired"
        if final:
            return {"status": final, "entry_date": entry_date, "entry_price": entry, "exit_date": b["trade_date"],
                    "exit_price": exit_price, "return_pct": round((exit_price / entry - 1) * 100 - 2 * commission_pct, 2),
                    "max_gain_pct": round((hi_max / entry - 1) * 100, 2), "max_loss_pct": round((lo_min / entry - 1) * 100, 2),
                    "sessions": j - start + 1}
    last = bars[-1]
    return {"status": "open", "entry_date": entry_date, "entry_price": entry, "exit_date": None, "exit_price": last["close"],
            "return_pct": round((last["close"] / entry - 1) * 100 - 2 * commission_pct, 2),
            "max_gain_pct": round((hi_max / entry - 1) * 100, 2), "max_loss_pct": round((lo_min / entry - 1) * 100, 2),
            "sessions": len(bars) - start}


def update_signal_log(trade_date: date) -> dict:
    """Follow open paper trades with today's prices, then record today's BUY signals."""
    comm = float(settings.get("commission_rate") or 0.4)
    updated = 0
    for s in db.query("SELECT * FROM signal_log WHERE status IN ('pending', 'open')"):
        bars = db.query(
            "SELECT trade_date, open, high, low, close FROM daily_prices WHERE code=%s AND trade_date > %s "
            "ORDER BY trade_date LIMIT 60", (s["code"], s["signal_date"]))
        if not bars:
            continue
        u = _follow(s, bars, comm)
        if not u:
            continue
        cols = ", ".join(f"{k}=%s" for k in u)
        db.execute(f"UPDATE signal_log SET {cols} WHERE id=%s", (*u.values(), s["id"]))
        updated += 1
    ap = all_plans()
    rows = [(trade_date, p["code"], "BUY", p["close"], (p["buy_zone"] or [None, p["close"]])[1] or p["close"],
             p["stop"], p["t1"], p["t2"], p["dates"]["short"]["exit_by"], p["win_rate"], p["ev_pct"])
            for p in ap.get("buy_all", ap["buy"]) if p["stop"] and p["t2"]]
    db.executemany(
        "INSERT IGNORE INTO signal_log (signal_date, code, signal_type, close_at_signal, entry_limit, stop, t1, t2, "
        "exit_by, win_rate, ev_pct) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    log.info("Signal log: %d updated, %d new BUY signals for %s", updated, len(rows), trade_date)
    return {"updated": updated, "logged": len(rows)}


def signal_record(limit: int = 60) -> dict:
    rows = db.query("SELECT s.*, i.name FROM signal_log s LEFT JOIN instruments i ON i.code=s.code "
                    "ORDER BY s.signal_date DESC, s.ev_pct DESC LIMIT %s", (limit,))
    closed = db.query("SELECT status, return_pct, sessions FROM signal_log WHERE status IN ('target', 'stopped', 'expired')")
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM signal_log GROUP BY status")}
    n = len(closed)
    rets = [r["return_pct"] for r in closed if r["return_pct"] is not None]
    open_rets = [r["return_pct"] for r in db.query("SELECT return_pct FROM signal_log WHERE status='open'") if r["return_pct"] is not None]
    return {
        "since": db.scalar("SELECT MIN(signal_date) FROM signal_log"),
        "counts": counts,
        "closed": n,
        "win_rate": round(sum(1 for r in closed if r["status"] == "target") / n * 100, 1) if n else None,
        "stop_rate": round(sum(1 for r in closed if r["status"] == "stopped") / n * 100, 1) if n else None,
        "avg_return": round(float(np.mean(rets)), 2) if rets else None,
        "total_return": round(float(np.sum(rets)), 2) if rets else None,
        "avg_sessions": round(float(np.mean([r["sessions"] for r in closed if r["sessions"]])), 1) if n else None,
        "open_avg_return": round(float(np.mean(open_rets)), 2) if open_rets else None,
        "rows": rows,
    }


def intraday_range(code: str, quote: dict, state: dict, sigma: float, quantiles: dict) -> dict | None:
    """While DSE is open: likely range for TODAY's close from the live price, shrinking as the
    session runs out (variance ~ time left)."""
    if not state or not state.get("is_open") or not quote or not quote.get("ltp"):
        return None
    sess = state.get("session") or {}
    today = sess.get("today") or {}
    try:
        o_h, o_m = (int(x) for x in str(today.get("opening") or sess.get("opening") or "10:00").split(":")[:2])
        c_h, c_m = (int(x) for x in str(today.get("closing") or sess.get("closing") or "14:00").split(":")[:2])
    except ValueError:
        return None
    now = datetime.now()
    start, end = now.replace(hour=o_h, minute=o_m, second=0), now.replace(hour=c_h, minute=c_m, second=0)
    if not start < now < end:
        return None
    frac = (end - now).total_seconds() / (end - start).total_seconds()
    s = sigma * math.sqrt(frac)
    ltp = float(quote["ltp"])
    out = {"ltp": ltp, "time_left_pct": round(frac * 100), "as_of": now.isoformat(timespec="minutes")}
    for lv, (lo_k, hi_k) in (("50", ("q25", "q75")), ("90", ("q5", "q95"))):
        out["low" + lv] = floor_tick(ltp * math.exp(quantiles[lo_k] * s))
        out["high" + lv] = ceil_tick(ltp * math.exp(quantiles[hi_k] * s))
    return out
