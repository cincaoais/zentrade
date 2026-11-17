# Ported from wyk/detectors/base.py @ fc142999 (user's own code).
"""Foundation for all Wyckoff detectors (docs/01 §5.1). Adapted from smc/detectors/base.py (commit 7bba4da):
same incremental known_at discipline, plus volume features and a Wilder ATR read at i-1.

Rules every detector follows:
- A detector reads candle i only through arrays indexed <= i, and only for decisions made at the close of i.
- Every output is an Event with idx (happens@, the candle the event is about) and known_at (the candle whose
  close makes it knowable, >= idx). Consumers may act on an event only from known_at.
- Normalisers use atrp[i] = ATR through i-1, so a bar's own range never scales its own test.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.research.wyckoff.volume import abnormal_effort, rv

ATR_PERIOD = 14
TICK = 0.01


@dataclass(frozen=True)
class Candles:
    """Closed candles of one timeframe as numpy arrays.

    t, tc       open / close time, int64 ms since epoch UTC
    o h l c     bid OHLC rounded to TICK
    rng         h - l
    atr         Wilder ATR(14) including candle i; atrp[i] = atr[i-1] (what a decision on candle i may use)
    v rv ae     volume, relative volume, abnormal effort for the selected feed (NaN where unavailable). Intraday ae
                is session-adjusted; D1 ae is not. Only the D1 path is ported for US daily bars.
    v2 rv2 ae2  the same for the other feed (robustness; may be all NaN)
    holiday     CME holiday or short session (D1: full-day minute count; intraday: the causal rule of S0-3 NDE-7,
                the source's holiday flags). Excluded from low-volume conditions and from every volume reference
                (RV / deseason / AE, rv_climax).
    news        a restricted USD release inside the bar
    post_gap    the candle follows an abnormal data gap (holiday / missing data), not a normal weekend
    holiday_lowvol  holiday OR an England & Wales bank holiday (S0-3 NSU-1). Read ONLY by LOW-volume conditions
                (no_supply, no_demand, spring vol_ok, spring_test, LPS, ST volume flags), never by a volume
                reference. Defaults to a copy of holiday when not given (synthetic candles, old positional calls).
    """

    tf: str
    feed: str
    t: np.ndarray
    tc: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    rng: np.ndarray
    atr: np.ndarray
    atrp: np.ndarray
    v: np.ndarray
    rv: np.ndarray
    ae: np.ndarray
    v2: np.ndarray
    rv2: np.ndarray
    ae2: np.ndarray
    holiday: np.ndarray
    news: np.ndarray
    post_gap: np.ndarray
    holiday_lowvol: np.ndarray | None = None

    def __post_init__(self):
        if self.holiday_lowvol is None:
            object.__setattr__(self, "holiday_lowvol", np.asarray(self.holiday, dtype=bool).copy())

    def __len__(self) -> int:
        return len(self.t)

    def head(self, n: int) -> "Candles":
        """The first n candles, with every derived array as it was then: used by truncation tests. Derived
        arrays are causal by construction, so slicing equals recomputing on truncated input; the truncation
        tests recompute from truncated input anyway to prove it."""
        kw = {k: (getattr(self, k)[:n] if isinstance(getattr(self, k), np.ndarray) else getattr(self, k))
              for k in self.__dataclass_fields__}
        return Candles(**kw)


@dataclass(frozen=True)
class Event:
    """kind: e.g. 'sc_candidate', 'spring'. idx = happens@, known_at = first candle close at which a causal rule
    labels it. direction: +1 bullish / upper side, -1 bearish / lower side. data: plain values only."""

    kind: str
    known_at: int
    idx: int
    direction: int
    price: float | None = None
    data: dict = field(default_factory=dict)


def wilder_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, post_gap: np.ndarray, n: int = ATR_PERIOD) -> np.ndarray:
    tr = h - l
    if len(h) > 1:
        prev_c = c[:-1]
        full = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev_c), np.abs(l[1:] - prev_c)])
        tr[1:] = np.where(post_gap[1:], h[1:] - l[1:], full)
    atr = np.full(len(tr), np.nan)
    if len(tr) >= n:
        atr[n - 1] = tr[:n].mean()
        for i in range(n, len(tr)):
            atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def _post_gap(t: np.ndarray, tc: np.ndarray, tf: str) -> np.ndarray:
    gap = np.zeros(len(t), dtype=bool)
    if len(t) < 2:
        return gap
    g = t[1:] - tc[:-1]
    day = 86_400_000
    # Normal: contiguous, daily break (< 3h), weekend (< 3.2 days). Anything longer is abnormal.
    gap[1:] = g > int(3.2 * day)
    if tf != "D1":
        dow = pd.to_datetime(t[1:], unit="ms", utc=True).dayofweek.to_numpy()
        weekend_ok = (dow == 0) | (dow == 6)
        gap[1:] |= (g > 3 * 3_600_000) & ~weekend_ok
    return gap


def candles_from_arrays(o, h, l, c, v=None, v2=None, tf: str = "D1", holiday=None, news=None,
                        start_ms: int = 1_700_000_000_000, holiday_lowvol=None) -> Candles:
    """Build Candles straight from arrays (tests and synthetic shapes). Volume features are computed on the
    D1 path (rv/ae without de-seasonalisation or session adjustment), whatever `tf` says.
    holiday_lowvol defaults to holiday; a given array is OR-ed with holiday."""
    o, h, l, c = (np.asarray(x, dtype="float64") for x in (o, h, l, c))
    n = len(o)
    period = 86_400_000 if tf == "D1" else 3_600_000
    t = start_ms + np.arange(n, dtype="int64") * period
    tc = t + period
    pg = np.zeros(n, dtype=bool)
    atr = wilder_atr(h, l, c, pg)
    atrp = np.r_[np.nan, atr[:-1]]
    rng = h - l
    hol = np.zeros(n, bool) if holiday is None else np.asarray(holiday, bool)
    nws = np.zeros(n, bool) if news is None else np.asarray(news, bool)
    lowvol = hol.copy() if holiday_lowvol is None else (np.asarray(holiday_lowvol, bool) | hol)

    def feats(vol):
        if vol is None:
            nan = np.full(n, np.nan)
            return nan, nan, nan
        s = pd.Series(np.asarray(vol, dtype="float64"))
        ex = pd.Series(hol)
        return (s.to_numpy(), rv(s, exclude=ex).to_numpy(),
                abnormal_effort(s, pd.Series(rng), pd.Series(atrp), exclude=ex).to_numpy())

    vv, rr, aa = feats(v)
    vv2, rr2, aa2 = feats(v2)
    return Candles(tf, "ftmo", t, tc, o, h, l, c, rng, atr, atrp, vv, rr, aa, vv2, rr2, aa2, hol, nws, pg, lowvol)


class Detector:
    """Incremental detector over one Candles series. Subclasses implement update(i) -> list[Event]."""

    def __init__(self, candles: Candles):
        self.k = candles

    def update(self, i: int) -> list[Event]:
        raise NotImplementedError

    def run(self) -> list[Event]:
        out: list[Event] = []
        for i in range(len(self.k)):
            out.extend(self.update(i))
        return out
