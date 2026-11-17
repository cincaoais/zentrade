"""Distribution Phase C/D detectors (src/wyk/detectors/distribution.py): synthetic shapes where each event is found
where intended, negative cases, volume-gate and holiday behaviour, and truncation tests (events on k.head(cut),
with ranges recomputed on the head, equal the full-series events known by cut - 1, field by field)."""

import math

import numpy as np
import pytest

from app.research.wyckoff.base import candles_from_arrays
from app.research.wyckoff.distribution import (distribution_events, lpsy_events, sow_events, upthrusts,
                                         utad_confirmations)
from app.research.wyckoff.ranges import event_ranges, generic_ranges
from .shapes import accumulation, distribution


# ---------------------------------------------------------------- shapes

def _dist_shape(seed: int = 0, ut: bool = True, ut_vol: float = 60.0, hold: int = 0, x_high: float = 0.2,
                x_vol: float = 260.0, sow: str | None = "break", rally_vol: float = 60.0, rally_top: float = -14.9,
                holiday: tuple = (), hold_vol: float = 90.0):
    """Up-trend -> BC (huge volume) -> AR -> quiet ST -> Phase B -> UT (break above the BC high, back inside next
    bar, or after `hold` bars above) -> rebound to a lower high x with an effort-without-result bar near the BC high
    (LPSY_C) -> close below the low of the bar after x (UTAD confirmed) -> SOW through the AR low (ice) ->
    low-volume rally back to the ice (LPSY_D) -> markdown. Prices of the scripted bars are relative to the BC
    close. sow: 'break' (SOW_break), 'minor' (wide high-volume bar to just above the ice) or None; without a
    SOW_break the tail is flat (a markdown tail would itself close below the ice). hold_vol: volume of the `hold`
    bars above the range (review addition, so the UT high can sit on a louder bar than the break bar).
    Returns (candles, marks) with the intended bar indices."""
    rng = np.random.default_rng(seed)
    path, vol, marks, high_at = [], [], {}, {}
    p = 100.0
    for _ in range(120):                       # mark-up, normal volume
        p += 0.6 + rng.normal(0, 0.3)
        path.append(p); vol.append(100 + rng.normal(0, 10))
    marks["bc"] = len(path)
    p += 6.0
    path.append(p); vol.append(400.0)          # BC: wide bull bar, climax volume
    bc = p
    for _ in range(6):                         # AR: reaction
        p -= 2.2
        path.append(p); vol.append(160.0)
    for _ in range(6):                         # ST: rally back towards the BC on drying volume
        p += 2.0
        path.append(p); vol.append(70.0)
    for j in range(40):                        # Phase B
        p = bc - 6 + 4 * np.sin(j / 3.0) + rng.normal(0, 0.3)
        path.append(p); vol.append(90 + rng.normal(0, 8))

    def add(level, v, key=None, high=None):
        if key:
            marks[key] = len(path)
        if high is not None:
            high_at[len(path)] = bc + high
        path.append(bc + level); vol.append(v)

    add(-3.0, 90.0)
    if ut:
        add(1.5, ut_vol, "ut")
        for _ in range(hold):
            add(1.6, hold_vol)
        add(-4.0, 110.0, "fb")
    else:
        add(-2.5, 90.0); add(-4.0, 90.0)
    add(-4.5, 90.0); add(-4.2, 90.0)
    add(-2.6, 100.0)
    add(-2.4, x_vol, "x", high=x_high)         # lower high; long upper wick, tiny close change
    add(-3.8, 90.0); add(-5.3, 90.0); add(-6.8, 90.0, "x_trig")
    add(-8.3, 90.0); add(-9.8, 90.0)
    if sow == "break":
        add(-15.3, 260.0, "sow"); add(-20.8, 260.0)
        add(-19.0, rally_vol, "rally_start"); add(-17.6, rally_vol); add(-16.2, rally_vol)
        add(rally_top, rally_vol, "rally_top")
        add(-16.0, 80.0); add(-17.6, 80.0, "lpsy_trig")
    elif sow == "minor":
        add(-12.9, 260.0, "minor")
        add(-10.5, 90.0); add(-8.5, 90.0); add(-7.0, 90.0)
    else:
        add(-11.0, 90.0); add(-9.0, 90.0); add(-7.0, 90.0)
    for _ in range(30):
        step = -0.8 if sow == "break" else rng.normal(0, 0.2)
        path.append(path[-1] + step); vol.append(120.0 if sow == "break" else 90.0)
    c = np.asarray(path)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.2, 0.6, len(c)) * 0.6
    l = np.minimum(o, c) - rng.uniform(0.2, 0.6, len(c)) * 0.6
    for i, hv in high_at.items():
        h[i] = max(hv, h[i])
    if ut:
        f = marks["fb"]
        h[f] = max(o[f], c[f]) + 0.05          # the UT high is the break bar, not the fall-back bar
    hol = np.zeros(len(c), dtype=bool)
    for key in holiday:
        hol[marks[key]] = True
    v = np.asarray(vol)
    return candles_from_arrays(o, h, l, c, v, v2=v * 0.6, holiday=hol), marks


def _random_walk(seed: int, n: int = 1500):
    rng = np.random.default_rng(seed)
    c = 1000 + np.cumsum(rng.normal(0, 1.0, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.1, 1.0, n)
    l = np.minimum(o, c) - rng.uniform(0.1, 1.0, n)
    v = rng.lognormal(4.6, 0.45, n) * (1 + (h - l))
    hol = rng.random(n) < 0.02
    return candles_from_arrays(o, h, l, c, v, v2=v * rng.lognormal(0, 0.2, n), holiday=hol)


def _ranges(k):
    return generic_ranges(k) + event_ranges(k, "distribution")[1]


def _rw_ranges(k):
    return generic_ranges(k, window=40) + event_ranges(k, "distribution", volume_gate=False)[1]


def _by_kind(events, kind):
    return [e for e in events if e.kind == kind]


# ---------------------------------------------------------------- found where intended

def test_default_shape_ut_and_sow_found_where_intended():
    for seed in range(3):
        k, m = distribution(seed=seed)
        ev = distribution_events(k, _ranges(k))
        uts = _by_kind(ev, "ut")
        assert len(uts) == 1
        u = uts[0]
        assert (u.idx, u.known_at, u.direction) == (m["spring"], m["recovery"], -1)   # mirrored spring = UT
        assert u.data["range_kind"] == "distribution" and u.price == k.h[m["spring"]]
        assert u.data["lv"] and u.data["lv_rv"] and not u.data["hv"]                   # 60 vs ~90 volume
        assert u.data["penetration_atr"] > 0 and u.data["bars_outside"] == 2
        sows = _by_kind(ev, "sow")
        assert [(s.idx, s.known_at) for s in sows] == [(m["sos"] + 1, m["sos"] + 1)]   # mirrored SOS
        s = sows[0]
        assert s.data["agg_bars"] == 1 and s.data["vol_ok"] and s.data["clv"] <= 0.3
        assert s.price < s.data["ice"] == s.data["range_lo"]
        # negatives for free: the mirrored LPS rally stays > 2 ATR under the ice; no rally back to the BC high
        assert not _by_kind(ev, "lpsy_d") and not _by_kind(ev, "lpsy_c") and not _by_kind(ev, "ut_failed")


def test_full_chain_found_where_intended():
    k, m = _dist_shape()
    ev = distribution_events(k, _ranges(k))
    got = [(e.kind, e.idx, e.known_at) for e in ev if e.data["range_kind"] == "distribution"]
    assert got == [("ut", m["ut"], m["fb"]),
                   ("lpsy_c", m["x"], m["x"]),
                   ("utad_confirmed", m["x"], m["x_trig"]),
                   ("sow", m["sow"], m["sow"]),
                   ("lpsy_d", m["rally_top"], m["lpsy_trig"])]
    assert all(e.direction == -1 for e in ev)
    c = _by_kind(ev, "lpsy_c")[0]
    assert c.data["vol_ok"] and "rv" in c.data["effort"] and c.data["result"] != "none"
    assert c.data["zone_lo"] <= c.price < c.data["ut_high"]
    u = _by_kind(ev, "utad_confirmed")[0]
    assert u.data["lower_high"] < u.data["ut_high"] and u.data["trigger_close"] < u.data["trigger_level"]
    d = _by_kind(ev, "lpsy_d")[0]
    assert d.data["retest"] == 1 and d.data["vol_ok"] and d.data["rv_mean"] <= 0.8
    assert d.data["rally_low_idx"] == m["sow"] + 1 and d.data["touch_idx"] == m["rally_top"]
    assert d.data["sow_known_at"] == m["sow"]


def test_ut_volume_variants_hv_and_lv():
    k, m = _dist_shape(ut_vol=250.0)
    u = _by_kind(upthrusts(k, _ranges(k)), "ut")[0]
    assert u.data["hv"] and u.data["hv2"] and not u.data["lv"] and u.data["rv"] >= 1.5
    assert u.data["prev_pivot_idx"] >= 0 and u.data["rv_ratio_prev_pivot"] >= 1.0
    k, m = _dist_shape(ut_vol=60.0)
    u = _by_kind(upthrusts(k, _ranges(k)), "ut")[0]
    assert u.data["lv"] and u.data["lv2"] and not u.data["hv"]
    assert u.data["vol_ok"] is False and u.data["variant"] == "hv"
    assert _by_kind(upthrusts(k, _ranges(k), variant="lv"), "ut")[0].data["vol_ok"] is True


def test_ut_failed_and_long_window_variant():
    k, m = _dist_shape(hold=4)                  # 5 closes above TR_hi, back inside on the 6th bar
    rs = _ranges(k)
    short = [e for e in upthrusts(k, rs, J_u=3) if e.data["range_kind"] == "distribution"]
    assert [(e.kind, e.idx, e.known_at, e.direction) for e in short] == [("ut_failed", m["ut"], m["ut"] + 2, +1)]
    long_ = [e for e in upthrusts(k, rs, J_u=10) if e.data["range_kind"] == "distribution"]
    assert [(e.kind, e.known_at) for e in long_] == [("ut", m["fb"])]
    assert long_[0].data["J_u"] == 10 and long_[0].data["bars_outside"] == m["fb"] - m["ut"] + 1


def test_sow_minor_found_where_intended():
    k, m = _dist_shape(sow="minor")
    ev = sow_events(k, _ranges(k))
    assert not _by_kind(ev, "sow")
    mi = [e for e in _by_kind(ev, "sow_minor") if e.data["range_kind"] == "distribution"]
    assert [(e.idx, e.known_at) for e in mi] == [(m["minor"], m["minor"])]
    e = mi[0]
    assert e.price >= e.data["ice"] and e.data["low_vs_ice_atr"] <= 0.5 and e.data["vol_ok"]


# ---------------------------------------------------------------- negative cases

def test_no_upthrust_means_no_ut_followons():
    k, m = _dist_shape(ut=False)
    ev = distribution_events(k, _ranges(k))
    kinds = {e.kind for e in ev}
    assert not kinds & {"ut", "ut_failed", "utad_confirmed", "lpsy_c"}
    assert "sow" in kinds and "lpsy_d" in kinds            # the breakdown half is independent


def test_rebound_to_a_new_high_cancels_utad_and_lpsy_c():
    k, m = _dist_shape(x_high=4.0)              # the rebound bar exceeds the UT high
    ev = [e for e in distribution_events(k, _ranges(k)) if e.data["range_kind"] == "distribution"]
    uts = _by_kind(ev, "ut")
    assert [u.idx for u in uts] == [m["ut"], m["x"]]      # the rebound bar is itself a one-bar upthrust
    follow = [e for e in ev if e.kind in ("utad_confirmed", "lpsy_c")]
    assert all(e.data["ut_idx"] != m["ut"] for e in follow)


def test_decline_that_holds_the_ice_is_no_sow():
    k, m = _dist_shape(sow=None)
    ev = distribution_events(k, _ranges(k))
    assert not _by_kind(ev, "sow") and not _by_kind(ev, "lpsy_d") and not _by_kind(ev, "sow_minor")


def test_rally_closing_back_above_the_ice_is_no_lpsy_d_and_fails_the_sow():
    k, m = _dist_shape(rally_top=-12.0)
    ev = distribution_events(k, _ranges(k))
    assert _by_kind(ev, "sow") and not _by_kind(ev, "lpsy_d")
    f = _by_kind(ev, "sow_failed")
    assert [(e.idx, e.known_at, e.direction) for e in f] == [(m["rally_top"], m["rally_top"], +1)]


def test_accumulation_ranges_are_ignored_by_default():
    k, m = accumulation(seed=0)                 # an accumulation range broken upwards by the SOS
    acc = event_ranges(k, "accumulation")[1]
    assert acc
    assert upthrusts(k, acc) == [] and sow_events(k, acc) == []
    assert upthrusts(k, acc, kinds=("accumulation",))      # the SOS break is seen once the kind is allowed


def test_utad_confirmation_expiry():
    k, m = _dist_shape()
    uts = [u for u in upthrusts(k, _ranges(k)) if u.data["range_kind"] == "distribution"]
    assert [(e.idx, e.known_at) for e in utad_confirmations(k, uts)] == [(m["x"], m["x_trig"])]
    assert m["x_trig"] - m["fb"] > 2 and utad_confirmations(k, uts, expiry=2) == []


# ---------------------------------------------------------------- volume gates and holidays

def test_volume_gate_ut():
    k, m = _dist_shape(ut_vol=60.0)
    rs = _ranges(k)
    assert _by_kind(distribution_events(k, rs), "ut")
    gated = distribution_events(k, rs, volume_gate=True)                       # gate on 'hv'
    assert not {"ut", "utad_confirmed", "lpsy_c"} & {e.kind for e in gated}
    assert _by_kind(distribution_events(k, rs, volume_gate=True, ut_kw={"variant": "lv"}), "ut")


def test_volume_gate_sow():
    k, m = distribution(seed=1)
    v = k.v.copy()
    v[m["sos"]:m["sos"] + 3] = 95.0                     # the breakdown bars lose their volume
    k2 = candles_from_arrays(k.o, k.h, k.l, k.c, v, v2=v)
    rs = _ranges(k2)
    un = _by_kind(sow_events(k2, rs), "sow")
    assert un and not un[0].data["vol_ok"] and not un[0].data["vol_ok2"]
    assert _by_kind(sow_events(k2, rs, volume_gate=True), "sow") == []


def test_volume_gate_lpsy_d_and_lpsy_c():
    k, m = _dist_shape(rally_vol=150.0, x_vol=90.0)      # loud LPSY_D rally; quiet LPSY_C bar
    rs = _ranges(k)
    sw = sow_events(k, rs)
    ut = upthrusts(k, rs)
    un = lpsy_events(k, sw, ut, rs)
    d, c = _by_kind(un, "lpsy_d"), _by_kind(un, "lpsy_c")
    assert d and not d[0].data["vol_ok"] and d[0].data["rv_mean"] > 0.8
    assert c and not c[0].data["vol_ok"] and c[0].data["effort"] == "none" and c[0].data["result"] != "none"
    assert lpsy_events(k, sw, ut, rs, volume_gate=True) == []


def test_holiday_never_satisfies_a_low_volume_condition():
    k, m = _dist_shape(holiday=("ut",))
    u = _by_kind(upthrusts(k, _ranges(k)), "ut")[0]
    assert u.data["holiday"] is True and u.data["lv"] is False and u.data["lv_rv"] is False
    assert u.data["rv_high"] < 1.0                       # the volume itself is low; the flag is still False
    k, m = _dist_shape(holiday=("rally_top",))
    d = _by_kind(distribution_events(k, _ranges(k)), "lpsy_d")[0]
    assert d.data["holiday"] is True and d.data["vol_ok"] is False and d.data["rv_mean"] <= 0.8


# ---------------------------------------------------------------- contract: types, order, determinism

def test_event_contract():
    for k in (_dist_shape()[0], _random_walk(11)):
        rs = _ranges(k) if len(k) < 1000 else _rw_ranges(k)
        ev = distribution_events(k, rs, lpsy_kw={"c_kinds": ("distribution", "generic")})
        assert ev
        again = distribution_events(k, rs, lpsy_kw={"c_kinds": ("distribution", "generic")})
        assert [_key(e) for e in ev] == [_key(e) for e in again]
        assert all(_same_data(a.data, b.data) for a, b in zip(ev, again))
        keys = [(e.known_at, e.idx) for e in ev]
        assert keys == sorted(keys)
        for e in ev:
            assert e.known_at >= e.idx >= 0 and e.direction in (-1, 1)
            assert e.direction == (+1 if e.kind.endswith("_failed") else -1)
            for key, val in e.data.items():
                assert type(val) in (bool, int, float, str), (e.kind, key, type(val))
            assert {"range_kind", "range_start", "range_known_at", "range_lo", "range_hi"} <= set(e.data)
            if e.kind in ("ut", "ut_failed", "sow", "sow_minor", "lpsy_c", "lpsy_d", "utad_confirmed"):
                assert {"rv", "rv2", "ae", "ae2"} <= set(e.data)
            if e.kind in ("ut", "ut_failed", "sow", "sow_minor", "lpsy_c", "lpsy_d"):
                assert {"vol_ok", "vol_ok2", "holiday"} <= set(e.data)


def test_bad_parameters_raise():
    k, _ = _dist_shape()
    with pytest.raises(ValueError):
        upthrusts(k, _ranges(k), J_u=0)
    with pytest.raises(ValueError):
        upthrusts(k, _ranges(k), variant="mid")


# ---------------------------------------------------------------- truncation

def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    return type(a) is type(b) and a == b


def _key(e):
    return (e.kind, e.idx, e.known_at, e.direction, None if e.price is None else round(e.price, 6))


def _same_data(a: dict, b: dict) -> bool:
    return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)


def _check_truncation(k, ranges_fn, cuts, **kw):
    full = distribution_events(k, ranges_fn(k), **kw)
    assert full
    n_checked = 0
    for cut in cuts:
        kh = k.head(cut)
        part = distribution_events(kh, ranges_fn(kh), **kw)        # ranges recomputed on the head
        expect = [e for e in full if e.known_at <= cut - 1]
        assert [_key(e) for e in part] == [_key(e) for e in expect], cut
        for a, b in zip(part, expect):
            assert a.data.keys() == b.data.keys(), (cut, a.kind)
            bad = [key for key in a.data if not _same(a.data[key], b.data[key])]
            assert not bad, (cut, a.kind, a.idx, bad)
        n_checked += 1
    assert n_checked >= 20
    return full


CONFIGS = [{}, {"J_u": 10}, {"volume_gate": True, "ut_kw": {"variant": "lv"}}]


@pytest.mark.parametrize("kw", CONFIGS)
def test_truncation_on_synthetic_shapes(kw):
    k, _ = _dist_shape()
    full = _check_truncation(k, _ranges, range(60, len(k), 5), **kw)
    assert {"ut", "utad_confirmed", "lpsy_c", "sow", "lpsy_d"} <= {e.kind for e in full}
    k, _ = _dist_shape(hold=4, sow="minor")
    _check_truncation(k, _ranges, range(60, len(k), 5), **kw)
    for seed in range(2):
        k, _ = distribution(seed=seed)
        _check_truncation(k, _ranges, range(60, len(k), 5), **kw)


@pytest.mark.parametrize("kw", CONFIGS)
def test_truncation_on_random_walk_with_ranges(kw):
    kinds = set()
    for seed in (3, 11, 21):
        k = _random_walk(seed)
        full = _check_truncation(k, _rw_ranges, range(100, 1500, 60),
                                 lpsy_kw={"c_kinds": ("distribution", "generic")}, **kw)
        kinds |= {e.kind for e in full}
    expected = {"ut", "utad_confirmed", "sow", "sow_minor", "lpsy_c", "lpsy_d"}
    if not kw.get("volume_gate"):
        expected |= {"ut_failed", "sow_failed"}
    assert expected <= kinds, expected - kinds


# ---------------------------------------------------------------- review additions (adversarial review of this module)

def test_ut_lv_reads_the_break_bar_rv():
    """Regression (review): d.12 UT_lv is "new high with RV_b < 1", the BREAK bar's RV. Here (J_u = 10) the UT high
    sits on a loud hold bar after a quiet break bar; the old code read the UT-high bar's RV and missed this UT_lv.
    The UT-high-bar version is kept as lv_rv_high. Both X2 variants are also exposed as Stage 0 '_ok' gate flags."""
    k, m = _dist_shape(hold=4, ut_vol=60.0, hold_vol=150.0)
    u = [e for e in upthrusts(k, _ranges(k), J_u=10, variant="lv") if e.data["range_kind"] == "distribution"]
    assert [(e.kind, e.known_at) for e in u] == [("ut", m["fb"])]
    d = u[0].data
    assert d["break_idx"] == m["ut"] and d["high_idx"] != d["break_idx"]
    assert d["rv"] < 1.0 <= d["rv_high"]
    assert d["lv_rv"] and d["lv_rv2"] and not d["lv_rv_high"] and not d["lv_pivot"]
    assert d["lv"] and d["vol_ok"] and not d["hv"]
    assert (d["hv_ok"], d["hv_ok2"], d["lv_ok"], d["lv_ok2"]) == (d["hv"], d["hv2"], d["lv"], d["lv2"])
    # a holiday on the break bar voids the RV_b clause (and only it); the event records it
    k, m = _dist_shape(hold=4, ut_vol=60.0, hold_vol=150.0, holiday=("ut",))
    d = [e for e in upthrusts(k, _ranges(k), J_u=10) if e.data["range_kind"] == "distribution"][0].data
    assert d["rv"] < 1.0 and d["holiday"] and d["holiday_break"] and not d["holiday_high"]
    assert not d["lv_rv"] and not d["lv"] and not d["lv_ok"]


def test_holiday_pivot_is_never_the_ut_volume_reference():
    """Regression (review): a holiday / short session is never a volume reference (base.py), so the 'lv' pivot
    clause skips a holiday pivot high and uses the latest confirmed non-holiday one."""
    k, m = _dist_shape(ut_vol=250.0)
    u = [e for e in upthrusts(k, _ranges(k)) if e.kind == "ut" and e.data["range_kind"] == "distribution"][0]
    p = u.data["prev_pivot_idx"]
    assert p >= 0
    hol = np.zeros(len(k), dtype=bool)
    hol[p] = True
    k2 = candles_from_arrays(k.o, k.h, k.l, k.c, k.v, v2=k.v2, holiday=hol)
    u2 = [e for e in upthrusts(k2, _ranges(k2)) if e.kind == "ut" and e.data["range_kind"] == "distribution"][0]
    assert (u2.idx, u2.known_at) == (u.idx, u.known_at)
    q = u2.data["prev_pivot_idx"]
    assert q != p and (q == -1 or (not k2.holiday[q] and q < p))


def _rw_ranges_aged(k):
    """Short-lived ranges (max_age = 25 bars) so ranges also END BY AGE inside the series. The builder reported that a
    detector reading r.end_reason == 'age' went uncaught because no test range lived 250 bars."""
    return (generic_ranges(k, window=40, max_age=25)
            + event_ranges(k, "distribution", volume_gate=False, max_age=25)[1])


def _random_walk_late_v2(seed: int):
    """_random_walk with the other feed missing for the first 600 bars (Dukascopy starts years after FTMO), so the
    feed-2 metrics turn from NaN to finite inside the series."""
    k = _random_walk(seed)
    v2 = k.v2.copy()
    v2[:600] = np.nan
    return candles_from_arrays(k.o, k.h, k.l, k.c, k.v, v2=v2, holiday=k.holiday)


def _boundary_cuts(k, ranges_fn, **kw):
    """Every cut on a decision boundary of the full run: each event's idx + 1 (its happens@ bar is the last bar
    seen), known_at (the event must be absent) and known_at + 1 (present and identical); each range's known_at + 1
    and its end_idx and end_idx + 1 (where active_at flips); plus a coarse grid over the quiet stretches."""
    rs = ranges_fn(k)
    cuts = set(range(50, len(k) + 1, 97))
    for e in distribution_events(k, rs, **kw):
        cuts |= {e.idx + 1, e.known_at, e.known_at + 1}
    for r in rs:
        cuts.add(r.known_at + 1)
        if r.end_idx is not None:
            cuts |= {r.end_idx, r.end_idx + 1}
    return sorted(c for c in cuts if 1 <= c <= len(k))


@pytest.mark.parametrize("kw", CONFIGS)
def test_truncation_every_cut_on_synthetic_shapes(kw):
    """Adversarial truncation (review): EVERY cut 1..n on the short synthetic shapes (not every 5th bar from 60),
    including a J_u = 10 UT whose high is not the break bar."""
    for k, _ in (_dist_shape(), _dist_shape(hold=4, ut_vol=60.0, hold_vol=150.0, sow="minor"), distribution(seed=0)):
        _check_truncation(k, _ranges, range(1, len(k) + 1), **kw)


@pytest.mark.parametrize("kw", [{}, {"volume_gate": True, "ut_kw": {"variant": "lv"}}])
def test_truncation_at_every_decision_boundary_on_random_walks(kw):
    """Adversarial truncation (review) on 1500-bar random walks with holidays: cuts at every event / range decision
    boundary, with ranges that end by breakout, breakdown AND age, and a second feed that starts late."""
    gated = kw.get("volume_gate", False)
    kw = {"lpsy_kw": {"c_kinds": ("distribution", "generic")}, **kw}
    kinds, reasons = set(), set()
    walks = (_random_walk_late_v2(11),) if gated else (_random_walk(3), _random_walk_late_v2(11))   # test time
    for k in walks:
        for rf in (_rw_ranges, _rw_ranges_aged):
            cuts = _boundary_cuts(k, rf, **kw)
            assert len(cuts) >= 40
            full = _check_truncation(k, rf, cuts, **kw)
            kinds |= {e.kind for e in full}
            reasons |= {r.end_reason for r in rf(k)}
    assert {"breakout_up", "breakdown", "age"} <= reasons
    expected = {"ut", "utad_confirmed", "sow", "lpsy_c"} | (set() if gated else
                                                           {"ut_failed", "sow_failed", "sow_minor", "lpsy_d"})
    assert expected <= kinds, expected - kinds


def test_uk_only_holiday_does_not_veto_ut_or_lpsy_d_low_volume():
    """s03-fix F1: gate S0-3 NSU-1 lists the readers of holiday_lowvol exhaustively (no_supply, no_demand, spring,
    spring_test, LPS, ST flags) and says "No change to upthrusts()". A bar flagged ONLY as a UK bank holiday
    therefore leaves the UT 'lv' clauses and the LPSY_D rally gate unchanged; holiday_lowvol is stored, descriptive;
    'holiday' stays CME-only. A CME holiday on the same bar still vetoes (the pre-S0-3 rule)."""
    import dataclasses
    k, m = _dist_shape()
    u0 = _by_kind(upthrusts(k, _ranges(k)), "ut")[0]
    assert u0.data["lv_rv"] is True and u0.data["holiday_lowvol"] is False
    lv = np.zeros(len(k), bool)
    lv[m["ut"]] = True
    k2 = dataclasses.replace(k, holiday_lowvol=lv)
    u = _by_kind(upthrusts(k2, _ranges(k2)), "ut")[0]
    assert u.data["holiday"] is False and u.data["holiday_lowvol"] is True
    assert u.data["lv_rv"] is True and u.data["lv"] == u0.data["lv"] and u.data["vol_ok"] == u0.data["vol_ok"]
    k3 = dataclasses.replace(k, holiday=lv, holiday_lowvol=lv)            # CME holiday on the same bar: vetoed
    u3 = _by_kind(upthrusts(k3, _ranges(k3)), "ut")[0]
    assert u3.data["holiday"] is True and u3.data["lv_rv"] is False
    k, m = _dist_shape()
    d0 = _by_kind(distribution_events(k, _ranges(k)), "lpsy_d")[0]
    assert d0.data["vol_ok"] is True and d0.data["holiday_lowvol"] is False
    lv = np.zeros(len(k), bool)
    lv[m["rally_top"]] = True
    k2 = dataclasses.replace(k, holiday_lowvol=lv)
    d = _by_kind(distribution_events(k2, _ranges(k2)), "lpsy_d")[0]
    assert d.data["holiday"] is False and d.data["holiday_lowvol"] is True and d.data["vol_ok"] is True
    k3 = dataclasses.replace(k, holiday=lv, holiday_lowvol=lv)
    d3 = _by_kind(distribution_events(k3, _ranges(k3)), "lpsy_d")[0]
    assert d3.data["holiday"] is True and d3.data["vol_ok"] is False
