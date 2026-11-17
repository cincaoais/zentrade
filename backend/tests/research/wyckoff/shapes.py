"""Synthetic price/volume shapes for detector tests. Deterministic; no market data."""

import numpy as np

from app.research.wyckoff.base import candles_from_arrays


def _bars_from_path(path, vol, noise=0.15, seed=0):
    rng = np.random.default_rng(seed)
    c = np.asarray(path, dtype=float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.2, 0.6, len(c)) * noise * 4
    l = np.minimum(o, c) - rng.uniform(0.2, 0.6, len(c)) * noise * 4
    return o, h, l, c, np.asarray(vol, dtype=float)


def accumulation(seed: int = 0, spring: bool = True, spring_volume: float = 60.0):
    """Long decline -> SC (wide, huge volume) -> AR -> quiet ST higher low -> range -> optional spring
    (break below SC low on low volume, recovery next bar) -> SOS breakout on volume -> LPS pullback -> markup.
    Returns (candles, marks) where marks has the intended bar indices."""
    rng = np.random.default_rng(seed)
    path, vol, marks = [], [], {}
    p = 200.0
    for _ in range(120):                       # slow down-trend, normal volume
        p -= 0.6 + rng.normal(0, 0.3)
        path.append(p); vol.append(100 + rng.normal(0, 10))
    marks["sc"] = len(path)
    p -= 6.0
    path.append(p); vol.append(400)            # SC: big drop, climax volume
    sc_low_close = p
    for j in range(6):                         # AR rally
        p += 2.2
        path.append(p); vol.append(160)
    marks["ar_top"] = len(path) - 1
    for j in range(6):                         # decline to ST, drying volume
        p -= 2.0
        path.append(p); vol.append(70)
    marks["st"] = len(path) - 1
    for j in range(40):                        # trading range (phase B)
        p = sc_low_close + 6 + 4 * np.sin(j / 3.0) + rng.normal(0, 0.3)
        path.append(p); vol.append(90 + rng.normal(0, 8))
    if spring:
        marks["spring"] = len(path)
        p = sc_low_close - 1.5                  # break below the range on low volume
        path.append(p); vol.append(spring_volume)
        p = sc_low_close + 4
        path.append(p); vol.append(110)        # recovery bar
        marks["recovery"] = len(path) - 1
    for j in range(5):
        p = sc_low_close + 6 + rng.normal(0, 0.3)
        path.append(p); vol.append(85)
    marks["sos"] = len(path)
    for j in range(3):                         # SOS: strong rally on volume above the AR high
        p += 5.5
        path.append(p); vol.append(260)
    for j in range(5):                         # LPS: quiet pullback
        p -= 0.8
        path.append(p); vol.append(70)
    marks["lps_end"] = len(path) - 1
    for j in range(30):
        p += 1.0 + rng.normal(0, 0.3)
        path.append(p); vol.append(120)
    o, h, l, c, v = _bars_from_path(path, vol, seed=seed)
    return candles_from_arrays(o, h, l, c, v, v2=v * 0.6), marks


def distribution(seed: int = 0):
    """Mirror of accumulation: rally -> BC -> AR -> ST -> range -> UTAD -> SOW."""
    k, marks = accumulation(seed=seed)
    o, h, l, c = 400 - k.o, 400 - k.l, 400 - k.h, 400 - k.c
    return candles_from_arrays(o, h, l, c, k.v, v2=k.v2), marks
