import numpy as np
import pandas as pd
import pytest

from app.research.wyckoff.base import candles_from_arrays, wilder_atr
from app.research.wyckoff.pivots import pivots, trend_context
from app.research.wyckoff.volume import abnormal_effort, deseason, rv


# ---------- causal features ----------

def test_rv_uses_only_previous_bars():
    v = pd.Series(np.arange(1, 41, dtype=float))
    r = rv(v, n=5)
    assert np.isnan(r.iloc[4])
    assert r.iloc[5] == pytest.approx(6 / 3.0)        # median of 1..5 = 3
    v2 = v.copy()
    v2.iloc[30:] = 999.0
    assert np.allclose(rv(v2, n=5).iloc[:30], r.iloc[:30], equal_nan=True)


def test_rv_skips_excluded_reference_bars():
    v = pd.Series([10.0] * 10 + [1.0] + [10.0] * 5)
    ex = pd.Series(False, index=v.index)
    ex.iloc[10] = True
    r = rv(v, n=5, exclude=ex)
    assert r.iloc[11] == pytest.approx(1.0)            # the holiday bar does not depress the reference


def test_deseason_and_abnormal_effort_are_causal():
    rng = np.random.default_rng(1)
    n = 600
    v = pd.Series(rng.lognormal(3, 0.5, n))
    slot = pd.Series(np.arange(n) % 6)
    r = pd.Series(rng.uniform(0.5, 2.0, n))
    a = pd.Series(np.ones(n))
    d_full, ae_full = deseason(v, slot), abnormal_effort(v, r, a)
    cut = 400
    d_cut = deseason(v.iloc[:cut], slot.iloc[:cut])
    ae_cut = abnormal_effort(v.iloc[:cut], r.iloc[:cut], a.iloc[:cut])
    assert np.allclose(d_full.iloc[:cut], d_cut, equal_nan=True)
    assert np.allclose(ae_full.iloc[:cut], ae_cut, equal_nan=True)


def test_wilder_atr():
    h = np.array([2.0] * 20)
    l = np.array([1.0] * 20)
    c = np.array([1.5] * 20)
    atr = wilder_atr(h, l, c, np.zeros(20, bool), n=14)
    assert np.isnan(atr[12]) and atr[13] == pytest.approx(1.0) and atr[19] == pytest.approx(1.0)


# ---------- pivots truncation ----------

def _candles(days: int = 120, seed: int = 3):
    rng = np.random.default_rng(seed)
    c = 2000 + np.cumsum(rng.normal(0, 3.0, days))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.5, 3.0, days)
    l = np.minimum(o, c) - rng.uniform(0.5, 3.0, days)
    v = rng.poisson(50.0, days).astype(float)
    return candles_from_arrays(o, h, l, c, v)


def test_pivots_known_at_and_truncation():
    k = _candles()
    full = pivots(k, lag=3)
    assert all(e.known_at == e.idx + 3 for e in full)
    for cut in (30, 55, 90):
        part = pivots(k.head(cut), lag=3)
        expect = [e for e in full if e.known_at <= cut - 1]
        assert [(e.kind, e.idx) for e in part] == [(e.kind, e.idx) for e in expect]


def test_trend_context_is_causal():
    k = _candles()
    d, u = trend_context(k)
    d2, u2 = trend_context(k.head(80))
    assert (d[:80] == d2).all() and (u[:80] == u2).all()
