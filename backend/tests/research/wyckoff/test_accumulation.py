"""Tests for wyk.detectors.accumulation (docs/01 §5.2-5.3; notes.md d.5-d.8, d.19).

Synthetic shapes only (no market data, no outcome statistics). Each detector gets (a) a shape where the event is
found where intended, (b) negative cases, and (c) truncation tests: events computed on k.head(cut), with the
ranges recomputed on the head, equal the full-series events with known_at <= cut-1, field by field and data dict
included (so nothing in Event.data depends on bars after known_at).
"""

import math
import re

import numpy as np

from app.research.wyckoff.accumulation import (accumulation_events, lps_events, range_key, sos_events, spring_tests,
                                        springs)
from app.research.wyckoff.base import Event, candles_from_arrays
from app.research.wyckoff.quality import pivot_index, qualification_map, qualified_at
from app.research.wyckoff.ranges import TradingRange, event_ranges, generic_ranges
from .shapes import accumulation

VOL_KEYS = ("rv", "rv2", "ae", "ae2", "vol_ok", "vol_ok2", "holiday")


# ---------------------------------------------------------------- helper shapes

def _bars(path, vol, seed):
    hw = np.random.default_rng([seed, 1]).uniform(0.12, 0.36, len(path))
    lw = np.random.default_rng([seed, 2]).uniform(0.12, 0.36, len(path))
    c = np.asarray(path, dtype=float)
    o = np.r_[c[0], c[:-1]]
    return o, np.maximum(o, c) + hw, np.minimum(o, c) - lw, c, np.asarray(vol, dtype=float)


def _cycle(seed=0, spring=True, spring_volume=60.0, spring_depth=1.5, bars_below=0, test=True, test_volume=50.0,
           sos=True, lps_level=14.7, lps_volume=60.0, holiday_at=None):
    """Full accumulation cycle: decline -> SC -> AR -> ST -> Phase B -> (spring: two bars down to TR_lo, a low-volume
    break, `bars_below` extra bars outside, recovery) -> (test: two bars inside, a quiet dip near TR_lo, a close above
    the dip's high) -> SOS (three wide bars on volume) -> pullback to sc + lps_level (three small-step bars) ->
    trigger -> markup. TR_lo ~ sc - 0.2, TR_hi ~ sc + 13.5 (sc = SC close). Phase B swings sc + 0.6 .. sc + 12.6
    (two touches of each bound), so the range passes S0-1 (qualified_at ~150-152 < the break at 175), which the
    gate S0-3 fold requires before any spring sequence. Returns (candles, marks, sc)."""
    rng = np.random.default_rng(seed)
    path, vol, m = [], [], {}
    p = 200.0
    for _ in range(120):
        p -= 0.6 + rng.normal(0, 0.3)
        path.append(p); vol.append(100 + rng.normal(0, 10))
    m["sc"] = len(path)
    p -= 6.0
    path.append(p); vol.append(400.0)
    sc = p
    for _ in range(6):
        p += 2.2
        path.append(p); vol.append(160.0)
    for _ in range(6):
        p -= 2.0
        path.append(p); vol.append(70.0)
    for j in range(40):
        path.append(sc + 6.6 + 6.0 * np.sin(j / 3.0) + rng.normal(0, 0.3)); vol.append(90 + rng.normal(0, 8))
    if spring:
        path += [sc + 3, sc + 1]; vol += [85.0, 85.0]
        m["spring"] = len(path)
        path.append(sc - spring_depth); vol.append(spring_volume)
        for _ in range(bars_below):
            path.append(sc - spring_depth - 0.3); vol.append(spring_volume)
        m["recovery"] = len(path)
        path.append(sc + 4); vol.append(110.0)
        path += [sc + 5, sc + 5.5]; vol += [85.0, 85.0]
        if test:
            path.append(sc + 3); vol.append(80.0)
            m["test"] = len(path)
            path.append(sc + 0.5); vol.append(test_volume)
            m["test_trigger"] = len(path)
            path.append(sc + 5); vol.append(100.0)
    for _ in range(5):
        path.append(sc + 6 + rng.normal(0, 0.3)); vol.append(85.0)
    m["sos"] = len(path)
    p = path[-1]
    for _ in range(3):
        p += 5.5 if sos else 1.0
        path.append(p); vol.append(260.0)
    start, target = p, sc + lps_level + 0.3
    for q in range(1, 4):                              # pullback towards TR_hi
        path.append(start + (target - start) * q / 3); vol.append(lps_volume)
    m["lps_small"] = len(path)
    for d in (-0.1, 0.2, 0.0):                         # three small-step bars
        path.append(sc + lps_level + d); vol.append(lps_volume)
    m["lps_trigger"] = len(path)
    path.append(sc + lps_level + 1.8); vol.append(150.0)
    p = path[-1]
    for _ in range(30):
        p += 1.0 + rng.normal(0, 0.3)
        path.append(p); vol.append(120.0)
    o, h, l, c, v = _bars(path, vol, seed)
    hol = np.zeros(len(c), bool)
    if holiday_at is not None:
        hol[m[holiday_at]] = True
    return candles_from_arrays(o, h, l, c, v, v2=v * 0.6, holiday=hol), m, sc


def _walk(seed, n=1500):
    """Random walk with lognormal volume on two noisy feeds and ~3% holiday bars."""
    rng = np.random.default_rng(seed)
    c = 1000 + np.cumsum(rng.normal(0, 1.0, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.1, 1.0, n)
    l = np.minimum(o, c) - rng.uniform(0.1, 1.0, n)
    v = 100 * np.exp(rng.normal(0, 0.5, n))
    return candles_from_arrays(o, h, l, c, v, v2=v * np.exp(rng.normal(0, 0.3, n)), holiday=rng.random(n) < 0.03)


def _box(extra_o=(), extra_h=(), extra_l=(), extra_c=(), extra_v=(), n=80, seed=0):
    """Flat oscillation around 100 (closes ~99.3-100.7, ATR ~0.9), then hand-written bars."""
    rng = np.random.default_rng(seed)
    c = 100 + 0.6 * np.sin(np.arange(n) / 2.0) + rng.normal(0, 0.1, n)
    o = np.r_[c[0], c[:-1]]
    h, l = np.maximum(o, c) + 0.3, np.minimum(o, c) - 0.3
    v = 100 + rng.normal(0, 5, n)
    cat = lambda a, b: np.r_[a, np.asarray(b, dtype=float)]
    return candles_from_arrays(cat(o, extra_o), cat(h, extra_h), cat(l, extra_l), cat(c, extra_c),
                               cat(v, extra_v), v2=cat(v, extra_v))


def _ranges(k):
    return event_ranges(k, "accumulation")[1] + generic_ranges(k)


def _acc_range(k):
    rs = event_ranges(k, "accumulation")[1]
    assert rs and rs[0].kind == "accumulation"
    return rs


def _as_qualified(rs):
    """S0-1 map that treats every range as qualified from its known_at (q = known_at - 1, so the scan starts where
    it did before the gate S0-3 fold). Only for unit tests of the recovery / volume / SOS mechanics on shapes that
    are not S0-1 ranges (tests/shapes.accumulation has one pivot per bound; _box is 2.7 ATR high). Never used in
    a truncation test: those run the default, computed map."""
    return {range_key(r): r.known_at - 1 for r in rs}


def _sp_kw(rs, **kw):
    return {"spring_kw": {"qual": _as_qualified(rs), **kw}}


# generic range on _box for spring tests: TR_lo 98.8 as before; TR_hi 102.0 (3.6 ATR at bar 81) so the S0-3 SPR-6
# height check at the break bar passes (101.2 is 2.7 ATR). SOS tests keep TR_hi 101.2 (no height check there).
GBOX = TradingRange("generic", 20, 60, 98.8, 102.0)


def _of(events, kind, range_kind=None):
    return [e for e in events if e.kind == kind and (range_kind is None or e.data["range_kind"] == range_kind)]


# ---------------------------------------------------------------- comparison helpers

def _norm(x):
    if isinstance(x, float):
        return "nan" if math.isnan(x) else round(x, 6)
    return x


def _key(e: Event):
    return (e.kind, e.idx, e.known_at, e.direction, None if e.price is None else round(e.price, 6),
            tuple(sorted((k, _norm(v)) for k, v in e.data.items())))


def _check_truncation(k, cuts, ranges_fn=_ranges, **kw):
    full = accumulation_events(k, ranges_fn(k), **kw)
    n_cuts = 0
    for cut in cuts:
        kh = k.head(cut)
        part = accumulation_events(kh, ranges_fn(kh), **kw)
        expect = [e for e in full if e.known_at <= cut - 1]
        assert [_key(e) for e in part] == [_key(e) for e in expect], cut
        n_cuts += 1
    assert n_cuts >= 20
    return full


# ---------------------------------------------------------------- springs (d.5)

def test_spring_found_on_stock_shape():
    for seed in range(4):
        k, m = accumulation(seed=seed)
        rs = _acc_range(k)
        # the stock shape's range never passes S0-1 (one pivot per bound), so by default it has no spring (S0-3 fold)
        assert qualification_map(k, rs)[range_key(rs[0])] is None and springs(k, rs) == []
        sp = _of(springs(k, rs, qual=_as_qualified(rs)), "spring")
        assert len(sp) == 1, seed
        e = sp[0]
        assert e.data["break_idx"] == m["spring"] and e.known_at == m["recovery"]
        assert e.idx in (m["spring"], m["recovery"]) and e.price == k.l[e.idx]
        assert e.data["range_kind"] == "accumulation" and e.data["phase_b_bars"] >= 10
        assert e.data["vol_basis"] == "rv_ratio_to_sc" and e.data["vol_ratio"] <= 0.7 and e.data["vol_ok"]
        assert e.data["vol_ok2"] and not e.data["high_volume_break"] and e.data["clv"] >= 0.5
        assert 0 < e.data["penetration_atr"] <= 1.5 and e.data["bars_outside"] == 1
        assert all(key in e.data for key in VOL_KEYS + ("vol_ratio2", "ref_rv", "ref_rv2", "rv_break_max"))


def test_no_spring_without_a_break():
    for seed in range(3):
        k, m = accumulation(seed=seed, spring=False)
        rs = _acc_range(k)
        assert not [e for e in springs(k, rs, qual=_as_qualified(rs)) if e.kind in ("spring", "spring_failed")]


def test_b_min_blocks_an_early_spring():
    k, m = accumulation(seed=0)
    rs = _acc_range(k)
    q = _as_qualified(rs)
    assert springs(k, rs, b_min=10, qual=q)
    assert not springs(k, rs, b_min=m["spring"] - rs[0].known_at + 1, qual=q)


def test_high_volume_break_is_flagged_and_gated():
    k, m = accumulation(seed=1, spring_volume=350.0)
    rs = _acc_range(k)
    q = _as_qualified(rs)
    sp = _of(springs(k, rs, qual=q), "spring")
    assert len(sp) == 1
    d = sp[0].data
    assert d["high_volume_break"] and d["high_volume_break2"] and not d["vol_ok"] and d["vol_ratio"] > 0.7
    assert not _of(springs(k, rs, volume_gate=True, qual=q), "spring")


def test_no_recovery_within_j_is_spring_failed():
    k, m, sc = _cycle(seed=0, bars_below=3, test=False)
    rs = _acc_range(k)
    ev = springs(k, rs)
    b = m["spring"]
    failed = [e for e in ev if e.kind == "spring_failed" and e.data["break_idx"] == b]
    assert len(failed) == 1
    f = failed[0]
    assert f.known_at == b + 2 and f.direction == -1 and f.data["fail_reason"] == "no_recovery"
    assert f.data["bars_outside"] == 3
    assert not [e for e in ev if e.kind == "spring" and e.data["break_idx"] == b]
    # the recovery window is sweepable: J = 5 accepts the slow recovery
    late = [e for e in springs(k, rs, j=5) if e.kind == "spring" and e.data["break_idx"] == b]
    assert len(late) == 1 and late[0].known_at == m["recovery"] and late[0].data["bars_outside"] == 4


def test_too_deep_break_is_spring_failed():
    k, m, sc = _cycle(seed=0, spring_depth=6.0, test=False)
    rs = _acc_range(k)
    ev = [e for e in springs(k, rs) if e.data["break_idx"] == m["spring"]]
    assert len(ev) == 1 and ev[0].kind == "spring_failed" and ev[0].data["fail_reason"] == "too_deep"
    assert ev[0].known_at == m["spring"] and ev[0].data["penetration_atr"] > 1.5


def test_holiday_break_bar_never_satisfies_low_volume():
    k, m, sc = _cycle(seed=0, holiday_at="spring")
    sp = [e for e in springs(k, _acc_range(k)) if e.kind == "spring" and e.data["break_idx"] == m["spring"]]
    assert len(sp) == 1
    d = sp[0].data
    assert d["holiday"] and not d["vol_ok"] and not d["vol_ok2"]
    assert d["vol_ratio"] <= 0.7                         # the ratio itself would pass; the holiday vetoes it


def test_generic_range_spring_uses_rv_break():
    # box, then: a bar down to TR_lo, a quiet break below it, a recovery closing high
    k = _box(extra_o=[99.6, 99.0, 98.4], extra_h=[99.7, 99.1, 99.6], extra_l=[98.95, 98.3, 98.35],
             extra_c=[99.0, 98.4, 99.5], extra_v=[100, 50, 100])
    r = GBOX
    q = _as_qualified([r])
    sp = springs(k, [r], qual=q)
    assert [(e.kind, e.idx, e.known_at) for e in sp] == [("spring", 81, 82)]
    d = sp[0].data
    assert d["vol_basis"] == "rv_break" and math.isnan(d["vol_ratio"]) and d["vol_ok"]
    assert d["rv_break_max"] <= 0.7 and d["bear_bar_idx"] == 81
    assert abs(d["height_atr"] - 3.2 / k.atrp[81]) < 1e-12
    # the same break on climax volume: still reported, flagged, and removed by the gate
    k2 = _box(extra_o=[99.6, 99.0, 98.4], extra_h=[99.7, 99.1, 99.6], extra_l=[98.95, 98.3, 98.35],
              extra_c=[99.0, 98.4, 99.5], extra_v=[100, 200, 100])
    d2 = springs(k2, [r], qual=q)[0].data
    assert d2["high_volume_break"] and not d2["vol_ok"]
    assert springs(k2, [r], volume_gate=True, qual=q) == []


def test_distribution_ranges_are_ignored():
    k, m = accumulation(seed=0)
    r = _acc_range(k)[0]
    d = TradingRange("distribution", r.start_idx, r.known_at, r.lo, r.hi, ref_rv=r.ref_rv, ref_rv2=r.ref_rv2)
    assert accumulation_events(k, [d]) == []


# ---------------------------------------------------------------- test of spring and entries (d.6, d.19)

def test_spring_test_and_entries_on_full_cycle():
    for seed in range(3):
        k, m, sc = _cycle(seed=seed)
        ev = accumulation_events(k, _acc_range(k))
        sp = _of(ev, "spring")
        assert len(sp) == 1 and sp[0].data["break_idx"] == m["spring"]
        agg = _of(ev, "entry_aggressive")
        # 反包 on the recovery bar, or on the next bar when the recovery bar itself made the spring low
        t_agg = m["recovery"] + (sp[0].idx == m["recovery"])
        assert len(agg) == 1 and agg[0].known_at == agg[0].idx == t_agg and agg[0].idx > sp[0].idx
        assert agg[0].data["trigger_level"] == k.h[m["spring"]] and agg[0].price == k.c[t_agg]
        assert agg[0].data["stop_ref"] < sp[0].price and agg[0].data["target"] == sp[0].data["range_hi"]
        st = _of(ev, "spring_test")
        assert len(st) == 1, seed
        t = st[0]
        assert t.idx == m["test"] and t.known_at == m["test_trigger"] and t.price == k.l[m["test"]]
        assert t.data["spring_idx"] == sp[0].idx and t.data["rv_ratio_to_break"] < 1 and t.data["vol_ok"]
        assert t.data["trigger_level"] == k.h[m["test"]] and t.data["bars_inside_before_trigger"] >= 2
        steady = _of(ev, "entry_steady")
        assert len(steady) == 1 and steady[0].known_at == steady[0].idx == m["test_trigger"]
        assert steady[0].data["test_idx"] == m["test"]


def test_no_spring_test_without_a_dip_and_no_steady_entry_too_early():
    k, m, sc = _cycle(seed=0, test=False)
    ev = accumulation_events(k, _acc_range(k))
    assert _of(ev, "spring") and not _of(ev, "spring_test") and not _of(ev, "entry_steady")
    # dip right after the recovery: a test, but fewer than 2 closes back inside -> no steady entry
    k = _box(extra_o=[99.6, 99.0, 98.4, 99.5, 99.0], extra_h=[99.7, 99.1, 99.6, 99.6, 99.8],
             extra_l=[98.95, 98.3, 98.35, 98.7, 98.9], extra_c=[99.0, 98.4, 99.5, 99.0, 99.7],
             extra_v=[100, 50, 100, 40, 90])
    ev = spring_tests(k, springs(k, [GBOX], qual=_as_qualified([GBOX])))
    st = _of(ev, "spring_test")
    assert [(e.idx, e.known_at) for e in st] == [(83, 84)]
    assert st[0].data["bars_inside_before_trigger"] == 1 and not _of(ev, "entry_steady")


def test_spring_test_volume_gate():
    k, m, sc = _cycle(seed=0, test_volume=200.0)
    ev = accumulation_events(k, _acc_range(k))
    st = _of(ev, "spring_test")
    assert len(st) == 1 and not st[0].data["vol_ok"] and st[0].data["rv_ratio_to_break"] > 1
    assert not _of(accumulation_events(k, _acc_range(k), volume_gate=True), "spring_test")


# ---------------------------------------------------------------- SOS (d.7)

def test_sos_after_spring_on_shapes():
    for seed in range(3):
        k, m = accumulation(seed=seed)
        rs = _acc_range(k)
        ev = accumulation_events(k, rs, **_sp_kw(rs))
        sos = _of(ev, "sos")
        assert len(sos) == 1
        d = sos[0].data
        assert m["sos"] <= sos[0].idx <= m["sos"] + 2 and sos[0].known_at == sos[0].idx
        assert d["sos_type"] == "after_spring" and d["spring_idx"] == _of(ev, "spring")[0].idx
        assert sos[0].price > d["range_hi"] and d["body_atr"] >= 0.8 and d["clv"] >= 0.7
        assert d["rv_agg"] >= 1.5 and d["vol_ok"] and d["vol_ok2"]
        assert all(key in d for key in VOL_KEYS + ("rv_agg", "rv2_agg", "ae_agg", "ae2_agg"))
        assert not _of(ev, "sos_failed")


def test_sos_without_spring_is_the_no_spring_type():
    k, m = accumulation(seed=0, spring=False)
    sos = _of(accumulation_events(k, _acc_range(k)), "sos")
    assert len(sos) == 1 and sos[0].data["sos_type"] == "no_spring" and sos[0].data["spring_idx"] == -1


def test_sos_bar_quality_aggregate_volume_and_b_min():
    # 80 box bars (TR 98.8-101.2), then: weak close above TR_hi (small body, long upper wick), back inside,
    # three +0.5 bars ending above TR_hi (a 2-bar aggregate), then two closes back inside (failure)
    o = [100.8, 101.4, 100.2, 100.7, 101.2, 100.9]
    h = [102.6, 101.5, 100.8, 101.3, 101.8, 101.0]
    l = [100.7, 100.4, 100.1, 100.6, 101.1, 100.3]
    c = [101.4, 100.5, 100.7, 101.2, 101.7, 100.4]
    v = [100, 100, 300, 300, 300, 100]
    k = _box(o, h, l, c, v)
    r = TradingRange("generic", 20, 60, 98.8, 101.2)
    ev = sos_events(k, [r], [])
    sos = _of(ev, "sos")
    assert [e.idx for e in sos] == [84]                  # 80 (weak) and 83 (close == TR_hi) are not SOS bars
    d = sos[0].data
    assert d["agg_bars"] == 2 and d["agg_start"] == 83 and d["sos_type"] == "no_spring"
    assert d["vol_ok"] and abs(d["rv_agg"] - np.mean(k.rv[83:85])) < 1e-12
    fail = _of(ev, "sos_failed")
    assert [(e.idx, e.known_at, e.direction) for e in fail] == [(85, 85, -1)] and fail[0].data["sos_idx"] == 84
    # low volume: still reported (vol_ok False), removed by the gate
    k_lo = _box(o, h, l, c, [100, 100, 100, 100, 100, 100])
    lo_sos = _of(sos_events(k_lo, [r], []), "sos")
    assert len(lo_sos) == 1 and not lo_sos[0].data["vol_ok"]
    assert not _of(sos_events(k_lo, [r], [], volume_gate=True), "sos")
    # Phase B too short for the no-spring type (accumulation range known at 79) ...
    r_acc = TradingRange("accumulation", 30, 79, 98.8, 101.2, ref_rv=3.0, ref_rv2=3.0)
    assert not _of(sos_events(k, [r_acc], []), "sos")
    # ... but a spring on that range makes the same bar an SOS after the spring
    fake = Event("spring", 80, 80, +1, 98.5, {"range_kind": "accumulation", "range_start": 30,
                                              "range_known_at": 79})
    s2 = _of(sos_events(k, [r_acc], [fake]), "sos")
    assert [e.idx for e in s2] == [84] and s2[0].data["sos_type"] == "after_spring"


def test_sos_failed_on_full_cycle_and_no_lps():
    k, m, sc = _cycle(seed=0, lps_level=11.0)            # pullback closes below TR_hi - 0.25 ATR
    ev = accumulation_events(k, _acc_range(k))
    sos, fail = _of(ev, "sos"), _of(ev, "sos_failed")
    assert len(sos) == 1 and len(fail) == 1
    assert fail[0].data["sos_idx"] == sos[0].idx and 0 < fail[0].known_at - sos[0].idx <= 10
    assert fail[0].price < sos[0].data["range_hi"]
    assert not _of(ev, "lps")


# ---------------------------------------------------------------- LPS (d.8)

def test_lps_found_on_full_cycle():
    for seed in range(3):
        k, m, sc = _cycle(seed=seed)
        ev = accumulation_events(k, _acc_range(k))
        sos, lps = _of(ev, "sos"), _of(ev, "lps")
        assert len(sos) == 1 and len(lps) == 1
        e = lps[0]
        d = e.data
        hi = d["range_hi"]
        assert e.known_at == m["lps_trigger"] and m["lps_small"] - 1 <= e.idx <= m["lps_trigger"]
        assert e.price == d["pullback_low"] == k.l[e.idx]
        assert d["sos_idx"] == sos[0].idx and d["trigger_close"] == k.c[m["lps_trigger"]]
        assert d["trigger_level"] == k.h[m["lps_small"]:m["lps_trigger"]].max()
        assert hi - 0.25 * k.atrp[e.known_at] <= e.price <= hi + 1.0 * k.atrp[e.known_at]
        assert d["rv_mean"] <= 0.8 and d["vol_ok"] and d["vol_ok2"]
        assert d["stop_ref"] < min(e.price, hi)
        assert all(key in d for key in VOL_KEYS + ("rv_mean", "rv2_mean", "ae_mean", "ae2_mean"))


def test_lps_negatives():
    # pullback on heavy volume: reported with vol_ok False, removed by the gate
    k, m, sc = _cycle(seed=0, lps_volume=250.0)
    lps = _of(accumulation_events(k, _acc_range(k)), "lps")
    assert len(lps) == 1 and not lps[0].data["vol_ok"] and lps[0].data["rv_mean"] > 0.8
    assert not _of(accumulation_events(k, _acc_range(k), volume_gate=True), "lps")
    # pullback that never comes back to TR_hi + 1 ATR: no LPS
    k, m, sc = _cycle(seed=0, lps_level=19.0)
    ev = accumulation_events(k, _acc_range(k))
    assert _of(ev, "sos") and not _of(ev, "lps")
    # expiry: with m_lps shorter than the time to the trigger there is no LPS
    k, m, sc = _cycle(seed=0)
    sos = _of(accumulation_events(k, _acc_range(k)), "sos")
    assert lps_events(k, sos) and not lps_events(k, sos, m_lps=m["lps_trigger"] - sos[0].known_at - 1)


# ---------------------------------------------------------------- general properties

def test_events_sorted_causal_and_deterministic():
    for k in (_cycle(seed=1)[0], accumulation(seed=2)[0], _walk(2)):
        rs = _ranges(k)
        a, b = accumulation_events(k, rs), accumulation_events(k, rs)
        assert [_key(e) for e in a] == [_key(e) for e in b]
        assert [(e.known_at, e.idx) for e in a] == sorted((e.known_at, e.idx) for e in a)
        assert all(0 <= e.idx <= e.known_at < len(k) for e in a)
        assert all({"rv", "rv2", "ae", "ae2", "holiday", "range_kind", "range_hi"} <= set(e.data) for e in a)


def test_volume_gate_only_removes_events():
    for k in (_cycle(seed=0, lps_volume=250.0)[0], _walk(2), _walk(4)):
        rs = _ranges(k)
        ungated = {_key(e)[:5] for e in accumulation_events(k, rs) if e.kind == "spring"}
        gated = accumulation_events(k, rs, volume_gate=True)
        g_sp = [e for e in gated if e.kind == "spring"]
        assert {_key(e)[:5] for e in g_sp} <= ungated
        assert all(e.data["vol_ok"] and not e.data["high_volume_break"] for e in g_sp)
        assert all(e.data["vol_ok"] for e in gated if e.kind in ("sos", "lps", "spring_test", "entry_steady"))


# ---------------------------------------------------------------- truncation

def test_truncation_on_synthetic_shapes():
    for seed in range(3):
        k, _ = accumulation(seed=seed)
        _check_truncation(k, range(100, len(k) + 1, 4))
    variants = [{}, {"bars_below": 3}, {"lps_level": 11.0}, {"spring": False}, {"lps_volume": 250.0}]
    seen = set()
    for i, kw in enumerate(variants):
        k, m, _ = _cycle(seed=i, **kw)
        for gate in (False, True):
            full = _check_truncation(k, range(100, len(k) + 1, 4), volume_gate=gate)
            seen |= {e.kind for e in full}
    assert seen == {"spring", "spring_failed", "spring_test", "entry_aggressive", "entry_steady", "sos",
                    "sos_failed", "lps"}


def test_truncation_on_random_walk_with_ranges():
    seen = {}
    for seed in range(4):
        k = _walk(seed)
        assert generic_ranges(k)
        for gate in (False, True):
            full = _check_truncation(k, range(60, 1501, 59), volume_gate=gate)
            for e in full:
                seen[e.kind] = seen.get(e.kind, 0) + 1
    assert {"spring", "spring_failed", "spring_test", "entry_aggressive", "entry_steady", "sos",
            "sos_failed"} <= set(seen)


# ---------------------------------------------------------------- independent review: adversarial checks
# Stronger than the truncation tests above in three ways: (1) the truncated Candles are REBUILT from the raw arrays
# (ATR, RV, AE recomputed on the truncated input; k.head only slices arrays computed on the full series); (2) cuts
# at every bar of a series that yields all eight kinds, and on the long series at every event/range boundary
# (idx, known_at and every *_idx field: cut there and one bar later) as well as a stride; (3) a different future
# after the cut must not change any event known before it, which exercises the full-length path where ranges.py
# has set end_idx.

ALL_KINDS = {"spring", "spring_failed", "spring_test", "entry_aggressive", "entry_steady", "sos", "sos_failed", "lps"}
_FLAG = re.compile(r"_ok2?$")                       # Stage-0 runner gate-flag pattern ('_ok' selected, '_ok2' other)


def _raw(k):
    return k.o, k.h, k.l, k.c, k.v, k.v2, k.holiday


def _rebuild(raw, cut=None):
    o, h, l, c, v, v2, hol = (np.asarray(x)[:cut] for x in raw)
    return candles_from_arrays(o, h, l, c, v, v2=v2, holiday=hol)


def _cycles(variants, seed=0):
    """_cycle shapes stitched end to end (each shifted to open at the previous close), ~3% random holidays."""
    parts, last = [], None
    for i, kw in enumerate(variants):
        k, _, _ = _cycle(seed=seed + i, **kw)
        shift = 0.0 if last is None else last - k.o[0]
        parts.append((k.o + shift, k.h + shift, k.l + shift, k.c + shift, k.v, k.v2, k.holiday))
        last = parts[-1][3][-1]
    o, h, l, c, v, v2, hol = (np.concatenate(x) for x in zip(*parts))
    return o, h, l, c, v, v2, hol | (np.random.default_rng(seed).random(len(c)) < 0.03)


CYCLES6 = ({}, {"bars_below": 3}, {"lps_level": 11.0}, {"spring": False}, {"lps_volume": 250.0},
           {"spring_volume": 200.0})


def _boundary_cuts(events, ranges, n, stride):
    pts = set(range(1, n + 1, stride)) | {n}
    for e in events:
        pts |= {e.idx, e.known_at}
        pts |= {v for key, v in e.data.items() if key.endswith("_idx") and isinstance(v, int) and v >= 0}
    for r in ranges:
        pts |= {r.start_idx, r.known_at} | ({r.end_idx} if r.end_idx is not None else set())
    # cut p keeps bars < p (an event known at p is excluded); cut p+1 is the first cut that includes bar p
    return sorted({p + d for p in pts for d in (0, 1)} & set(range(1, n + 1)))


def _truncation_raw(raw, cuts, **kw):
    k = _rebuild(raw)
    full = accumulation_events(k, _ranges(k), **kw)
    for cut in cuts:
        kh = _rebuild(raw, cut)
        part = accumulation_events(kh, _ranges(kh), **kw)
        assert [_key(e) for e in part] == [_key(e) for e in full if e.known_at <= cut - 1], cut
    return full


def test_review_truncation_every_bar_rebuilt_candles():
    seen = set()
    raw = _cycles(({}, {"bars_below": 3}, {"lps_level": 11.0}), seed=3)
    seen |= {e.kind for e in _truncation_raw(raw, range(1, len(raw[0]) + 1))}           # every bar, ~680 cuts
    k, _, _ = _cycle(seed=0)
    seen |= {e.kind for e in _truncation_raw(_raw(k), range(1, len(k) + 1), volume_gate=True)}
    assert seen == ALL_KINDS


def test_review_truncation_boundary_cuts_on_long_series():
    seen = set()
    for raw in (_raw(_walk(0)), _cycles(CYCLES6)):
        k = _rebuild(raw)
        rs = _ranges(k)
        for gate in (False, True):
            full = accumulation_events(k, rs, volume_gate=gate)
            cuts = _boundary_cuts(full, rs, len(k), stride=15)   # denser after the lead's SC-cluster fix (was 20)
            assert len(cuts) >= 200
            seen |= {e.kind for e in _truncation_raw(raw, cuts, volume_gate=gate)}
    assert seen == ALL_KINDS


def _perturb_after(raw, cut, seed):
    """Same bars before `cut`; from `cut` on a 10-bar drift (up or down) then a noisy walk with its own volume and
    holidays, so ranges end (or not) at different bars than in the original series."""
    o, h, l, c, v, v2, hol = (np.array(x) for x in raw)
    m = len(c) - cut
    rng = np.random.default_rng([seed, cut])
    drift = np.where(np.arange(m) < 10, 2.0 if (seed + cut) % 2 else -2.0, 0.0)
    c2 = c[cut - 1] + np.cumsum(drift + rng.normal(0, 1.0, m))
    o2 = np.r_[c[cut - 1], c2[:-1]]
    o[cut:], c[cut:] = o2, c2
    h[cut:] = np.maximum(o2, c2) + rng.uniform(0.1, 1.2, m)
    l[cut:] = np.minimum(o2, c2) - rng.uniform(0.1, 1.2, m)
    v[cut:] = 100 * np.exp(rng.normal(0, 0.8, m))
    v2[cut:] = v[cut:] * np.exp(rng.normal(0, 0.3, m))
    hol[cut:] = rng.random(m) < 0.1
    return o, h, l, c, v, v2, hol


def test_review_a_different_future_never_changes_known_events():
    for seed, raw in enumerate((_raw(_walk(1)), _cycles(CYCLES6, seed=5))):
        k = _rebuild(raw)
        rs = _ranges(k)
        ends = {(r.kind, r.start_idx, r.known_at): r.end_idx for r in rs}
        for gate in (False, True):
            full = accumulation_events(k, rs, volume_gate=gate)
            changed_later = changed_end = 0
            for cut in range(40, len(k) - 5, 23):
                kp = _rebuild(_perturb_after(raw, cut, seed))
                rp = _ranges(kp)
                alt = accumulation_events(kp, rp, volume_gate=gate)
                assert [_key(e) for e in alt if e.known_at <= cut - 1] == \
                       [_key(e) for e in full if e.known_at <= cut - 1], (seed, gate, cut)
                changed_later += [_key(e) for e in alt] != [_key(e) for e in full]
                changed_end += any(r.known_at <= cut - 1 and (r.kind, r.start_idx, r.known_at) in ends
                                   and ends[(r.kind, r.start_idx, r.known_at)] != r.end_idx for r in rp)
            assert changed_later > 0 and changed_end > 0          # the perturbation really reached the detector


def _selected_flags_pass(e):
    """Stage-0 rule: every selected-feed gate flag ('_ok', not '2_ok' / '_ok2') is True."""
    return all(v is True for key, v in e.data.items()
               if _FLAG.search(key) and not (key.endswith("2_ok") or key.endswith("_ok2")))


def test_review_spring_veto_is_a_gate_flag():
    # Regression: RV_SC ~4 and RV_b ~2.2 -> ratio ~0.55 passes vol_ok, but RV_b >= 1.5 trips the veto. Before the
    # fix the veto was not an '_ok' flag, so Stage 0's flag-based gated count kept a spring the gated run drops.
    k, m = accumulation(seed=1, spring_volume=200.0)
    # on every series: the ungated springs whose flags all pass are exactly the gated run's springs
    for kk in (k, _cycle(seed=0)[0], _cycle(seed=0, spring_volume=200.0)[0], _walk(0), _walk(2), _walk(4)):
        rs = _ranges(kk)
        flagged = [_key(e) for e in springs(kk, rs) if e.kind == "spring" and _selected_flags_pass(e)]
        assert flagged == [_key(e) for e in springs(kk, rs, volume_gate=True) if e.kind == "spring"]
    rs = _acc_range(k)
    q = _as_qualified(rs)
    sp = _of(springs(k, rs, qual=q), "spring")
    assert len(sp) == 1
    d = sp[0].data
    assert d["vol_ok"] and d["vol_ratio"] <= 0.7 and d["high_volume_break"] and d["break_vol_ok"] is False
    assert not _selected_flags_pass(sp[0]) and not _of(springs(k, rs, volume_gate=True, qual=q), "spring")
    # test-of-spring and entry events inherit the spring's whole gate
    kc, _, _ = _cycle(seed=0, spring_volume=200.0)
    ev = [e for e in accumulation_events(kc, _acc_range(kc)) if e.kind in ("spring_test", "entry_aggressive",
                                                                           "entry_steady")]
    assert ev and all(e.data["spring_vol_ok"] and e.data["spring_break_vol_ok"] is False for e in ev)
    assert not any(_selected_flags_pass(e) for e in ev)


def test_review_b_min_defaults_to_phase_a_length():
    # Regression (notes d.5 default B_min = max(10, Phase-A length); d.7 uses the same B_min for the no-spring SOS).
    # Accumulation range with a 40-bar Phase A (SC bar 20 -> ST confirmation 60); the break at 81 has 21 Phase-B bars.
    k = _box(extra_o=[99.6, 99.0, 98.4], extra_h=[99.7, 99.1, 99.6], extra_l=[98.95, 98.3, 98.35],
             extra_c=[99.0, 98.4, 99.5], extra_v=[100, 50, 100])
    r = TradingRange("accumulation", 20, 60, 98.8, 102.0, ref_rv=3.0, ref_rv2=3.0)
    q = _as_qualified([r])
    assert springs(k, [r], qual=q) == []
    flat = springs(k, [r], b_min_phase_a=False, qual=q)
    assert [(e.kind, e.idx, e.known_at, e.data["b_min"]) for e in flat] == [("spring", 81, 82, 10)]
    # the bars of test_sos_bar_quality_aggregate_volume_and_b_min: SOS at 84, 14 bars after a 40-bar Phase A
    k2 = _box([100.8, 101.4, 100.2, 100.7, 101.2, 100.9], [102.6, 101.5, 100.8, 101.3, 101.8, 101.0],
              [100.7, 100.4, 100.1, 100.6, 101.1, 100.3], [101.4, 100.5, 100.7, 101.2, 101.7, 100.4],
              [100, 100, 300, 300, 300, 100])
    r2 = TradingRange("accumulation", 30, 70, 98.8, 101.2, ref_rv=3.0, ref_rv2=3.0)
    assert not _of(sos_events(k2, [r2], []), "sos")
    s = _of(sos_events(k2, [r2], [], b_min_phase_a=False), "sos")
    assert [(e.idx, e.data["b_min"], e.data["sos_type"]) for e in s] == [(84, 10, "no_spring")]


def test_review_weak_close_is_not_labelled_no_recovery():
    # Gate S0-3 B1 (rewrites the old test, which locked the "any close within J" rule). After the break at 81, bar 82
    # is the FIRST close back above TR_lo (98.8), with CLV 0.38 < 0.5: d.5 feature 2 came, feature 3 failed, so the
    # sequence is 'weak_close' known at 82 (the old rule waited to b+J = 83 and called it 'weak_close' there).
    k = _box(extra_o=[99.6, 99.0, 98.4, 98.9], extra_h=[99.7, 99.1, 99.8, 99.9], extra_l=[98.95, 98.3, 98.35, 98.85],
             extra_c=[99.0, 98.4, 98.9, 99.0], extra_v=[100, 50, 100, 100])
    r = GBOX
    q = _as_qualified([r])
    ev = springs(k, [r], qual=q)
    assert [(e.kind, e.idx, e.known_at, e.data["fail_reason"], e.data["recovery_idx"]) for e in ev] ==         [("spring_failed", 81, 82, "weak_close", -1)]
    assert ev[0].data["bars_outside"] == 2 and abs(ev[0].data["clv"] - (98.9 - 98.35) / (99.8 - 98.35)) < 1e-12
    # a stay below TR_lo through b+J keeps 'no_recovery' (also asserted on the full cycle above)
    k2 = _box(extra_o=[99.6, 99.0, 98.4, 98.5], extra_h=[99.7, 99.1, 98.7, 98.7], extra_l=[98.95, 98.3, 98.35, 98.4],
              extra_c=[99.0, 98.4, 98.5, 98.6], extra_v=[100, 50, 100, 100])
    assert [(e.data["fail_reason"], e.known_at) for e in springs(k2, [r], qual=q)] == [("no_recovery", 83)]


def test_b1_a_weak_first_close_is_not_relabelled_by_a_later_strong_close():
    # Gate S0-3 B1: break at 81; 82 is the first close above TR_lo with CLV 0.38 (weak); 83 closes high (CLV 0.93)
    # inside J = 2. The old rule ("any close within J with CLV >= q") made 83 a spring with break_idx 81; d.5 says
    # the FIRST reclaim decides, so the sequence is 'weak_close' at 82 and 83 (no new break) starts nothing.
    ext = dict(extra_o=[99.6, 99.0, 98.4, 98.9], extra_h=[99.7, 99.1, 99.8, 99.6], extra_l=[98.95, 98.3, 98.35, 98.85],
               extra_c=[99.0, 98.4, 98.9, 99.55], extra_v=[100, 50, 100, 100])
    k = _box(**ext)
    q = _as_qualified([GBOX])
    assert (k.c[83] - k.l[83]) / (k.h[83] - k.l[83]) >= 0.5          # the later close alone would be a spring
    ev = springs(k, [GBOX], qual=q)
    assert [(e.kind, e.known_at, e.data["fail_reason"]) for e in ev] == [("spring_failed", 82, "weak_close")]
    # the same strong close as the FIRST reclaim is a spring known at that bar
    k2 = _box(extra_o=[99.6, 99.0, 98.4, 98.5], extra_h=[99.7, 99.1, 98.7, 99.6], extra_l=[98.95, 98.3, 98.35, 98.4],
              extra_c=[99.0, 98.4, 98.5, 99.55], extra_v=[100, 50, 100, 100])
    assert [(e.kind, e.known_at, e.data["break_idx"], e.data["recovery_idx"]) for e in springs(k2, [GBOX], qual=q)]         == [("spring", 83, 81, 83)]


def test_review_gate_flags_follow_the_stage0_contract():
    kw = _walk(0)
    k = _rebuild(_cycles(CYCLES6))
    ev = accumulation_events(k, _ranges(k)) + accumulation_events(kw, _ranges(kw))
    assert {e.kind for e in ev} == ALL_KINDS
    for e in ev:
        flags = [key for key in e.data if _FLAG.search(key)]
        assert all(type(e.data[f]) is bool for f in flags), e.kind
        assert not [key for key, v in e.data.items() if type(v) is bool and "ok" in key and key not in flags]
        if "vol_ok" in flags:
            assert "vol_ok2" in flags
        if e.kind == "spring":
            assert {"vol_ok", "vol_ok2", "break_vol_ok", "break_vol_ok2"} <= set(flags)


# ---------------------------------------------------------------- gate S0-3 T1 / T3: spring eligibility
# A sine box that passes S0-1 on its own (closes 98.0-102.0, wicks 0.2: TR_lo 97.8, TR_hi 102.2, ATR ~0.9, height
# ~4.8 ATR, pivot lows at 12 + 16m, highs at 4 + 16m). Hand bars are spliced in by index; the box phase follows the
# bar index, so troughs stay at 12 + 16m. Ranges are hand-made with end_idx None (active to the end of the data, as
# an unended range is), except the Phase-A ranges of the S0-2 tests.

LO, HI = 97.8, 102.2


def _qseries(n, bars=None, quiet=None):
    """n bars of the box; bars = {idx: (o, h, l, c)} replaces bars (o of the next box bar = previous close);
    quiet = (start, stop): bars in [start, stop) oscillate +-0.02 around 100 with 0.2 wicks (ATR falls to ~0.4)."""
    cs = np.round(100.0 + 2.0 * np.sin(2 * np.pi * np.arange(n) / 16), 4)
    bars = bars or {}
    o, h, l, c = [], [], [], []
    for i in range(n):
        if i in bars:
            oo, hh, ll, cc = bars[i]
        else:
            cc = cs[i] if quiet is None or not quiet[0] <= i < quiet[1] else round(100 + 0.02 * np.sin(i), 4)
            oo = c[-1] if c else cc
            hh, ll = max(oo, cc) + 0.2, min(oo, cc) - 0.2
        o.append(oo); h.append(hh); l.append(ll); c.append(cc)
    return candles_from_arrays(o, h, l, c, np.full(n, 100.0), v2=np.full(n, 60.0))


def _pc(n, i, quiet=None):
    """Close of bar i - 1 of the plain box (the open of a hand bar spliced in at i)."""
    return float(_qseries(n, quiet=quiet).c[i - 1])


def _spring_bar(o):
    """One-bar spring: low 0.3 under TR_lo, close back inside near the high (CLV > 0.9)."""
    return (o, o + 0.1, LO - 0.3, o + 0.05)


def _no_recovery(o, b):
    """Break at b and two more closes under TR_lo: 'no_recovery' known at b + 2 (J = 2)."""
    return {b: (o, o + 0.05, LO - 0.3, LO - 0.2), b + 1: (LO - 0.2, LO - 0.1, LO - 0.4, LO - 0.25),
            b + 2: (LO - 0.25, LO - 0.1, LO - 0.4, LO - 0.2)}


def _too_deep(o, b):
    return {b: (o, o + 0.05, LO - 2.0, LO - 1.5)}                  # ~2.4 ATR under TR_lo > P_max 1.5


def _weak(o, b):
    """Break at b; b+1 is the first close back above TR_lo, CLV 0.41 < 0.5: 'weak_close' known at b + 1."""
    return {b: (o, o + 0.05, LO - 0.3, LO - 0.2), b + 1: (LO - 0.2, LO + 1.6, LO - 0.25, LO + 0.5)}


def _g(start=0, known=40):
    return TradingRange("generic", start, known, LO, HI)


def _sig(ev):
    return [(e.kind, e.data["break_idx"], e.known_at, e.data["fail_reason"]) for e in ev]


def test_qbox_range_qualifies_at_known_at():
    k = _qseries(200)
    ph, pl = pivot_index(k, 3)
    assert float(k.l.min()) == LO and float(k.h.max()) == HI and float(k.c.min()) > LO
    assert qualified_at(k, _g(), ph, pl) == 40
    assert 3 < (HI - LO) / k.atrp[60] < 10
    assert springs(k, [_g()]) == []                                  # the box never breaks its own TR_lo


def test_t3_i_breakdown_ends_spring_eligibility():
    # (i) a 'no_recovery' or a 'too_deep' at 60 ends the range's spring scan: the clean spring at 92 is not emitted.
    # Without the breakdown the same bar 92 is a spring.
    o60, o92 = _pc(200, 60), _pc(200, 92)
    assert _sig(springs(_qseries(200, {92: _spring_bar(o92)}), [_g()])) == [("spring", 92, 92, None)]
    for fail, reason, known in ((_no_recovery, "no_recovery", 62), (_too_deep, "too_deep", 60)):
        k = _qseries(200, {**fail(o60, 60), 92: _spring_bar(o92)})
        r = _g()
        assert _sig(springs(k, [r])) == [("spring_failed", 60, known, reason)]
        assert r.end_idx is None                                     # the spring scan does not end the range


def test_t3_ii_weak_close_keeps_the_range_eligible():
    # (ii) a 'weak_close' closed back inside the range, so the scan goes on and the break at 92 is the spring.
    o60, o92 = _pc(200, 60), _pc(200, 92)
    k = _qseries(200, {**_weak(o60, 60), 92: _spring_bar(o92)})
    ev = springs(k, [_g()])
    assert _sig(ev) == [("spring_failed", 60, 61, "weak_close"), ("spring", 92, 92, None)]
    assert ev[1].data["seq_no"] == 1 and ev[1].data["n_prior_springs"] == 0


def test_t3_iii_one_spring_per_range():
    # (iii) after the first spring (60) the range emits nothing more: no spring at 92, no failure at 108.
    o = {i: _pc(200, i) for i in (60, 92, 108)}
    k = _qseries(200, {60: _spring_bar(o[60]), 92: _spring_bar(o[92]), **_too_deep(o[108], 108)})
    assert _sig(springs(k, [_g()])) == [("spring", 60, 60, None)]
    # each later break alone is an event on this range
    assert _sig(springs(_qseries(200, {92: _spring_bar(o[92])}), [_g()])) == [("spring", 92, 92, None)]
    assert _sig(springs(_qseries(200, _too_deep(o[108], 108)), [_g()])) == [("spring_failed", 108, 108, "too_deep")]
    # the gated run scans identically: a spring suppressed by the volume gate still ends the range's scan
    kk = _qseries(200, {60: _spring_bar(o[60]), 92: _spring_bar(o[92])})
    v = np.full(200, 100.0)
    v[60], v[92] = 400.0, 20.0                                       # 60: high-volume break; 92: quiet break
    kv = candles_from_arrays(kk.o, kk.h, kk.l, kk.c, v, v2=v * 0.6)
    ung = springs(kv, [_g()])
    assert _sig(ung) == [("spring", 60, 60, None)] and not ung[0].data["vol_ok"]
    assert springs(kv, [_g()], volume_gate=True) == []


def test_t3_iv_b_min_generic_is_10_in_springs_and_sos():
    # (iv) generic range known at 40 (qualified at 40): a break at 49 = known_at + 9 starts nothing, at 50 it does.
    for b, expect in ((49, []), (50, [("spring", 50, 50, None)])):
        k = _qseries(120, {b: _spring_bar(_pc(120, b))})
        assert _sig(springs(k, [_g()])) == expect, b
    k49 = _qseries(120, {49: _spring_bar(_pc(120, 49))})
    assert _sig(springs(k49, [_g()], b_min_generic=9)) == [("spring", 49, 49, None)]
    # sos_events: a no-spring SOS bar at known_at + 9 is not eligible, at + 10 it is (TRG-3)
    for t, expect in ((49, []), (50, [50])):
        o = _pc(120, t)
        k = _qseries(120, {t: (o, HI + 0.75, o - 0.05, HI + 0.7)})
        assert [e.idx for e in sos_events(k, [_g()], []) if e.kind == "sos"] == expect, t
        assert all(e.data["b_min"] == 10 for e in sos_events(k, [_g()], []) if e.kind == "sos")


def test_t3_v_height_out_of_3_10_atr_at_the_break_starts_nothing():
    # (v) qualified at 40 at ~4.8 ATR; bars 80-139 are quiet, so at the break bar 140 TR_hi - TR_lo is > 10 ATR.
    q = (80, 140)
    o = _pc(260, 140, quiet=q)
    brk = {140: (o, o + 0.1, LO - 0.3, o - 0.1)}
    k = _qseries(260, brk, quiet=q)
    assert qualification_map(k, [_g()])[range_key(_g())] == 40
    assert 10 < (HI - LO) / k.atrp[140] < 12
    assert springs(k, [_g()]) == []
    ev = springs(k, [_g()], w_max=12.0)                               # only the height check blocked it
    assert _sig(ev) == [("spring", 140, 140, None)] and 10 < ev[0].data["height_atr"] < 12
    # not consumed: a later break, once ATR has recovered, is a spring on the same range
    k2 = _qseries(260, {**brk, 220: _spring_bar(_pc(260, 220, quiet=q))}, quiet=q)
    assert 3 <= (HI - LO) / k2.atrp[220] <= 10
    assert _sig(springs(k2, [_g()])) == [("spring", 220, 220, None)]


def test_t3_vi_break_before_qualification_starts_nothing_and_does_not_consume():
    # (vi) range (2, 14): by 14 it has one confirmed pivot per bound; it qualifies later (q). A break at 28
    # (>= known_at + 10, < q) starts nothing; the break at 60 (> q) is the range's spring.
    r = _g(2, 14)
    k = _qseries(120, {28: _spring_bar(_pc(120, 28)), 60: _spring_bar(_pc(120, 60))})
    q = qualification_map(k, [r])[range_key(r)]
    assert 28 < q < 60
    ev = springs(k, [r])
    assert _sig(ev) == [("spring", 60, 60, None)] and ev[0].data["seq_no"] == 0
    # treated as qualified from known_at, the early break would have been the range's one spring
    assert _sig(springs(k, [r], qual=_as_qualified([r]))) == [("spring", 28, 28, None)]
    # s03-fix F3: the boundary of "a sequence may start only at t > qualified_at". A break AT the qualifying bar
    # (q == 28) starts nothing and does not consume the range (the break at 60 is still its spring); with q == 27
    # the break at 28 is strictly after q and is the spring.
    assert _sig(springs(k, [r], qual={range_key(r): 28})) == [("spring", 60, 60, None)]
    assert _sig(springs(k, [r], qual={range_key(r): 27})) == [("spring", 28, 28, None)]
    # a range that never qualifies emits nothing at all
    never = TradingRange("generic", 2, 14, LO, HI + 8.0)              # ~13 ATR: fails S0-1 height at every bar
    assert qualification_map(k, [never])[range_key(never)] is None and springs(k, [never]) == []


def test_t3_s0_2_fold_generic_break_under_active_phase_a_range():
    # S0-2 folded in: while an accumulation range is active (45..70), a break on the generic range starts nothing
    # and does not consume it; the break at 92 (Phase-A range ended at 70) is the generic range's spring.
    pa = TradingRange("accumulation", 30, 45, 90.0, 101.0, ref_rv=3.0, ref_rv2=3.0, end_idx=70, end_reason="breakdown")
    k = _qseries(150, {60: _spring_bar(_pc(150, 60)), 92: _spring_bar(_pc(150, 92))})
    assert qualification_map(k, [pa])[range_key(pa)] is None          # the Phase-A range itself never qualifies
    assert _sig(springs(k, [pa, _g()])) == [("spring", 92, 92, None)]
    assert _sig(springs(k, [_g()])) == [("spring", 60, 60, None)]
    # a distribution range is not the accumulation side: it does not block
    pdist = TradingRange("distribution", 30, 45, 90.0, 101.0, end_idx=70)
    assert _sig(springs(k, [pdist, _g()])) == [("spring", 60, 60, None)]


FOLD_N, FOLD_QUIET = 280, (140, 200)


def _fold_series():
    """One series with every T1 / T3 path, on several hand-made ranges. Bars: a spring bar at 28, weak_close 60-61,
    spring 76, too_deep 92, no_recovery 108-110, spring 124, quiet 140-199 with a break at 200 (> 10 ATR), springs
    at 236 and 252. An accumulation range is active 100..115 (it never qualifies)."""
    pc = lambda i: _pc(FOLD_N, i, quiet=FOLD_QUIET)
    bars = {28: _spring_bar(pc(28)), **_weak(pc(60), 60), 76: _spring_bar(pc(76)), **_too_deep(pc(92), 92),
            **_no_recovery(pc(108), 108), 124: _spring_bar(pc(124)),
            200: (pc(200), pc(200) + 0.1, LO - 0.3, pc(200) - 0.1), 236: _spring_bar(pc(236)),
            252: _spring_bar(pc(252))}
    k = _qseries(FOLD_N, bars, quiet=FOLD_QUIET)
    pa = TradingRange("accumulation", 40, 100, 90.0, 101.0, ref_rv=3.0, ref_rv2=3.0, end_idx=115,
                      end_reason="breakdown")
    rs = [_g(0, 40), _g(2, 14), _g(0, 62), _g(0, 80), _g(0, 96), _g(0, 160),
          TradingRange("generic", 3, 14, LO, HI + 8.0), pa]
    return k, rs


def _ranges_at(rs, cut):
    """The hand-made ranges as a causal builder returns them on bars < cut: unknown ranges dropped, an end at or
    after the cut not yet known."""
    out = []
    for r in rs:
        if r.known_at > cut - 1:
            continue
        end = r.end_idx if r.end_idx is not None and r.end_idx <= cut - 1 else None
        out.append(TradingRange(r.kind, r.start_idx, r.known_at, r.lo, r.hi, ref_rv=r.ref_rv, ref_rv2=r.ref_rv2,
                                end_idx=end, end_reason=r.end_reason if end is not None else None))
    return out


def test_t3_fold_series_paths_and_truncation_every_bar():
    k, rs = _fold_series()
    assert 28 < qualification_map(k, rs)[("generic", 2, 14)] < 60
    full = springs(k, rs)
    sig = {}
    for e in full:
        sig.setdefault((e.data["range_start"], e.data["range_known_at"], e.data["range_hi"]), []).append(
            (e.kind, e.data["break_idx"], e.data["fail_reason"]))
    assert sig == {
        (0, 40, HI): [("spring_failed", 60, "weak_close"), ("spring", 76, None)],    # (ii) then (iii)
        (2, 14, HI): [("spring_failed", 60, "weak_close"), ("spring", 76, None)],    # (vi): 28 is before q
        (0, 62, HI): [("spring", 76, None)],
        (0, 80, HI): [("spring_failed", 92, "too_deep")],                             # (i): 124 not emitted
        (0, 96, HI): [("spring", 124, None)],                                         # S0-2: 108 under the Phase-A
        (0, 160, HI): [("spring", 236, None)],                                        # (v): 200 > 10 ATR; (iii): 252
    }
    # the counterfactual inputs show that each fold changes this series' events
    assert springs(k, rs, w_max=1e9) != full
    assert springs(k, [r for r in rs if r.kind == "generic"]) != full
    assert springs(k, rs, qual=_as_qualified(rs)) != full
    # truncation at every bar (>= 280 cuts; rebuilt candles; ranges as the causal builder would see them)
    raw = _raw(k)
    gated = springs(k, rs, volume_gate=True)
    acc_full = accumulation_events(k, rs)
    n_cuts = 0
    for cut in range(1, len(k) + 1):
        kh = _rebuild(raw, cut)
        rh = _ranges_at(rs, cut)
        assert [_key(e) for e in springs(kh, rh)] == [_key(e) for e in full if e.known_at <= cut - 1], cut
        assert [_key(e) for e in springs(kh, rh, volume_gate=True)] == \
            [_key(e) for e in gated if e.known_at <= cut - 1], cut
        assert [_key(e) for e in accumulation_events(kh, rh)] == \
            [_key(e) for e in acc_full if e.known_at <= cut - 1], cut
        n_cuts += 1
    assert n_cuts >= 50


def test_t3_truncation_regime_walks_default_folds():
    # Regime walks with ranges recomputed on every head (event + generic), default parameters (folds on, the
    # qualification map computed inside): >= 50 cuts per series, at every event / range boundary and a stride.
    found = {}
    for seed in (1, 2, 5):
        rng = np.random.default_rng(seed)
        steps = np.concatenate([rng.normal(-0.6 if j % 3 == 0 else (0.6 if j % 3 == 1 else 0.0), 1.0, 100)
                                for j in range(15)])
        c = 1000 + np.cumsum(steps)
        o = np.r_[c[0], c[:-1]]
        h = np.maximum(o, c) + rng.uniform(0.1, 1.5, len(c))
        l = np.minimum(o, c) - rng.uniform(0.1, 1.5, len(c))
        v = rng.lognormal(4.5, 0.5, len(c))
        raw = (o, h, l, c, v, v * rng.lognormal(0, 0.2, len(c)), rng.random(len(c)) < 0.03)
        k = _rebuild(raw)
        rs = _ranges(k)
        full = accumulation_events(k, rs)
        for e in full:
            found[e.kind] = found.get(e.kind, 0) + 1
        cuts = _boundary_cuts(full, rs, len(k), stride=25)
        assert len(cuts) >= 50
        for cut in cuts:
            kh = _rebuild(raw, cut)
            part = accumulation_events(kh, _ranges(kh))
            assert [_key(e) for e in part] == [_key(e) for e in full if e.known_at <= cut - 1], (seed, cut)
    assert found.get("spring", 0) > 0 and found.get("spring_failed", 0) > 0


def test_t3_v_height_below_3_atr_at_the_break_starts_nothing():
    # SPR-6 lower bound: bars 60-79 swing 98.1 <-> 102.0 inside the range (wide bars, ATR up to ~3), so at the
    # break bar 80 TR_hi - TR_lo is < 3 ATR: nothing starts; with w_min = 0 the same bar is a spring.
    wide = {i: ((98.1, 102.05, 98.05, 102.0) if i % 2 == 0 else (102.0, 102.05, 98.05, 98.1)) for i in range(60, 80)}
    k = _qseries(120, {**wide, 80: _spring_bar(98.1)})
    assert qualification_map(k, [_g()])[range_key(_g())] == 40
    assert (HI - LO) / k.atrp[80] < 3
    assert springs(k, [_g()]) == []
    assert _sig(springs(k, [_g()], w_min=0.0)) == [("spring", 80, 80, None)]


def test_uk_only_holiday_vetoes_spring_and_test_low_volume():
    """S0-3 NSU-1: a holiday_lowvol bar that is not a CME holiday still vetoes the spring's low-volume flags; the
    event stores holiday False (CME-only) and holiday_lowvol True."""
    import dataclasses
    k, m, sc = _cycle(seed=0)
    base = [e for e in springs(k, _acc_range(k)) if e.kind == "spring" and e.data["break_idx"] == m["spring"]]
    assert len(base) == 1 and base[0].data["vol_ok"] and base[0].data["holiday_lowvol"] is False
    lv = np.zeros(len(k), bool)
    lv[m["spring"]] = True
    k2 = dataclasses.replace(k, holiday_lowvol=lv)
    sp = [e for e in springs(k2, _acc_range(k2)) if e.kind == "spring" and e.data["break_idx"] == m["spring"]]
    assert len(sp) == 1
    d = sp[0].data
    assert d["holiday"] is False and d["holiday_lowvol"] is True and not d["vol_ok"] and not d["vol_ok2"]
    assert d["vol_ratio"] == base[0].data["vol_ratio"]           # the reference (CME-only) is unchanged
