"""Technical indicators in plain Python. Every function returns a list aligned with
its input, with None where there is not yet enough data."""
from __future__ import annotations

import math
from typing import Sequence

Series = list[float | None]


def sma(values: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: Sequence[float | None], n: int) -> Series:
    """Exponential moving average seeded with the SMA of the first n values.
    Leading None values (e.g. from another indicator) are skipped."""
    out: Series = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) - start < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[start : start + n]) / n  # type: ignore[arg-type]
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        v = values[i]
        if v is None:
            out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    """Wilder's Relative Strength Index."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist: Series = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[Series, Series, Series]:
    mid = sma(closes, n)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        m = mid[i]
        window = closes[i - n + 1 : i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / n)  # type: ignore[operator]
        upper[i] = m + k * sd  # type: ignore[operator]
        lower[i] = m - k * sd  # type: ignore[operator]
    return mid, upper, lower


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> Series:
    """Wilder's Average True Range."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    trs = [0.0] * len(closes)
    for i in range(1, len(closes)):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    prev = sum(trs[1 : n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(closes)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def crossed(a: Series, b: Series, i: int, lookback: int) -> int:
    """+1 if series a crossed above b within the last `lookback` bars ending at i,
    -1 if it crossed below, 0 otherwise. Most recent cross wins."""
    for j in range(i, max(0, i - lookback), -1):
        a0, b0, a1, b1 = a[j - 1], b[j - 1], a[j], b[j]
        if None in (a0, b0, a1, b1):
            return 0
        if a0 <= b0 and a1 > b1:  # type: ignore[operator]
            return 1
        if a0 >= b0 and a1 < b1:  # type: ignore[operator]
            return -1
    return 0
