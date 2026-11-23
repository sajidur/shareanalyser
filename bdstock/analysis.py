"""Per-stock technical analysis: indicator values, a transparent -100..+100 score with
the reasons behind it, and flags used by the screener, alerts and daily report.

This is a rules-based summary of chart conditions - not investment advice."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean
from typing import Any

from . import db, indicators as ind, settings

log = logging.getLogger(__name__)

# Treasury bonds and corporate/debt securities: excluded from signals, movers and sectors.
DEBT_ASSET_TYPES = {"GOVDBT", "CB", "DB"}


def is_debt(asset_type: str | None) -> bool:
    return (asset_type or "") in DEBT_ASSET_TYPES


FLAG_LABELS = {
    "golden_cross": "Golden cross (50 > 200 DMA)",
    "death_cross": "Death cross (50 < 200 DMA)",
    "macd_bull_cross": "MACD bullish crossover",
    "macd_bear_cross": "MACD bearish crossover",
    "oversold": "RSI oversold (< 30)",
    "overbought": "RSI overbought (> 70)",
    "vol_spike": "Volume spike (≥ 2× avg)",
    "new_52w_high": "New 52-week high",
    "new_52w_low": "New 52-week low",
    "near_52w_high": "Within 5% of 52-week high",
    "near_52w_low": "Within 5% of 52-week low",
    "above_sma200": "Above 200-day average",
    "bb_above": "Above upper Bollinger band",
    "bb_below": "Below lower Bollinger band",
    "upper_circuit": "Near upper circuit limit",
    "lower_circuit": "Near lower circuit limit",
    "z_category": "Category Z (higher risk)",
}


def label_for(score: int) -> str:
    if score >= 40:
        return "Strong Bullish"
    if score >= 15:
        return "Bullish"
    if score > -15:
        return "Neutral"
    if score > -40:
        return "Bearish"
    return "Strong Bearish"


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def analyze_series(bars: list[dict], category: str | None = None, circuit_pct: float = 9.5) -> dict | None:
    """bars: daily rows sorted oldest -> newest with open/high/low/close/volume."""
    n = len(bars)
    if n < 20:
        return None
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"] or b["close"]) for b in bars]
    lows = [float(b["low"] or b["close"]) for b in bars]
    vols = [float(b["volume"] or 0) for b in bars]
    i = n - 1
    c = closes[i]

    sma20, sma50, sma200 = ind.sma(closes, 20), ind.sma(closes, 50), ind.sma(closes, 200)
    rsi = ind.rsi(closes, 14)
    macd_line, macd_sig, macd_hist = ind.macd(closes)
    bb_mid, bb_up, bb_lo = ind.bollinger(closes, 20, 2)
    atr = ind.atr(highs, lows, closes, 14)

    prior_vols = vols[max(0, i - 20) : i]
    avg_vol20 = mean(prior_vols) if prior_vols else 0
    vol_ratio = vols[i] / avg_vol20 if avg_vol20 > 0 else None
    window = slice(max(0, n - 250), n)
    high52, low52 = max(highs[window]), min(lows[window])
    prior_closes = closes[max(0, n - 251) : i]
    change_pct = (c / closes[i - 1] - 1) * 100 if closes[i - 1] else None

    def ret(k: int) -> float | None:
        return (c / closes[i - k] - 1) * 100 if i - k >= 0 and closes[i - k] else None

    score = 0
    reasons: list[dict] = []
    flags: list[str] = []

    def add(points: int, text: str) -> None:
        nonlocal score
        score += points
        reasons.append({"points": points, "text": text})

    # --- trend
    if sma20[i] is not None:
        up = c > sma20[i]
        add(8 if up else -8, f"Price {'above' if up else 'below'} 20-day average ({sma20[i]:.2f})")
    if sma50[i] is not None:
        up = c > sma50[i]
        add(10 if up else -10, f"Price {'above' if up else 'below'} 50-day average ({sma50[i]:.2f})")
    if sma50[i] is not None and sma200[i] is not None:
        up = sma50[i] > sma200[i]
        add(12 if up else -12, f"50-day average {'above' if up else 'below'} 200-day: long-term {'uptrend' if up else 'downtrend'}")
        cross = ind.crossed(sma50, sma200, i, 5)
        if cross > 0:
            flags.append("golden_cross")
            add(10, "Golden cross in the last 5 sessions")
        elif cross < 0:
            flags.append("death_cross")
            add(-10, "Death cross in the last 5 sessions")
    if sma200[i] is not None and c > sma200[i]:
        flags.append("above_sma200")

    # --- momentum
    if macd_hist[i] is not None:
        up = macd_hist[i] > 0
        add(8 if up else -8, f"MACD {'above' if up else 'below'} its signal line")
        cross = ind.crossed(macd_line, macd_sig, i, 3)
        if cross > 0:
            flags.append("macd_bull_cross")
            add(10, "MACD bullish crossover in the last 3 sessions")
        elif cross < 0:
            flags.append("macd_bear_cross")
            add(-10, "MACD bearish crossover in the last 3 sessions")
    r = rsi[i]
    if r is not None:
        if r < 30:
            flags.append("oversold")
            add(12, f"RSI {r:.1f}: oversold, rebound possible")
        elif r > 70:
            flags.append("overbought")
            add(-12, f"RSI {r:.1f}: overbought, pullback risk")
        elif r >= 55:
            add(5, f"RSI {r:.1f}: positive momentum")
        elif r <= 45:
            add(-5, f"RSI {r:.1f}: weak momentum")
        else:
            add(0, f"RSI {r:.1f}: neutral")

    # --- volatility bands
    if bb_lo[i] is not None and c < bb_lo[i]:
        flags.append("bb_below")
        add(6, "Closed below the lower Bollinger band (stretched to the downside)")
    elif bb_up[i] is not None and c > bb_up[i]:
        flags.append("bb_above")
        add(-4, "Closed above the upper Bollinger band (stretched to the upside)")

    # --- volume
    if vol_ratio is not None and vol_ratio >= 2:
        flags.append("vol_spike")
        if change_pct and change_pct > 0:
            add(10, f"Volume {vol_ratio:.1f}× the 20-day average on an up day (buying pressure)")
        elif change_pct and change_pct < 0:
            add(-10, f"Volume {vol_ratio:.1f}× the 20-day average on a down day (selling pressure)")
        else:
            add(0, f"Volume {vol_ratio:.1f}× the 20-day average")

    # --- 52-week range
    if prior_closes and c > max(prior_closes):
        flags.append("new_52w_high")
        add(8, "New 52-week closing high")
    elif c >= high52 * 0.95:
        flags.append("near_52w_high")
    if prior_closes and c < min(prior_closes):
        flags.append("new_52w_low")
        add(-8, "New 52-week closing low")
    elif c <= low52 * 1.05:
        flags.append("near_52w_low")

    # --- Bangladesh-specific context (information only, no points)
    if change_pct is not None and change_pct >= circuit_pct:
        flags.append("upper_circuit")
        add(0, f"Up {change_pct:.1f}% today: close to the DSE daily circuit-breaker limit")
    elif change_pct is not None and change_pct <= -circuit_pct:
        flags.append("lower_circuit")
        add(0, f"Down {abs(change_pct):.1f}% today: close to the DSE daily circuit-breaker limit")
    if category == "Z":
        flags.append("z_category")
        add(0, "Category Z: company missed AGM/dividend requirements (higher risk)")
    if n < 200:
        add(0, f"Only {n} sessions of history: long-term signals are limited")

    score = max(-100, min(100, score))
    last = bars[i]
    hi, lo_, cl = float(last["high"] or c), float(last["low"] or c), c
    pivot = (hi + lo_ + cl) / 3
    return {
        "close": _r(c),
        "change_pct": _r(change_pct),
        "score": score,
        "label": label_for(score),
        "reasons": reasons,
        "flags": flags,
        "rsi": _r(r),
        "macd": _r(macd_line[i], 3),
        "macd_signal": _r(macd_sig[i], 3),
        "macd_hist": _r(macd_hist[i], 3),
        "sma20": _r(sma20[i]),
        "sma50": _r(sma50[i]),
        "sma200": _r(sma200[i]),
        "bb_upper": _r(bb_up[i]),
        "bb_lower": _r(bb_lo[i]),
        "atr": _r(atr[i]),
        "atr_pct": _r(atr[i] / c * 100) if atr[i] and c else None,
        "avg_vol20": round(avg_vol20) if avg_vol20 else None,
        "vol_ratio": _r(vol_ratio),
        "high52": _r(high52),
        "low52": _r(low52),
        "pct_from_high52": _r((c / high52 - 1) * 100) if high52 else None,
        "pct_from_low52": _r((c / low52 - 1) * 100) if low52 else None,
        "ret_1w": _r(ret(5)),
        "ret_1m": _r(ret(21)),
        "ret_3m": _r(ret(63)),
        "ret_6m": _r(ret(126)),
        "ret_1y": _r(ret(250)),
        "pivots": {
            "R2": _r(pivot + (hi - lo_)),
            "R1": _r(2 * pivot - lo_),
            "P": _r(pivot),
            "S1": _r(2 * pivot - hi),
            "S2": _r(pivot - (hi - lo_)),
        },
        "sessions": n,
    }


def chart_series(bars: list[dict]) -> dict[str, list[dict]]:
    """Indicator series aligned to bars, in TradingView lightweight-charts format.
    Missing values become whitespace points ({time}) so all panes stay aligned."""
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"] or b["close"]) for b in bars]
    lows = [float(b["low"] or b["close"]) for b in bars]
    times = [str(b["trade_date"]) for b in bars]
    mid, up, lo = ind.bollinger(closes, 20, 2)
    line, sig, hist = ind.macd(closes)

    def pts(values: list[float | None], nd: int = 2) -> list[dict]:
        return [{"time": t} if v is None else {"time": t, "value": round(v, nd)} for t, v in zip(times, values)]

    return {
        "sma20": pts(ind.sma(closes, 20)),
        "sma50": pts(ind.sma(closes, 50)),
        "sma200": pts(ind.sma(closes, 200)),
        "bb_upper": pts(up),
        "bb_lower": pts(lo),
        "rsi": pts(ind.rsi(closes, 14)),
        "macd": pts(line, 3),
        "macd_signal": pts(sig, 3),
        "macd_hist": pts(hist, 3),
        "atr": pts(ind.atr(highs, lows, closes, 14)),
    }


SIGNAL_UPSERT = """INSERT INTO signals (code, trade_date, close, change_pct, score, label, rsi, macd_hist,
    sma20, sma50, sma200, vol_ratio, high52, low52, ret_1w, ret_1m, ret_3m, ret_1y, flags, reasons)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE close=VALUES(close), change_pct=VALUES(change_pct), score=VALUES(score),
    label=VALUES(label), rsi=VALUES(rsi), macd_hist=VALUES(macd_hist), sma20=VALUES(sma20),
    sma50=VALUES(sma50), sma200=VALUES(sma200), vol_ratio=VALUES(vol_ratio), high52=VALUES(high52),
    low52=VALUES(low52), ret_1w=VALUES(ret_1w), ret_1m=VALUES(ret_1m), ret_3m=VALUES(ret_3m),
    ret_1y=VALUES(ret_1y), flags=VALUES(flags), reasons=VALUES(reasons)"""


def analyze_all(trade_date: date, lookback_days: int = 420) -> int:
    """Analyse every instrument that has a price row on trade_date; store in `signals`."""
    rows = db.query(
        "SELECT code, trade_date, open, high, low, close, volume FROM daily_prices "
        "WHERE trade_date BETWEEN %s AND %s AND close IS NOT NULL AND close > 0 "
        "ORDER BY code, trade_date",
        (trade_date - timedelta(days=lookback_days), trade_date),
    )
    meta = {r["code"]: r for r in db.query("SELECT code, category, asset_type FROM instruments")}
    categories = {c: m["category"] for c, m in meta.items()}
    circuit = settings.get_float("circuit_pct")
    by_code: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_code[r["code"]].append(r)
    out: list[tuple] = []
    for code, bars in by_code.items():
        if bars[-1]["trade_date"] != trade_date or is_debt((meta.get(code) or {}).get("asset_type")):
            continue
        try:
            a = analyze_series(bars, categories.get(code), circuit)
        except Exception:  # never let one bad series stop the run
            log.exception("Analysis failed for %s", code)
            continue
        if not a:
            continue
        out.append(
            (
                code, trade_date, a["close"], a["change_pct"], a["score"], a["label"], a["rsi"],
                a["macd_hist"], a["sma20"], a["sma50"], a["sma200"], a["vol_ratio"], a["high52"],
                a["low52"], a["ret_1w"], a["ret_1m"], a["ret_3m"], a["ret_1y"],
                ",".join(a["flags"])[:512], json.dumps(a["reasons"]),
            )
        )
    db.executemany(SIGNAL_UPSERT, out)
    types = ",".join(f"'{t}'" for t in sorted(DEBT_ASSET_TYPES))
    db.execute(f"DELETE s FROM signals s JOIN instruments i ON i.code=s.code WHERE i.asset_type IN ({types})")
    log.info("Analysed %d instruments for %s", len(out), trade_date)
    return len(out)


def latest_signal_date() -> date | None:
    return db.scalar("SELECT MAX(trade_date) FROM signals")


def parse_flags(value: Any) -> list[str]:
    return [f for f in str(value or "").split(",") if f]
