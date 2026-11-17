import numpy as np

from app.research.wyckoff.accumulation import accumulation_events
from app.research.wyckoff.base import candles_from_arrays
from app.research.wyckoff.distribution import distribution_events
from app.research.wyckoff.pivots import pivots
from app.research.wyckoff.quality import apply_s0, qualification_map, qualified_at, range_quality_at
from app.research.wyckoff.ranges import TradingRange, event_ranges, generic_ranges


def _box(n_trend=80, n_box=140, seed=7, period=1.6):
    rng = np.random.default_rng(seed)
    trend = 1000 - np.arange(n_trend) * 1.0
    box = 920 + 6 * np.sin(np.arange(n_box) / period) + rng.normal(0, 0.4, n_box)
    c = np.r_[trend, box]
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.2, 0.8, len(c))
    l = np.minimum(o, c) - rng.uniform(0.2, 0.8, len(c))
    return candles_from_arrays(o, h, l, c, rng.poisson(100, len(c)), v2=rng.poisson(60, len(c)))


def _piv(k):
    p = pivots(k, 3)
    return (np.array([e.idx for e in p if e.kind == "pivot_high"]), np.array([e.idx for e in p if e.kind == "pivot_low"]))


def test_a_real_box_qualifies_and_a_compressed_pseudo_range_does_not():
    k = _box()
    ph, pl = _piv(k)
    box = TradingRange("accumulation", 80, 95, float(k.l[80:140].min()), float(k.h[80:140].max()))
    q = qualified_at(k, box, ph, pl)
    assert q is not None and 95 <= q < 160
    # a 2-bar "range" right at the end of the trend, far too small and without touches
    tiny = TradingRange("accumulation", 70, 75, float(k.l[70:76].min()), float(k.h[70:76].max()))
    assert not range_quality_at(k, tiny, 75, ph, pl)


def test_quality_uses_only_bars_up_to_i():
    k = _box()
    ph_full, pl_full = _piv(k)
    r = TradingRange("generic", 80, 120, float(k.l[80:120].min()), float(k.h[80:120].max()))
    for i in range(120, 215, 5):
        ph_cut, pl_cut = _piv(k.head(i + 1))
        assert range_quality_at(k, r, i, ph_full, pl_full) == range_quality_at(k.head(i + 1), r, i, ph_cut, pl_cut)


def _walk(seed, n=1500):
    rng = np.random.default_rng(seed)
    # regime walk: alternating trends and flat oscillations so ranges and Phase C/D events occur
    steps = np.concatenate([rng.normal(-0.6 if j % 3 == 0 else (0.6 if j % 3 == 1 else 0.0), 1.0, 100)
                            for j in range(n // 100)])
    c = 1000 + np.cumsum(steps)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.1, 1.5, n)
    l = np.minimum(o, c) - rng.uniform(0.1, 1.5, n)
    v = rng.lognormal(4.5, 0.5, n)
    return candles_from_arrays(o, h, l, c, v, v2=v * rng.lognormal(0, 0.2, n))


def _run(k, side):
    _, ev = event_ranges(k, side, volume_gate=False)
    rs = sorted(list(ev) + list(generic_ranges(k)), key=lambda r: (r.known_at, r.start_idx, r.kind))
    fn = accumulation_events if side == "accumulation" else distribution_events
    return apply_s0(k, fn(k, rs), rs)


def _key(e):
    return (e.kind, e.idx, e.known_at, e.direction, None if e.price is None else round(e.price, 6),
            tuple(sorted((a, round(b, 9) if isinstance(b, float) and np.isfinite(b) else str(b))
                         for a, b in e.data.items())))


def test_apply_s0_truncation_on_regime_walks():
    found = 0
    for seed in (1, 2):
        k = _walk(seed)
        for side in ("accumulation", "distribution"):
            full = _run(k, side)
            found += len(full)
            cuts = sorted({e.known_at + 1 for e in full} | {e.known_at for e in full} | set(range(100, len(k), 37)))
            for cut in cuts:
                if cut < 2 or cut > len(k):
                    continue
                part = _run(k.head(cut), side)
                assert [_key(e) for e in part] == [_key(e) for e in full if e.known_at <= cut - 1], (seed, side, cut)
    assert found > 0


def test_unqualified_and_overlapped_generic_events_are_dropped():
    k = _walk(3)
    _, ev = event_ranges(k, "accumulation", volume_gate=False)
    rs = sorted(list(ev) + list(generic_ranges(k)), key=lambda r: (r.known_at, r.start_idx, r.kind))
    raw = accumulation_events(k, rs)
    kept = apply_s0(k, raw, rs)
    assert all(e.data["range_quality_ok"] for e in kept if "range_kind" in e.data)
    assert all(e.data["range_qualified_at"] < e.data["seq_start"] for e in kept if "range_kind" in e.data)
    assert len(kept) <= len(raw)
    keys = [(e.kind, e.idx, e.known_at) for e in kept]
    assert len(keys) == len(set(keys))


def test_qualification_map_equals_qualified_at_per_range():
    k = _walk(1)
    _, ev = event_ranges(k, "accumulation", volume_gate=False)
    rs = list(ev) + list(generic_ranges(k))
    ph, pl = _piv(k)
    qm = qualification_map(k, rs)
    assert len(qm) == len(rs)
    assert all(qm[(r.kind, r.start_idx, r.known_at)] == qualified_at(k, r, ph, pl) for r in rs)
    assert any(v is None for v in qm.values()) and any(v is not None for v in qm.values())


def test_spring_fold_leaves_apply_s0_nothing_to_drop():
    # Gate S0-3 T3: S0-1 / S0-2 are folded into springs(), so on the Stage 0 construction (Phase-A + generic
    # ranges) every spring sequence starts after its range qualified and no generic one under an active Phase-A
    # range; apply_s0 then only annotates and de-duplicates springs.
    n_seq = 0
    for seed in (1, 2, 3):
        k = _walk(seed)
        _, ev = event_ranges(k, "accumulation", volume_gate=False)
        rs = sorted(list(ev) + list(generic_ranges(k)), key=lambda r: (r.known_at, r.start_idx, r.kind))
        raw = [e for e in accumulation_events(k, rs) if e.kind in ("spring", "spring_failed")]
        ann = apply_s0(k, raw, rs, drop_unqualified=False, one_range_per_side=False)
        n_seq += len(ann)
        assert all(e.data["range_quality_ok"] and e.data["seq_start"] == e.data["break_idx"] for e in ann)
        acc = [r for r in rs if r.kind == "accumulation"]
        assert not any(r.active_at(e.data["break_idx"]) for e in ann if e.data["range_kind"] == "generic" for r in acc)
        kept = apply_s0(k, raw, rs)
        assert {(e.kind, e.idx, e.known_at) for e in kept} == {(e.kind, e.idx, e.known_at) for e in raw}
    assert n_seq > 0
