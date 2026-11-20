import numpy as np

from app.research.wyckoff.base import candles_from_arrays
from app.research.wyckoff.ranges import event_ranges, generic_ranges
from .shapes import accumulation, distribution


def _key(r):
    return (r.kind, r.start_idx, r.known_at, round(r.lo, 6), round(r.hi, 6))


def test_accumulation_chain_found_on_synthetic_shape():
    k, m = accumulation(seed=1)
    ev, rs = event_ranges(k, "accumulation")
    kinds = [e.kind for e in ev]
    assert "sc_candidate" in kinds and "ar" in kinds and "st" in kinds
    r = rs[0]
    assert abs(r.anchors["climax"] - m["sc"]) <= 1
    assert r.known_at > r.anchors["st"] >= r.anchors["ar"] > r.anchors["climax"]
    assert r.lo == k.l[r.anchors["extreme"]] and r.hi == k.h[r.anchors["ar"]]
    assert r.anchors["extreme"] - r.anchors["climax"] <= 2          # SC cluster
    assert r.lo == min(k.l[r.anchors["climax"]:r.anchors["extreme"] + 1])
    assert r.data["st_vol_ratio"] <= 0.7


def test_distribution_chain_found_on_mirrored_shape():
    k, m = distribution(seed=1)
    ev, rs = event_ranges(k, "distribution")
    assert {"bc_candidate", "ar_dist", "st_dist"} <= {e.kind for e in ev}
    r = rs[0]
    assert r.kind == "distribution" and r.hi >= k.h[r.anchors["climax"]] - 1e-9
    assert r.lo == k.l[r.anchors["ar"]]


def test_volume_gate_blocks_a_quiet_climax():
    k, m = accumulation(seed=2)
    v = k.v.copy()
    v[m["sc"]:m["sc"] + 7] = 100.0                 # neither the SC bar nor the AR bars are a volume climax
    k2 = candles_from_arrays(k.o, k.h, k.l, k.c, v)
    _, gated = event_ranges(k2, "accumulation", volume_gate=True)
    _, ungated = event_ranges(k2, "accumulation", volume_gate=False)
    assert not any(abs(r.anchors["climax"] - m["sc"]) <= 1 for r in gated)
    assert any(abs(r.anchors["climax"] - m["sc"]) <= 1 for r in ungated)


def _check_truncation(fn, k, cuts):
    _, full = fn(k) if isinstance(fn(k), tuple) else (None, fn(k))
    for cut in cuts:
        out = fn(k.head(cut))
        part = out[1] if isinstance(out, tuple) else out
        expect = [r for r in full if r.known_at <= cut - 1]
        assert [_key(r) for r in part] == [_key(r) for r in expect], cut
        for a, b in zip(part, expect):
            if b.end_idx is not None and b.end_idx <= cut - 1:
                assert a.end_idx == b.end_idx and a.end_reason == b.end_reason


def test_event_ranges_truncation():
    for seed in range(3):
        k, _ = accumulation(seed=seed)
        _check_truncation(lambda kk: event_ranges(kk, "accumulation"), k, range(60, len(k), 7))
        kd, _ = distribution(seed=seed)
        _check_truncation(lambda kk: event_ranges(kk, "distribution"), kd, range(60, len(kd), 7))


def test_generic_ranges_truncation_on_random_walk():
    rng = np.random.default_rng(5)
    c = 1000 + np.cumsum(rng.normal(0, 1.0, 1500))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.1, 1.0, 1500)
    l = np.minimum(o, c) - rng.uniform(0.1, 1.0, 1500)
    k = candles_from_arrays(o, h, l, c, rng.poisson(100, 1500))
    full = generic_ranges(k)
    assert len(full) > 0
    _check_truncation(generic_ranges, k, range(100, 1500, 97))


def test_generic_range_found_on_oscillation():
    rng = np.random.default_rng(7)
    trend = 1000 - np.arange(80) * 1.0
    box = 920 + 6 * np.sin(np.arange(120) / 1.6) + rng.normal(0, 0.4, 120)     # ~10-bar swings, flat
    c = np.r_[trend, box]
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.2, 0.8, len(c))
    l = np.minimum(o, c) - rng.uniform(0.2, 0.8, len(c))
    k = candles_from_arrays(o, h, l, c, rng.poisson(100, len(c)))
    rs = generic_ranges(k, window=40)
    assert any(r.known_at >= 80 and r.lo < 920 < r.hi and 3 <= r.data["height_atr"] <= 10 for r in rs)


# ---- regression tests for the lead's foundation fixes after the Stage 0 review ----

def test_ungated_run_extends_the_sc_cluster_instead_of_replacing_it():
    from app.research.wyckoff.ranges import event_ranges as er
    for seed in range(3):
        k, m = accumulation(seed=seed)
        _, gated = er(k, "accumulation", volume_gate=True)
        _, ungated = er(k, "accumulation", volume_gate=False)
        g = [r for r in gated if abs(r.anchors["climax"] - m["sc"]) <= 1]
        u = [r for r in ungated if abs(r.anchors["climax"] - m["sc"]) <= 1]
        assert g and u
        assert u[0].anchors["climax"] == g[0].anchors["climax"]
        assert u[0].ref_rv == g[0].ref_rv and u[0].ref_rv > 3      # anchored on the climax, not the quiet bar


def test_rv_climax_uses_relative_volume_and_needs_a_full_window():
    from app.research.wyckoff.ranges import rv_climax
    hol = np.zeros(80, bool)
    rv = np.full(80, 1.0)
    rv[70] = 1.5                       # largest RV of the window but below rv_min
    assert rv_climax(rv, hol, 70, 60, 2.0) is True          # max-of-window clause
    thin = np.full(80, np.nan)
    thin[65:71] = [1.0, 1.1, 0.9, 1.0, 1.0, 1.4]
    assert rv_climax(thin, hol, 70, 60, 2.0) is False       # only 6 valid values: max clause disabled
    assert rv_climax(thin, hol, 64, 60, 2.0) is None        # undefined RV
    hol2 = hol.copy(); hol2[70] = True
    assert rv_climax(rv, hol2, 70, 60, 2.0) is False        # a holiday is never a climax


# ---- S0-3 gate, B2 (T2): anchor order SC/BC cluster extreme < AR, max(extreme, t_ar) < ST ----

import dataclasses

import pytest


def _bars(rows):
    o, h, l, c, v = (np.asarray(x, float) for x in zip(*rows))
    return candles_from_arrays(o, h, l, c, v, v2=v * 0.6)


def _decline(n=120, p=200.0):
    rows = []
    for _ in range(n):                                   # steady decline of 1-point bars: a down-trend context
        rows.append((p, p + 0.2, p - 0.8, p - 0.6, 100.0))
        p -= 0.6
    return rows, p


def _cluster_shape():
    """SC at s, a lower low at s+1 (the cluster extreme) whose high tops the next bar, a retrace close at s+2 that
    would confirm an AR at s+1, and a 1-ATR bounce at s+3 off s+2's low. Before B2 this gave AR = extreme = s+1
    and ST = t_ar = s+2. The real AR rally and ST follow."""
    rows, p = _decline()
    s = len(rows)
    rows += [(p, p + 0.2, 121.5, 122.0, 400.0),          # SC: wide down bar on climax volume
             (122.0, 126.0, 120.5, 121.5, 300.0),        # s+1: lower low = cluster extreme, high 126
             (121.5, 123.5, 121.0, 123.0, 150.0),        # s+2: close <= 126 - 0.33 * 5.5
             (123.0, 123.6, 122.6, 123.2, 120.0)]        # s+3: close >= low(s+2) + 1 ATR
    for cl in (124.5, 126.0, 127.5, 129.0, 130.5):      # AR rally
        rows.append((rows[-1][3], cl + 0.3, rows[-1][3] - 0.3, cl, 140.0))
    for cl in (129.0, 127.5, 126.0, 124.5, 123.0, 121.8):  # decline to the ST on drying volume
        rows.append((rows[-1][3], rows[-1][3] + 0.3, cl - 0.4, cl, 60.0))
    for j in range(60):                                  # phase B
        cl = 125.0 + 2.5 * np.sin(j / 3.0)
        prev = rows[-1][3]
        rows.append((prev, max(prev, cl) + 0.3, min(prev, cl) - 0.3, cl, 90.0))
    return _bars(rows), s


def _st_shape():
    """No cluster. The AR is confirmed by the retrace close of bar t_ar whose own low lies in the ST zone, and the
    next bar bounces >= 1 ATR off it. Before B2 the ST was t_ar itself; now it must come after t_ar."""
    rows, p = _decline()
    s = len(rows)
    rows.append((p, p + 0.2, 121.0, 121.5, 400.0))      # SC
    for cl in (123.0, 124.5, 126.0):                    # AR rally, high 126.3
        rows.append((rows[-1][3], cl + 0.3, rows[-1][3] - 0.3, cl, 140.0))
    rows += [(126.0, 126.1, 121.6, 122.0, 60.0),         # retrace close confirms the AR; low in the ST zone
             (122.0, 123.6, 121.9, 123.5, 60.0)]         # bounce
    for j in range(60):
        cl = 124.0 + 1.5 * np.sin(j / 3.0)
        prev = rows[-1][3]
        rows.append((prev, max(prev, cl) + 0.3, min(prev, cl) - 0.3, cl, 90.0))
    return _bars(rows), s


def _mirror(k):
    return candles_from_arrays(400 - k.o, 400 - k.l, 400 - k.h, 400 - k.c, k.v, v2=k.v2)


def _t_ar(events, r):
    """AR confirmation bar of range r: the one AR event of its chain (known in [start_idx, known_at])."""
    kinds = ("ar", "ar_dist")
    t = [e.known_at for e in events if e.kind in kinds and e.idx == r.anchors["ar"]
         and r.start_idx <= e.known_at <= r.known_at]
    assert len(t) == 1, (r.anchors, t)
    return t[0]


def _anchor_violations(events, ranges):
    bad = []
    for r in ranges:
        a = r.anchors
        t_ar = _t_ar(events, r)
        if not (a["climax"] <= a["extreme"] < a["ar"] <= t_ar < a["st"] < r.known_at + 1 and a["st"] > a["extreme"]):
            bad.append((a, t_ar, r.known_at))
    return bad


@pytest.mark.parametrize("side", ["accumulation", "distribution"])
@pytest.mark.parametrize("gate", [False, True])
def test_b2_cluster_lower_low_at_s_plus_1(side, gate):
    k, s = _cluster_shape()
    if side == "distribution":
        k = _mirror(k)
    ev, rs = event_ranges(k, side, volume_gate=gate)
    assert len(rs) == 1
    r = rs[0]
    assert r.anchors["climax"] == s and r.anchors["extreme"] == s + 1
    assert r.anchors["ar"] > r.anchors["extreme"]
    assert r.anchors["ar"] == s + 8                                   # top of the real rally, not the cluster bar
    t_ar = _t_ar(ev, r)
    assert r.anchors["st"] > t_ar and r.anchors["st"] > r.anchors["extreme"]
    assert r.known_at >= r.anchors["st"]
    if side == "accumulation":
        assert r.lo == k.l[s + 1] and r.hi == k.h[r.anchors["ar"]]
    else:
        assert r.lo == k.l[r.anchors["ar"]] and r.hi >= k.h[s + 1]
    assert not _anchor_violations(ev, rs)


@pytest.mark.parametrize("side", ["accumulation", "distribution"])
def test_b2_st_comes_after_ar_confirmation(side):
    k, s = _st_shape()
    if side == "distribution":
        k = _mirror(k)
    ev, rs = event_ranges(k, side, volume_gate=False)
    assert len(rs) == 1
    r = rs[0]
    t_ar = _t_ar(ev, r)
    assert r.anchors["extreme"] == s and r.anchors["ar"] == s + 3 and t_ar == s + 4
    assert r.anchors["st"] == s + 5 > t_ar                            # was s + 4 (= t_ar) before B2


def _strip_end(r, cut):
    """The range as a run truncated at `cut` bars must report it: end fields only if the end is inside the cut."""
    if r.end_idx is not None and r.end_idx <= cut - 1:
        return r
    return dataclasses.replace(r, end_idx=None, end_reason=None)


def _check_event_ranges_truncation(k, side, gate, cuts):
    ev_full, rs_full = event_ranges(k, side, volume_gate=gate)
    for cut in cuts:
        ev, rs = event_ranges(k.head(cut), side, volume_gate=gate)
        exp_ev = [e for e in ev_full if e.known_at < cut]
        exp_rs = [_strip_end(r, cut) for r in rs_full if r.known_at < cut]
        assert repr(ev) == repr(exp_ev), (side, gate, cut)           # field by field (repr keeps NaN equal)
        assert repr(rs) == repr(exp_rs), (side, gate, cut)


def test_b2_event_ranges_truncation_synthetic():
    shapes = [_cluster_shape()[0], _st_shape()[0], accumulation(seed=0)[0], accumulation(seed=1)[0]]
    for k0 in shapes:
        for side, k in (("accumulation", k0), ("distribution", _mirror(k0))):
            cuts = list(range(60, len(k) + 1))
            assert len(cuts) >= 50
            for gate in (False, True):
                _check_event_ranges_truncation(k, side, gate, cuts)
