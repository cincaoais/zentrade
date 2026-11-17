"""Causal price and volume features for the research harness."""

from __future__ import annotations

import numpy as np


def atr14(high, low, close) -> np.ndarray:
    """Wilder ATR(14) including bar ``t``.

    The first 13 values are NaN; index 13 is the mean of the first 14 true
    ranges, and later values use Wilder smoothing.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n == 0:
        return out

    true_range = np.empty(n, dtype=float)
    true_range[0] = high[0] - low[0]
    if n > 1:
        previous_close = close[:-1]
        true_range[1:] = np.maximum.reduce(
            [
                high[1:] - low[1:],
                np.abs(high[1:] - previous_close),
                np.abs(low[1:] - previous_close),
            ]
        )

    if n >= 14:
        out[13] = true_range[:14].mean()
        for index in range(14, n):
            out[index] = (out[index - 1] * 13 + true_range[index]) / 14
    return out


def rel_volume(volume) -> np.ndarray:
    """``volume[t] / median(volume[t-20:t])`` using previous bars only."""
    volume = np.asarray(volume, dtype=float)
    n = volume.shape[0]
    out = np.full(n, np.nan, dtype=float)
    for index in range(20, n):
        median = np.median(volume[index - 20 : index])
        if median != 0 and not np.isnan(median):
            out[index] = volume[index] / median
    return out