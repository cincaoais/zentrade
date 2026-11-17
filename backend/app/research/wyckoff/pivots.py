# Ported from wyk/detectors/pivots.py @ fc142999 (user's own code).
"""Confirmed swing pivots and trend context (docs/01 §5.1).

Pivot high at p (confirmation lag k): H_p > max(H[p-k..p-1]) and H_p >= max(H[p+1..p+k]). It is known at p+k.
Pivot low mirrored. Trend: Down_t := C_t < EMA50_t and (C_{t-L} - C_t) >= D * ATR_{t-1}; Up_t mirrored.
"""

import numpy as np
import pandas as pd

from app.research.wyckoff.base import Candles, Event


def pivots(k: Candles, lag: int = 3) -> list[Event]:
    """All confirmed pivots, each with known_at = idx + lag. Strict on the left, non-strict on the right, so a
    flat double top yields the first bar as the pivot."""
    out: list[Event] = []
    h, l = k.h, k.l
    n = len(k)
    for p in range(lag, n - lag):
        left_h, right_h = h[p - lag:p], h[p + 1:p + lag + 1]
        if h[p] > left_h.max() and h[p] >= right_h.max():
            out.append(Event("pivot_high", p + lag, p, +1, float(h[p])))
        left_l, right_l = l[p - lag:p], l[p + 1:p + lag + 1]
        if l[p] < left_l.min() and l[p] <= right_l.min():
            out.append(Event("pivot_low", p + lag, p, -1, float(l[p])))
    out.sort(key=lambda e: (e.known_at, e.idx))
    return out


def trend_context(k: Candles, ema_n: int = 50, lookback: int = 60, depth_atr: float = 8.0) -> tuple[np.ndarray, np.ndarray]:
    """(down, up) boolean arrays, causal at each bar's close."""
    c = pd.Series(k.c)
    ema = c.ewm(span=ema_n, adjust=False).mean().to_numpy()
    prev = c.shift(lookback).to_numpy()
    down = (k.c < ema) & ((prev - k.c) >= depth_atr * k.atrp)
    up = (k.c > ema) & ((k.c - prev) >= depth_atr * k.atrp)
    return np.nan_to_num(down, nan=0).astype(bool), np.nan_to_num(up, nan=0).astype(bool)
