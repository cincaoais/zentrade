"""us_bars.us_candles: US daily bars -> wyk Candles with calendar-correct dates."""

from datetime import date

import numpy as np
import pandas as pd

from app.research.wyckoff.base import Candles
from app.research.wyckoff.us_bars import us_candles


def _frame(dates, *, flat=()):
    """A plausible daily frame; indices in `flat` are zero-range bars (high == low)."""
    n = len(dates)
    close = 100.0 + np.arange(n, dtype="float64")
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    for i in flat:
        high[i] = low[i] = close[i]
    return pd.DataFrame(
        {
            "date": list(dates),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        }
    )


def test_zero_range_bar_keeps_dates():
    dates = [date(2024, 1, 2) + pd.Timedelta(days=i) for i in range(30)]
    df = _frame(dates, flat=(10,))
    candles, kept_dates = us_candles(df)
    assert isinstance(candles, Candles)
    assert len(candles) == 29
    assert len(kept_dates) == 29
    assert all(isinstance(d, date) for d in kept_dates)
    assert dates[10] not in kept_dates
    assert kept_dates[10] == dates[11]


def test_post_gap_only_for_halts():
    # Thursday -> Friday -> Monday (normal weekend).
    dates = [date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)]
    candles, _ = us_candles(_frame(dates))
    assert candles.post_gap.tolist() == [False, False, False]

    # Friday -> Tuesday (three-day weekend, Monday holiday) is still normal.
    dates = [date(2024, 5, 23), date(2024, 5, 24), date(2024, 5, 28)]
    candles, _ = us_candles(_frame(dates))
    assert candles.post_gap.tolist() == [False, False, False]

    # A nine-calendar-day interval is a halt.
    dates = [date(2024, 6, 3), date(2024, 6, 12)]
    candles, _ = us_candles(_frame(dates))
    assert candles.post_gap.tolist() == [False, True]


def test_rv_uses_real_volume():
    n = 40
    dates = [date(2024, 1, 2) + pd.Timedelta(days=i) for i in range(n)]
    df = _frame(dates)
    vol = np.array([100.0 + 7.0 * i + (i % 5) for i in range(n)], dtype="float64")
    df["volume"] = vol
    candles, _ = us_candles(df)
    expected = vol[25] / np.median(vol[5:25])
    assert candles.rv[25] == expected


def test_calendar_fields_and_feeds():
    dates = [date(2024, 1, 2) + pd.Timedelta(days=i) for i in range(30)]
    candles, _ = us_candles(_frame(dates))
    assert not candles.holiday.any()
    assert not candles.holiday_lowvol.any()
    assert not candles.news.any()
    assert np.isnan(candles.v2).all()
    assert np.isnan(candles.rv2).all()
    assert np.isnan(candles.ae2).all()