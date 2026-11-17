"""Tests for wyk.detectors.effort (docs/01 §5.2 effort vs result x4 and 特性改变; notes.md d.15, d.16).
Synthetic data only; no market data, no outcomes."""

import math

import numpy as np

from app.research.wyckoff.base import candles_from_arrays
from app.research.wyckoff.effort import EFFORT_KINDS, choch_events, effort_events
from .shapes import accumulation, distribution


# ---------- synthetic builders ----------

def _ohlc(closes, wick=0.5, seed=0, jitter=0.2):
    """Bars from a close path: open = previous close, wicks of `wick` +- jitter on both sides."""
    rng = np.random.default_rng(seed)
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    w = wick + rng.uniform(-jitter, jitter, len(c))
    return o, np.maximum(o, c) + w, np.minimum(o, c) - w, c


def _vol(n, seed=0, base=100.0):
    return base + np.random.default_rng(seed + 100).normal(0, 3, n)


def _trend(n_trend, step, tail_steps, start=1000.0):
    """n_trend bars moving `step` per bar, then bars moving by tail_steps (list)."""
    steps = np.r_[np.full(n_trend, step), tail_steps]
    return start + np.cumsum(steps)


def _by_kind(events, kind):
    return {e.idx: e for e in events if e.kind == kind}


# ---------- churn / absorption ----------

def _stall(up: bool, vol_t: float = 300.0):
    """150-bar steady trend (step +-1, ATR ~2), then at T a flat close with a long wick against the trend, then
    10 more trend bars. Returns (candles, T)."""
    s = 1.0 if up else -1.0
    c = _trend(150, s, [0.0] + [s] * 10)
    o, h, l, c = _ohlc(c)
    T = 150
    if up:
        h[T], l[T] = c[T] + 1.5, c[T] - 0.3
    else:
        h[T], l[T] = c[T] + 0.3, c[T] - 1.5
    v = _vol(len(c))
    v[T] = vol_t
    return candles_from_arrays(o, h, l, c, v, v2=v * 0.8), T


def test_churn_found_at_high_volume_stall_in_uptrend():
    k, T = _stall(up=True)
    ev = effort_events(k)
    churn = _by_kind(ev, "churn")
    assert set(churn) == {T}                       # trend bars (dC = 0.5 ATR, wick 25%) never qualify
    d = churn[T].data
    assert churn[T].direction == -1 and churn[T].price == k.h[T] and churn[T].known_at == T
    assert d["ctx_trend"] and d["ctx_near_high"] and d["result_close"] and d["result_wick"]
    assert d["rv"] >= 2.0 and d["effort_rv_ok"] is d["effort_rv2_ok"] is d["vol_ok"] is d["vol2_ok"] is True
    assert d["effort_ae_ok"] is True and d["ae"] >= 1.0      # AE is defined after 100 reference bars
    assert not _by_kind(ev, "absorption")           # no Down context, never near the 20-bar low


def test_churn_without_volume_is_emitted_ungated_and_dropped_by_the_gate():
    k, T = _stall(up=True, vol_t=100.0)
    e = _by_kind(effort_events(k), "churn")[T]
    assert e.data["effort_rv_ok"] is False and e.data["effort_ae_ok"] is False and e.data["vol_ok"] is False
    assert T not in _by_kind(effort_events(k, volume_gate=True), "churn")
    k2, T2 = _stall(up=True)
    assert T2 in _by_kind(effort_events(k2, volume_gate=True), "churn")


def test_absorption_found_in_downtrend_and_not_churn():
    k, T = _stall(up=False)
    ev = effort_events(k)
    ab = _by_kind(ev, "absorption")
    assert T in ab and ab[T].direction == +1 and ab[T].price == k.l[T]
    d = ab[T].data
    assert d["ctx_trend"] and d["result_wick"] and d["effort_rv_ok"] is True and d["vol_ok"] is True
    assert all(e.data["vol_ok"] is not True for i, e in ab.items() if i != T)
    assert not _by_kind(ev, "churn")


# ---------- no supply ----------

def _no_supply(holiday_at: int | None = None):
    """150-bar downtrend, then 4 quiet bars (dC = -0.1, range 0.4, volume 30), then the decline resumes."""
    c = _trend(150, -1.0, [-0.1] * 4 + [-1.0] * 5)
    o, h, l, c = _ohlc(c)
    T = 150
    for i in range(T, T + 4):
        h[i], l[i] = o[i] + 0.2, c[i] - 0.2
    v = _vol(len(c))
    v[T:T + 4] = 30.0
    hol = np.zeros(len(c), bool)
    if holiday_at is not None:
        hol[holiday_at] = True
    return candles_from_arrays(o, h, l, c, v, v2=v * 0.8, holiday=hol), T


def test_no_supply_found_on_quiet_stall_in_downtrend():
    k, T = _no_supply()
    ns = _by_kind(effort_events(k), "no_supply")
    assert T + 2 in ns and T + 3 in ns                # first bars whose 3-bar window is all quiet
    assert T + 1 not in ns                            # window still holds the last trend bar (undercut 2x0.25 ATR)
    e = ns[T + 2]
    d = e.data
    assert e.direction == +1 and e.price == min(k.l[T:T + 3]) and d["window_start"] == T
    assert d["mean_rv"] <= 0.7 and d["effort_rv_ok"] is d["effort_rv2_ok"] is d["vol_ok"] is True
    assert not d["holiday"] and d["undercut_atr"] <= 0.25 and d["mean_abs_dc_atr"] <= 0.3
    assert not any(i < T for i in ns)                 # trend bars move 0.5 ATR per bar


def test_no_supply_holiday_never_counts_as_low_volume():
    k, T = _no_supply(holiday_at=151)
    ns = _by_kind(effort_events(k), "no_supply")
    for t in (T + 2, T + 3):                          # both windows contain the holiday bar
        d = ns[t].data
        assert d["holiday"] and d["effort_rv_ok"] is d["effort_rv2_ok"] is d["vol_ok"] is False
    gated = _by_kind(effort_events(k, volume_gate=True), "no_supply")
    assert T + 2 not in gated and T + 3 not in gated
    assert T + 2 in _by_kind(effort_events(_no_supply()[0], volume_gate=True), "no_supply")


def test_no_supply_needs_down_context():
    c = _trend(150, +1.0, [-0.1] * 4)                # same quiet bars after an UP trend
    o, h, l, c = _ohlc(c)
    v = _vol(len(c))
    v[150:] = 30.0
    k = candles_from_arrays(o, h, l, c, v)
    assert not _by_kind(effort_events(k), "no_supply")


# ---------- no demand ----------

def _no_demand(vol_t: float = 100.0, result: str | None = "wick", holiday=None, holiday_lowvol=None):
    """60-bar rise to a pivot high P on RV ~2.5, 4-bar pullback, rise to a new 20-bar high at T on normal volume.
    result (S0-3 NDE-5, the 01 §5.2 clause at T): 'wick' = a 2-point upper wick on T (wick share ~0.57 >= 0.5),
    'close' = T closes only 0.2 above C_{T-1} (~0.1 ATR <= 0.2 ATR) with its high still a new 20-bar high, None =
    the plain +1 close (0.5 ATR, wick share 0.25: no result clause)."""
    c = _trend(60, 1.0, [-1.0] * 4 + [1.0] * 12)
    o, h, l, c = _ohlc(c, jitter=0.0)
    P, T = 59, 68
    if result == "wick":
        h[T] = c[T] + 2.0
    elif result == "close":
        c[T] = c[T - 1] + 0.2
        h[T] = c[T] + 0.5
    v = _vol(len(c))
    v[P] = 250.0
    v[T] = vol_t
    return candles_from_arrays(o, h, l, c, v, v2=v * 0.8, holiday=holiday, holiday_lowvol=holiday_lowvol), P, T


def test_no_demand_new_high_on_lower_rv_than_previous_pivot_high():
    k, P, T = _no_demand()
    nd = _by_kind(effort_events(k), "no_demand")
    assert T in nd and T - 1 not in nd and T - 2 not in nd   # T is the first bar above the prior 20-bar high
    e = nd[T]
    d = e.data
    assert e.direction == -1 and e.price == k.h[T]
    assert d["prev_high_idx"] == P and d["prev_high_price"] == k.h[P]
    assert d["below_prev_rv_ok"] is True and d["low_rv_ok"] is False
    assert d["effort_rv_ok"] is d["vol_ok"] is d["below_prev_rv2_ok"] is d["effort_rv2_ok"] is True
    assert math.isclose(d["rv_ratio_prev"], d["rv"] / d["rv_prev_high"])


def test_no_demand_high_volume_breakout_fails_the_gate():
    k, P, T = _no_demand(vol_t=300.0)
    e = _by_kind(effort_events(k), "no_demand")[T]
    assert e.data["effort_rv_ok"] is False and e.data["vol_ok"] is False
    assert T not in _by_kind(effort_events(k, volume_gate=True), "no_demand")


def test_no_demand_pivot_is_only_used_once_known():
    """Before P is confirmed (P + 3), a new high must not see P as the previous pivot."""
    k, P, T = _no_demand()
    for e in effort_events(k, kinds=("no_demand",)):
        assert e.data["prev_high_idx"] == -1 or e.data["prev_high_idx"] + 3 <= e.known_at


def test_no_demand_result_clause():
    """S0-3 NDE-5: a new high is a no_demand candidate only if (C_t - C_{t-1}) <= 0.2 ATR or the upper wick is
    >= 50% of the range. A strong up close without a wick is not emitted (ungated either)."""
    k, P, T = _no_demand(result=None)
    assert (k.c[T] - k.c[T - 1]) / k.atrp[T] > 0.2
    assert T not in _by_kind(effort_events(k), "no_demand")
    k, P, T = _no_demand(result="wick")
    d = _by_kind(effort_events(k), "no_demand")[T].data
    assert d["result_wick"] is True and d["result_close"] is False and d["upper_wick_share"] >= 0.5
    assert math.isclose(d["upper_wick_share"], (k.h[T] - max(k.o[T], k.c[T])) / (k.h[T] - k.l[T]))
    assert math.isclose(d["result_atr"], (k.c[T] - k.c[T - 1]) / k.atrp[T]) and d["result_atr"] > 0.2
    k, P, T = _no_demand(result="close")
    d = _by_kind(effort_events(k), "no_demand")[T].data
    assert d["result_close"] is True and d["result_wick"] is False and d["result_atr"] <= 0.2
    # the boundary itself: exactly 0.2 ATR passes, a hair above fails (signed, one-sided like churn)
    assert not _by_kind(effort_events(k, result_atr=d["result_atr"] - 1e-9), "no_demand").get(T)
    assert T in _by_kind(effort_events(k, result_atr=d["result_atr"]), "no_demand")


def test_no_demand_ratio_branch_uses_e_lo():
    """S0-3 NDE-2: the relative branch is RV_t <= e_lo * RV_p (明显缩量), the same e_lo as the absolute branch.
    T at RV ~1.8 against a pivot at RV ~2.5 (ratio ~0.72): fails at e_lo 0.7, passes at 0.9; the absolute branch
    (RV_t <= e_lo) is False at both."""
    k, P, T = _no_demand(vol_t=180.0)
    r = k.rv[T] / k.rv[P]
    assert 0.7 < r < 0.9 and k.rv[T] > 0.9
    for e_lo, expect in ((0.7, False), (0.9, True)):
        d = _by_kind(effort_events(k, e_lo=e_lo), "no_demand")[T].data
        assert d["low_rv_ok"] is False
        assert d["below_prev_rv_ok"] is expect and d["effort_rv_ok"] is expect and d["vol_ok"] is expect
        assert math.isclose(d["rv_ratio_prev"], r) and math.isclose(d["rv_prev_high"], k.rv[P])
        assert d["below_prev_rv2_ok"] is bool(k.rv2[T] <= e_lo * k.rv2[P])
        assert d["below_prev_ae_ok"] is None and np.isnan(k.ae[T])       # AE needs 100 bars (min_obs) here
        gated = _by_kind(effort_events(k, e_lo=e_lo, volume_gate=True), "no_demand")
        assert (T in gated) is expect
    # the absolute branch alone: a very quiet new high passes at every e_lo >= its RV
    k, P, T = _no_demand(vol_t=50.0)
    d = _by_kind(effort_events(k, e_lo=0.7), "no_demand")[T].data
    assert d["low_rv_ok"] is True and d["effort_rv_ok"] is True


def test_low_volume_flags_read_holiday_lowvol_only():
    """S0-3 NSU-1: a UK-only bank holiday (holiday_lowvol True, CME holiday False) forces every low-volume flag to
    False; data['holiday'] stays the CME flag; high-volume kinds only record it."""
    k0, P, T = _no_demand()
    lv = np.zeros(len(k0), bool)
    lv[T] = True
    k, P, T = _no_demand(holiday_lowvol=lv)
    assert not k.holiday[T] and k.holiday_lowvol[T]
    assert np.array_equal(k.rv, k0.rv, equal_nan=True) and np.array_equal(k.ae, k0.ae, equal_nan=True)      # references do not read the UK flag
    d0 = _by_kind(effort_events(k0), "no_demand")[T].data
    d = _by_kind(effort_events(k), "no_demand")[T].data
    assert d0["effort_rv_ok"] is True and d0["holiday_lowvol"] is False
    assert d["holiday"] is False and d["holiday_lowvol"] is True
    for f in ("effort_rv_ok", "effort_rv2_ok", "low_rv_ok", "below_prev_rv_ok", "vol_ok", "effort_ae_ok"):
        assert d[f] is False, f
    # no_supply: a UK-only bar anywhere in the 3-bar window
    k, T = _no_supply()
    lv = np.zeros(len(k), bool)
    lv[T + 1] = True
    k2 = candles_from_arrays(k.o, k.h, k.l, k.c, k.v, v2=k.v2, holiday_lowvol=lv)
    ns = _by_kind(effort_events(k2), "no_supply")
    for t in (T + 2, T + 3):
        d = ns[t].data
        assert d["holiday"] is False and d["holiday_lowvol"] is True
        assert d["effort_rv_ok"] is d["effort_rv2_ok"] is d["vol_ok"] is False
    assert ns[T + 2].data["holiday_lowvol_t"] is False
    assert _by_kind(effort_events(_no_supply()[0]), "no_supply")[T + 2].data["vol_ok"] is True
    # churn / absorption carry the flag but do not use it
    for e in effort_events(k2, kinds=("churn", "absorption")):
        assert "holiday_lowvol" in e.data


CONTRACT = ("rv", "ae", "effort_rv_ok", "effort_ae_ok", "result_atr", "upper_wick_share", "lower_wick_share",
            "holiday", "holiday_lowvol", "news")


def test_field_contract():
    """Field names other builders rely on (S0-3 T8 manifest): present in every effort event."""
    k = _walk_with_ranges(5, uk=True)
    seen = set()
    for e in effort_events(k):
        missing = [f for f in CONTRACT if f not in e.data]
        assert not missing, (e.kind, missing)
        if e.kind == "no_supply":
            assert "mean_rv" in e.data
        if e.kind == "no_demand":
            assert "rv_prev_high" in e.data
        a, t = k.atrp[e.idx], e.idx
        assert math.isclose(e.data["result_atr"], (k.c[t] - k.c[t - 1]) / a, rel_tol=1e-12)
        seen.add(e.kind)
    assert seen == set(EFFORT_KINDS)
    # the AE mirror of no_demand keeps the literal AE_t < AE_p (descriptive, never gating), unlike the RV ratio
    n_ae = 0
    for e in effort_events(k, kinds=("no_demand",)):
        d = e.data
        if np.isfinite(d["ae"]) and np.isfinite(d["ae_prev_high"]) and not d["holiday_lowvol"]:
            assert d["below_prev_ae_ok"] is bool(d["ae"] < d["ae_prev_high"])
            assert d["below_prev_rv_ok"] is bool(d["rv"] <= 0.7 * d["rv_prev_high"])
            n_ae += 1
    assert n_ae > 10


def test_flags_are_tri_state_and_follow_the_runner_convention():
    """None = metric unavailable (NaN RV/AE or no prior pivot); False on holiday_lowvol bars (CME or UK) for
    low-volume flags; every '*_ok' value is True / False / None; Kleene OR for the combined flags."""
    k = _walk_with_ranges(5, uk=True)
    seen = set()
    for e in effort_events(k):
        d = e.data
        for name, v in d.items():
            if name.endswith("_ok"):
                assert v is None or isinstance(v, bool), (name, v)
        rv_nan = d["rv"] != d["rv"]
        if e.kind in ("churn", "absorption"):
            if rv_nan:
                assert d["effort_rv_ok"] is None
            if d["effort_rv_ok"] is True or d["effort_ae_ok"] is True:
                assert d["vol_ok"] is True
            elif None in (d["effort_rv_ok"], d["effort_ae_ok"]):
                assert d["vol_ok"] is None
            else:
                assert d["vol_ok"] is False
        assert d["holiday_lowvol"] or not d["holiday"]          # holiday implies holiday_lowvol
        if e.kind == "no_supply" and d["holiday_lowvol"]:
            assert d["effort_rv_ok"] is False and d["effort_rv2_ok"] is False
        if e.kind == "no_demand":
            assert d["result_close"] or d["result_wick"]
            if d["holiday_lowvol"]:
                assert d["effort_rv_ok"] is False and d["low_rv_ok"] is False and d["below_prev_rv_ok"] is False
            elif d["prev_high_idx"] == -1:
                assert d["below_prev_rv_ok"] is None
        seen.add((e.kind, d["vol_ok"]))
    assert ("churn", None) in seen                   # warm-up bars: RV reference not yet available
    assert ("no_demand", False) in seen and ("no_demand", True) in seen


# ---------- change of character ----------

def _choch(rejections: int = 2, drop_in_hold: bool = False, seed: int = 0, hold_len: int = 6):
    """Zigzag base, `rejections` rallies to a 110 high each followed by a fast 7-point drop, a rally back, a hold
    of 6 quiet closes just under 110, then a breakout. Returns (candles, hold_start, level_bar)."""
    base = list(100 + 0.8 * (-1.0) ** np.arange(80))
    up1 = [101.5, 103, 104.5, 106, 107.5, 109.2]
    up = [104.5, 106, 107.5, 109.2]
    down = [107, 105, 103]
    path, peaks = base + up1, [len(base) + len(up1) - 1]
    path += down
    for _ in range(rejections - 1):
        path += up
        peaks.append(len(path) - 1)
        path += down
    path += [104.5, 106, 107.5, 108.6]
    hold_start = len(path)
    hold = [109.0, 108.8, 109.2, 108.9, 109.1, 109.0] + list(109.0 + 0.3 * np.sin(np.arange(hold_len - 6)))
    if drop_in_hold:
        hold[3] = 107.9
    path += hold + [110.5, 112, 113.5, 115, 116.5]
    o, h, l, c = _ohlc(path, wick=0.3, seed=seed, jitter=0.05)
    for p in peaks:
        h[p] = 110.0
    if drop_in_hold:
        l[hold_start + 3] = 107.0
    v = _vol(len(c), seed)
    return candles_from_arrays(o, h, l, c, v, v2=v * 0.8), hold_start, peaks


def test_choch_found_after_two_rejections_and_a_hold():
    k, s0, peaks = _choch()
    for hold_on in ("close", "bar"):
        ev = choch_events(k, hold_on=hold_on)
        assert len(ev) == 1, hold_on
        e = ev[0]
        d = e.data
        assert e.kind == "choch" and e.direction == +1 and e.price == 110.0
        assert d["level_idx"] == peaks[-1] and d["n_rejections"] == 2 and d["rejections"] == tuple(peaks)
        # the last rally bar (close 108.6) already closes inside the 1-ATR band but its low does not
        assert d["hold_start"] == (s0 - 1 if hold_on == "close" else s0)
        assert e.known_at == e.idx == d["hold_start"] + 4          # the n_h-th bar of the stay
        assert all(end < d["hold_start"] for end in d["rejection_ends"])
        assert d["hold_max_decline_atr"] < 1.0


def test_choch_needs_two_rejections():
    k, _, _ = _choch(rejections=1)
    assert choch_events(k) == []
    k3, _, _ = _choch(rejections=3)
    ev = choch_events(k3)
    assert len(ev) == 1 and ev[0].data["n_rejections"] == 3
    assert choch_events(k3, min_rejections=4) == []


def test_choch_blocked_by_a_decline_inside_the_hold():
    k, s0, _ = _choch(drop_in_hold=True)            # bar s0+3 closes 107.9 with a low 107.0 (> 1 ATR drop)
    assert choch_events(k) == [] and choch_events(k, hold_on="bar") == []


def test_choch_long_hold_emits_once():
    """A 25-bar stay forms minor pivots inside the band that become 'the last swing high'; still one event."""
    k, s0, peaks = _choch(hold_len=25)
    ev = choch_events(k)
    assert len(ev) == 1 and ev[0].data["level_idx"] == peaks[-1]


def test_choch_max_level_variant_runs():
    k, _, peaks = _choch()
    ev = choch_events(k, level="max")
    assert len(ev) == 1 and ev[0].data["level"] == 110.0 and ev[0].data["level_mode"] == "max"


# ---------- truncation (causality) ----------

def _norm(x):
    if isinstance(x, float):
        return "nan" if math.isnan(x) else round(x, 6)
    if isinstance(x, tuple):
        return tuple(_norm(y) for y in x)
    return x


def _key(e):
    return (e.kind, e.idx, e.known_at, e.direction, None if e.price is None else round(e.price, 6),
            tuple(sorted((name, _norm(v)) for name, v in e.data.items())))


def _check_truncation(fn, k, cuts, at_events=False):
    """fn(k.head(cut)) must equal the full-series events with known_at <= cut - 1, data included. at_events adds
    the cuts that end exactly on each event bar and one bar later, where a look-ahead would show."""
    full = fn(k)
    assert all(e.known_at >= e.idx for e in full)
    assert [(e.known_at, e.idx) for e in full] == sorted((e.known_at, e.idx) for e in full)
    cuts = set(cuts)
    if at_events:
        cuts |= {e.known_at + d for e in full for d in (1, 2) if e.known_at + d <= len(k)}
    n_cuts = 0
    for cut in sorted(cuts):
        part = fn(k.head(cut))
        expect = [e for e in full if e.known_at <= cut - 1]
        assert [_key(e) for e in part] == [_key(e) for e in expect], cut
        n_cuts += 1
    assert n_cuts >= 20
    return full


def _walk_with_ranges(seed: int, n: int = 1500, uk: bool = False):
    """Random walk with box ranges, quiet stalls and embedded rejection-then-hold patterns; lognormal volume and
    ~1% holiday bars. uk=True adds ~2% UK-only holiday_lowvol bars (in runs of 1-3)."""
    rng = np.random.default_rng(seed)
    path = [1000.0]
    while len(path) < n:
        kind = rng.integers(0, 4)
        p = path[-1]
        if kind == 0:                                           # trend leg
            drift = rng.choice([-0.6, 0.6])
            path += list(p + np.cumsum(rng.normal(drift, 1.0, rng.integers(60, 140))))
        elif kind == 1:                                         # box range
            m = rng.integers(40, 90)
            path += list(p + 4 * np.sin(np.arange(1, m + 1) / rng.uniform(1.5, 3.0)) + rng.normal(0, 0.4, m))
        elif kind == 2:                                         # quiet stall
            path += list(p + np.cumsum(rng.normal(0, 0.15, rng.integers(5, 15))))
        else:                                                   # two rejections at a level, then a hold
            lv = p + 10
            seg = [p + 2, p + 4, p + 6, p + 8, lv - 0.8, lv - 3, lv - 5, lv - 7,
                   lv - 5, lv - 3, lv - 0.8, lv - 3, lv - 5, lv - 7, lv - 5, lv - 3, lv - 1.4]
            seg += list(lv - 1.0 + rng.normal(0, 0.15, 6))
            path += seg
    c = np.asarray(path[:n])
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.1, 0.9, n)
    l = np.minimum(o, c) - rng.uniform(0.1, 0.9, n)
    v = rng.lognormal(4.6, 0.5, n) * (1 + np.abs(c - o))
    v2 = v * rng.lognormal(0, 0.2, n)
    hol = rng.random(n) < 0.01
    lv = None
    if uk:
        lv = np.zeros(n, bool)
        for s0 in np.flatnonzero(np.random.default_rng(seed + 7).random(n) < 0.01):
            lv[s0:s0 + 1 + s0 % 3] = True
    return candles_from_arrays(o, h, l, c, v, v2=v2, holiday=hol, holiday_lowvol=lv)


def test_effort_truncation_on_shapes():
    for seed in range(2):
        for k in (accumulation(seed=seed)[0], distribution(seed=seed)[0]):
            full = _check_truncation(effort_events, k, range(30, len(k) + 1, 9))
            assert {e.kind for e in full} >= {"churn", "absorption", "no_demand"}


def test_effort_truncation_on_random_walk():
    k = _walk_with_ranges(5)
    full = _check_truncation(effort_events, k, range(100, 1501, 61))
    kinds = {e.kind for e in full}
    assert kinds == set(EFFORT_KINDS)
    for kind in EFFORT_KINDS:                           # the volume flags are exercised both ways
        flags = {e.data["vol_ok"] for e in full if e.kind == kind}
        assert {True, False} <= flags, kind
    _check_truncation(lambda kk: effort_events(kk, volume_gate=True), k, range(100, 1501, 61))


def test_effort_truncation_with_uk_holidays_and_ratio_branch():
    """S0-3 T5 truncation test: >= 50 cut points, UK-only holiday_lowvol bars, the NDE-2 ratio branch at every
    e_lo of the declared axis and the NDE-5 result clause; events known by cut - 1 equal the full run, field by
    field (pivot known_at = idx + lag)."""
    k = _walk_with_ranges(7, uk=True)
    assert (k.holiday_lowvol & ~k.holiday).sum() > 20
    cuts = list(range(60, 1501, 25))
    assert len(cuts) >= 50
    for e_lo in (0.5, 0.7, 0.9):
        full = _check_truncation(lambda kk: effort_events(kk, e_lo=e_lo), k, cuts, at_events=False)
        nd = [e for e in full if e.kind == "no_demand"]
        assert any(e.data["holiday_lowvol"] and not e.data["holiday"] for e in nd + [
            e for e in full if e.kind == "no_supply"])
        assert {e.data["below_prev_rv_ok"] for e in nd} >= {True, False}
    _check_truncation(lambda kk: effort_events(kk, volume_gate=True), k, cuts)


def test_choch_truncation():
    for seed in (5, 6):
        k = _walk_with_ranges(seed)
        for kw in ({}, {"hold_on": "bar"}, {"level": "max"}, {"n_h": 2}, {"n_h": 3, "hold_on": "bar"}):
            full = _check_truncation(lambda kk: choch_events(kk, **kw), k, range(100, 1501, 61), at_events=True)
            assert len(full) >= 3, kw
    ks, _, _ = _choch(rejections=3)
    full = _check_truncation(choch_events, ks, range(40, len(ks) + 1, 4), at_events=True)
    assert len(full) == 1


def test_head_equals_recomputed_candles():
    """k.head(cut) is a slice; recomputing the Candles from truncated arrays gives the same events."""
    k = _walk_with_ranges(7)
    full = effort_events(k) + choch_events(k)
    for cut in (300, 777, 1200):
        kk = candles_from_arrays(k.o[:cut], k.h[:cut], k.l[:cut], k.c[:cut], k.v[:cut], v2=k.v2[:cut],
                                 holiday=k.holiday[:cut])
        part = effort_events(kk) + choch_events(kk)
        expect = [e for e in full if e.known_at <= cut - 1]
        assert sorted(map(_key, part)) == sorted(map(_key, expect))


def test_output_is_deterministic_and_plain():
    k = _walk_with_ranges(9)
    a, b = effort_events(k), effort_events(k)
    assert [_key(e) for e in a] == [_key(e) for e in b]
    order = {name: i for i, name in enumerate(EFFORT_KINDS)}
    assert [(e.known_at, e.idx, order[e.kind]) for e in a] == sorted((e.known_at, e.idx, order[e.kind]) for e in a)
    for e in a[:500] + choch_events(k):
        assert isinstance(e.idx, int) and isinstance(e.known_at, int) and isinstance(e.price, float)
        for v in e.data.values():
            assert v is None or isinstance(v, (bool, int, float, str, tuple)), (e.kind, v, type(v))


# ---------- review additions: regressions and an exhaustive truncation sweep ----------

def _late_rejection():
    """_choch with one early rejection (peak p0); the second approach is the last rally bar j = s0 - 1 (high 110)
    and its 2-ATR decline ends ON the stay's first bar s0 (a long lower wick, close back inside the band).
    Returns (candles, s0, p0, j)."""
    k, s0, peaks = _choch(rejections=1)
    h, l = k.h.copy(), k.l.copy()
    j = s0 - 1
    h[j] = 110.0
    l[s0] = 104.5                                   # <= 110 - 2*ATR_j (ATR_j ~ 2.3); close stays 109.0
    return candles_from_arrays(k.o, h, l, k.c, k.v, v2=k.v2), s0, peaks[0], j


def test_choch_short_series_does_not_crash():
    """Regression: the dec_end offset loop raised ValueError whenever reject_bars > len(k) >= n_h + 1, so k.head()
    of a short prefix crashed for n_h < reject_bars - 1 or reject_bars > n_h + 1 (never with the defaults)."""
    k = _walk_with_ranges(5)
    for kw in ({}, {"n_h": 1}, {"n_h": 2}, {"reject_bars": 8}, {"n_h": 3, "reject_bars": 10}):
        _check_truncation(lambda kk: choch_events(kk, **kw), k, range(0, 30))


def test_choch_emits_when_the_last_rejection_completes_on_the_first_hold_bar():
    """Regression: only the first bar of a hold run could emit. Here the run's first window s0..s0+4 (closes in the
    band) fails the history test because the second decline ends ON s0; one bar later it passes. The old code
    dropped the whole run; now the run emits once, one bar after it started."""
    k, s0, p0, j = _late_rejection()
    ev = choch_events(k)
    assert len(ev) == 1
    e, d = ev[0], ev[0].data
    assert e.price == 110.0 and d["rejections"] == (p0, j) and d["rejection_ends"][-1] == s0
    assert d["run_hold_start"] == s0 and d["hold_start"] == s0 + 1 and e.known_at == e.idx == s0 + 5
    # whole-bar mode: the wick bar is outside the band, so the stay starts at s0 + 1 and its first bar emits
    eb = choch_events(k, hold_on="bar")
    assert len(eb) == 1 and eb[0].idx == s0 + 5 and eb[0].data["run_hold_start"] == s0 + 1
    _check_truncation(choch_events, k, range(0, len(k) + 1))


def test_truncation_at_every_bar_adversarial():
    """Every cut 0..n on short synthetic shapes (including the tiny prefixes where almost nothing is defined) and
    every 5th cut plus the cuts at/after each event on a 1500-bar random walk, for both detectors and several
    parameter sets: events and their full data must equal the full-series events known by cut - 1."""
    eff = (lambda kk: effort_events(kk), lambda kk: effort_events(kk, volume_gate=True),
           lambda kk: effort_events(kk, lag=2, n_low=2, ref_n=10))
    cho = (lambda kk: choch_events(kk), lambda kk: choch_events(kk, hold_on="bar"),
           lambda kk: choch_events(kk, level="max"), lambda kk: choch_events(kk, n_h=1),
           lambda kk: choch_events(kk, n_h=2, lag=5))
    # _no_supply with a holiday inside / right after the quiet window: a holiday flag read from bar t+1 shows here
    shapes = (accumulation(seed=0)[0], distribution(seed=1)[0], _choch(rejections=3)[0], _late_rejection()[0],
              _no_supply(holiday_at=151)[0], _no_supply(holiday_at=154)[0])
    for k in shapes:
        for fn in eff + cho:
            _check_truncation(fn, k, range(0, len(k) + 1))
    k = _walk_with_ranges(11)
    cuts = range(0, len(k) + 1, 5)
    for fn in eff[:2]:
        assert len(_check_truncation(fn, k, cuts)) > 150      # S0-3 NDE-5 result clause trims no_demand
    for fn in (cho[0], cho[3]):
        assert len(_check_truncation(fn, k, cuts, at_events=True)) >= 5
