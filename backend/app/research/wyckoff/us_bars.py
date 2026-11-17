# Ported from wyk/detectors/base.py @ fc142999 (user's own code).
"""US daily bars -> :class:`Candles` (D1 volume path, no TICK rounding, no XAUUSD sessions).

``us_candles`` is the ``prepare`` replacement for US equities: it keeps the ``wyk`` D1 feature definitions
(``rv`` = V / trailing 20-bar median, ``ae`` as in ``wyk.features.volume.abnormal_effort``) but reads its own
calendar columns (holidays, news, halts) instead of ``wyk.data.bars``. Bars with ``high <= low`` are dropped, as
``prepare`` does, and a stored-bar gap longer than five calendar days is flagged as ``post_gap``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.research.wyckoff.base import Candles, wilder_atr
from app.research.wyckoff.volume import abnormal_effort, rv

_MS_PER_DAY = 86_400_000
_HALT_GAP_DAYS = 5


def us_candles(df: pd.DataFrame) -> tuple[Candles, np.ndarray]:
    """Build the ``wyk`` D1 candles of one US symbol.

    ``df`` has ``date, open, high, low, close, volume``; ``date`` is a date-like column and ``volume`` is the real
    share count as stored in the enriched table (split-adjusted, so a split does not break the RV ratio). Returns ``(candles, kept_dates)`` where ``kept_dates[i]`` is the ``datetime.date`` of
    candle ``i`` (zero-range bars are dropped, as in ``wyk``'s ``prepare``).
    """
    dates = pd.to_datetime(pd.Series(df["date"])).dt.date.to_numpy()
    o = df["open"].to_numpy(dtype="float64")
    h = df["high"].to_numpy(dtype="float64")
    l = df["low"].to_numpy(dtype="float64")
    c = df["close"].to_numpy(dtype="float64")
    v = df["volume"].to_numpy(dtype="float64")

    keep = h > l
    o, h, l, c, v = o[keep], h[keep], l[keep], c[keep], v[keep]
    kept_dates = dates[keep]

    n = len(o)
    t = np.array(kept_dates, dtype="datetime64[D]").astype("datetime64[ms]").astype("int64")
    tc = t + _MS_PER_DAY

    post_gap = np.zeros(n, dtype=bool)
    if n > 1:
        gap_days = (t[1:] - t[:-1]) // _MS_PER_DAY
        post_gap[1:] = gap_days > _HALT_GAP_DAYS

    rng = h - l
    atr = wilder_atr(h, l, c, post_gap)
    atrp = np.r_[np.nan, atr[:-1]]

    holiday = np.zeros(n, dtype=bool)
    news = np.zeros(n, dtype=bool)
    ex = pd.Series(holiday)
    vol = pd.Series(v, dtype="float64")
    rel_vol = rv(vol, exclude=ex).to_numpy(dtype="float64")
    ae = abnormal_effort(vol, pd.Series(rng), pd.Series(atrp), exclude=ex).to_numpy(dtype="float64")

    nan = np.full(n, np.nan)
    candles = Candles(
        "D1",
        "ftmo",
        t,
        tc,
        o,
        h,
        l,
        c,
        rng,
        atr,
        atrp,
        v,
        rel_vol,
        ae,
        nan.copy(),
        nan.copy(),
        nan.copy(),
        holiday,
        news,
        post_gap,
        holiday.copy(),
    )
    return candles, kept_dates


__all__ = ["us_candles"]