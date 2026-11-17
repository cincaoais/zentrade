# Ported from wyk/detectors/ranges.py @ fc142999 (user's own code).
"""Trading ranges (docs/01 §5.2; full drafts in docs/evidence/raw/notes.md d.1-d.4, d.9-d.11).

Two constructions, both causal (every decision at bar i reads bars <= i only):

generic_ranges   a window W qualifies as a range at bar t when its height is 3-10 ATR, it has >= 2 confirmed pivot
                 highs near the top and >= 2 confirmed pivot lows near the bottom, and its close slope is flat.
                 Needed for re-accumulation springs and event-level tests. No Phase-A events, so no SC reference.
event_ranges     the Phase-A chain. Accumulation: SC candidate -> AR -> ST; the range (TR_lo = SC low,
                 TR_hi = AR high) is known only at the ST confirmation. Distribution mirror: BC -> AR -> ST.
                 Anchor order (gate S0-3 B2): the AR high is searched only after the SC/BC cluster extreme, and
                 ST candidates start after both the AR high and the AR confirmation bar t_ar, so
                 extreme < ar and max(extreme, t_ar) < st on every range.

A range's bounds are frozen when it becomes known (the spec's optional "widen on later pivots" is not
implemented: a documented simplification). A range ends at the close of the first later bar that closes more
than `breakout_atr` ATR outside it, or when it is older than `max_age` bars. Volume gates can be switched off
(`volume_gate=False`) so Stage 0 can measure how selective they are; the volume metrics are always stored.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.research.wyckoff.base import Candles, Event
from app.research.wyckoff.pivots import pivots, trend_context


@dataclass
class TradingRange:
    kind: str                  # 'generic' | 'accumulation' | 'distribution'
    start_idx: int
    known_at: int
    lo: float
    hi: float
    anchors: dict = field(default_factory=dict)
    ref_rv: float | None = None      # RV of the climax bar (SC/BC), selected feed
    ref_rv2: float | None = None     # same, other feed
    end_idx: int | None = None       # bar whose close ends the range
    end_reason: str | None = None    # 'breakout_up' | 'breakdown' | 'age'
    data: dict = field(default_factory=dict)

    def active_at(self, i: int) -> bool:
        """Known and not yet ended at the close of bar i (the ending bar itself counts as active)."""
        return self.known_at <= i and (self.end_idx is None or i <= self.end_idx)


def _close_range(k: Candles, r: TradingRange, breakout_atr: float, max_age: int) -> TradingRange:
    for i in range(r.known_at + 1, len(k)):
        a = k.atrp[i]
        if np.isfinite(a):
            if k.c[i] > r.hi + breakout_atr * a:
                r.end_idx, r.end_reason = i, "breakout_up"
                return r
            if k.c[i] < r.lo - breakout_atr * a:
                r.end_idx, r.end_reason = i, "breakdown"
                return r
        if i - r.known_at >= max_age:
            r.end_idx, r.end_reason = i, "age"
            return r
    return r


def _pivot_index(k: Candles, lag: int):
    piv = pivots(k, lag)
    hi = np.array([e.idx for e in piv if e.kind == "pivot_high"], dtype=int)
    lo = np.array([e.idx for e in piv if e.kind == "pivot_low"], dtype=int)
    return hi, lo


def generic_ranges(k: Candles, window: int = 60, w_min: float = 3.0, w_max: float = 10.0, tol: float = 0.5,
                   slope_max: float = 0.3, lag: int = 3, breakout_atr: float = 2.0, max_age: int = 250) -> list[TradingRange]:
    n = len(k)
    if n < window:
        return []
    top = pd.Series(k.h).rolling(window, min_periods=window).max().to_numpy()
    bot = pd.Series(k.l).rolling(window, min_periods=window).min().to_numpy()
    # OLS slope of closes over the window, from rolling sums (x = 0..W-1 inside each window)
    c = pd.Series(k.c)
    x = pd.Series(np.arange(n, dtype=float))
    sxy = (c * x).rolling(window).sum()
    sy = c.rolling(window).sum()
    sx = x.rolling(window).sum()
    sxx = (x * x).rolling(window).sum()
    slope = ((window * sxy - sx * sy) / (window * sxx - sx * sx)).to_numpy()
    ph, pl = _pivot_index(k, lag)
    out: list[TradingRange] = []
    i = window - 1
    while i < n:
        a = k.atrp[i]
        hgt = top[i] - bot[i]
        ok = np.isfinite(a) and w_min * a <= hgt <= w_max * a and abs(slope[i]) * window <= slope_max * hgt
        if ok:
            lo_i, hi_i = i - window + 1, i - lag            # pivots confirmed by i, inside the window
            hs = ph[(ph >= lo_i) & (ph <= hi_i)]
            ls = pl[(pl >= lo_i) & (pl <= hi_i)]
            n_top = int(np.sum(np.abs(k.h[hs] - top[i]) <= tol * a))
            n_bot = int(np.sum(np.abs(k.l[ls] - bot[i]) <= tol * a))
            if n_top >= 2 and n_bot >= 2:
                r = TradingRange("generic", lo_i, i, float(bot[i]), float(top[i]),
                                 data={"height_atr": float(hgt / a), "n_top": n_top, "n_bot": n_bot})
                _close_range(k, r, breakout_atr, max_age)
                out.append(r)
                if r.end_idx is None:
                    break
                i = r.end_idx + 1
                continue
        i += 1
    return out


CLIMAX_MIN_VALID = 20


def rv_climax(rv_arr: np.ndarray, holiday: np.ndarray, i: int, lookback: int, rv_min: float,
              min_valid: int = CLIMAX_MIN_VALID) -> bool | None:
    """Volume climax on RELATIVE volume only (01 §5.1: never raw volume across bars, which on intraday bars would
    just pick the busiest clock hour). Climax = RV_i >= rv_min, or RV_i is the largest valid (non-holiday) RV of the
    last `lookback` bars when that window holds at least `min_valid` valid values (so thin windows, e.g. right after
    a feed starts or after the 2021 FTMO gap, cannot pass trivially). None if RV_i is undefined; False on holidays."""
    r = float(rv_arr[i])
    if not np.isfinite(r):
        return None
    if holiday[i]:
        return False
    lo = max(0, i - lookback + 1)
    w = rv_arr[lo:i + 1]
    valid = np.isfinite(w) & ~holiday[lo:i + 1]
    is_max = int(valid.sum()) >= min_valid and r >= float(np.max(w[valid]))
    return bool(r >= rv_min or is_max)


def _vol_climax(k: Candles, i: int, lookback: int, rv_min: float) -> tuple[bool, float, float]:
    """(is climax on the selected feed, rv, rv2)."""
    clim = rv_climax(k.rv, k.holiday, i, lookback, rv_min)
    return bool(clim), float(k.rv[i]), float(k.rv2[i])


def event_ranges(k: Candles, side: str = "accumulation", volume_gate: bool = True, lookback: int = 60,
                 k_range: float = 1.5, rv_climax: float = 2.0, ar_move_atr: float = 3.0, ar_retrace: float = 0.33,
                 ar_max_bars: int = 20, st_max_bars: int = 40, st_zone_atr: float = 1.0, st_vol_ratio: float = 0.7,
                 st_undercut_atr: float = 0.0, st_overshoot_atr: float = 1.0, lag: int = 3,
                 breakout_atr: float = 2.0, max_age: int = 250, trend_lookback: int = 60,
                 trend_depth_atr: float = 8.0, cluster_bars: int = 2) -> tuple[list[Event], list[TradingRange]]:
    """Phase-A chains. Returns (events, ranges). Event kinds: accumulation 'sc_candidate', 'ar', 'st';
    distribution 'bc_candidate', 'ar_dist', 'st_dist'. The range is known at the ST confirmation bar.

    st_undercut_atr: 0 = the author's rule (the ST low must stay above the SC low, 上 00:07:06); 0.25 = canon-ish.
    For distribution the ST high may exceed the BC high by up to st_overshoot_atr (the author's cases do)."""
    if side not in ("accumulation", "distribution"):
        raise ValueError(side)
    acc = side == "accumulation"
    sgn = 1 if acc else -1
    # Work in a mirrored price space for distribution so one state machine serves both sides.
    hi_arr, lo_arr, cl_arr, op_arr = (k.h, k.l, k.c, k.o) if acc else (-k.l, -k.h, -k.c, -k.o)
    down, up = trend_context(k, lookback=trend_lookback, depth_atr=trend_depth_atr)
    trend = down if acc else up
    names = ("sc_candidate", "ar", "st") if acc else ("bc_candidate", "ar_dist", "st_dist")
    n = len(k)
    events: list[Event] = []
    ranges: list[TradingRange] = []
    lows60 = pd.Series(lo_arr).rolling(lookback, min_periods=lookback).min().to_numpy()

    def is_climax(i: int):
        a = k.atrp[i]
        if not (np.isfinite(a) and trend[i]):
            return None
        if lo_arr[i] > lows60[i]:            # must be the lookback extreme (low; mirrored high for distribution)
            return None
        if k.rng[i] < k_range * a:
            return None
        if not acc and not (k.c[i] > k.o[i]):  # BC must be a bullish bar
            return None
        clim, r, r2 = _vol_climax(k, i, lookback, rv_climax)
        if volume_gate and not clim:
            return None
        pre = k.rv[max(0, i - 20):i]
        pre_lv = bool(np.isfinite(pre).sum() >= 10 and np.nanmean(pre) < 1.0)
        return {"rv": r, "rv2": r2, "vol_climax": bool(clim), "pre_low_volume": pre_lv}

    i = 0
    state = None
    while i < n:
        a_i = k.atrp[i]
        if state is None:
            info = is_climax(i)
            if info is not None:
                state = {"s": i, "L": lo_arr[i], "info": info, "phase": "sc"}
                events.append(Event(names[0], i, i, sgn, float(k.l[i] if acc else k.h[i]), dict(info)))
            i += 1
            continue
        s, L = state["s"], state["L"]
        if state["phase"] == "sc":
            info = is_climax(i)
            if lo_arr[i] < L:
                if i - s <= cluster_bars:
                    # SC cluster (spec d.2: "a 1-3 bar cluster may replace the single bar"): extend the low in
                    # gated AND ungated runs alike. The volume reference becomes the cluster bar with the highest
                    # RV (so an ungated run can no longer anchor the range on a quiet bar).
                    state["L"], state["low_idx"] = lo_arr[i], i
                    ref = state["info"]["rv"]
                    if np.isfinite(k.rv[i]) and (not np.isfinite(ref) or k.rv[i] > ref):
                        clim, r, r2 = _vol_climax(k, i, lookback, rv_climax)
                        state["info"] = {**state["info"], "rv": r, "rv2": r2,
                                         "vol_climax": bool(clim or state["info"]["vol_climax"])}
                        state["ref_idx"] = i
                    i += 1
                    continue
                state = None                  # a later lower low replaces (if itself a climax) or drops it
                if info is not None:
                    state = {"s": i, "L": lo_arr[i], "info": info, "phase": "sc"}
                    events.append(Event(names[0], i, i, sgn, float(k.l[i] if acc else k.h[i]), dict(info)))
                i += 1
                continue
            if i - s > ar_max_bars:
                state = None
                continue                       # re-test bar i as a fresh candidate
            # S0-3 B2: the AR is searched only after the cluster extreme (d.3 "a = argmax H over (s, t]" with d.2's
            # cluster replacing the single bar), so AR > extreme.
            a0 = state.get("low_idx", s) + 1
            if i < a0:
                i += 1
                continue
            seg = hi_arr[a0:i + 1]
            a_idx = a0 + int(np.argmax(seg))
            H = hi_arr[a_idx]
            atr_s = k.atrp[s]
            move_ok = np.isfinite(atr_s) and (H - L) >= ar_move_atr * atr_s
            piv_ok = i >= a_idx + lag and H > np.max(hi_arr[max(0, a_idx - lag):a_idx], initial=-np.inf) \
                and H >= np.max(hi_arr[a_idx + 1:i + 1], initial=-np.inf)
            retr_ok = cl_arr[i] <= H - ar_retrace * (H - L)
            if move_ok and (piv_ok or retr_ok):
                state.update(phase="ar", a=a_idx, H=H, t_ar=i)
                events.append(Event(names[1], i, a_idx, sgn, float(k.h[a_idx] if acc else k.l[a_idx]),
                                    {"move_atr": float((H - L) / atr_s)}))
            i += 1
            continue
        # phase 'ar': wait for the secondary test. Accumulation: a close below the SC low breaks the chain.
        # Distribution: the ST may poke above the BC (author's cases), so only a close beyond BC + overshoot breaks it.
        undercut = st_undercut_atr if acc else st_overshoot_atr
        tol = 0.0 if acc else st_overshoot_atr * (a_i if np.isfinite(a_i) else 0.0)
        if cl_arr[i] < L - tol or i - state["t_ar"] > st_max_bars:
            state = None
            continue
        a_idx, H = state["a"], state["H"]
        found = None
        # S0-3 B2: ST candidates come after both the AR high and the AR confirmation bar (d.4 "after AR
        # confirmation, bar u"), so ST > max(extreme, t_ar).
        for u in range(max(a_idx + 1, state["t_ar"] + 1, i - lag), i + 1):
            atr_u = k.atrp[u]
            if not np.isfinite(atr_u):
                continue
            in_zone = lo_arr[u] <= L + st_zone_atr * atr_u and lo_arr[u] >= L - undercut * atr_u
            if not in_zone:
                continue
            piv_ok = i == u + lag and lo_arr[u] < np.min(lo_arr[max(0, u - lag):u], initial=np.inf) \
                and lo_arr[u] <= np.min(lo_arr[u + 1:i + 1], initial=np.inf)
            bounce_ok = cl_arr[i] >= lo_arr[u] + 1.0 * atr_u
            if piv_ok or bounce_ok:
                found = u
                break
        if found is not None:
            u = found
            leg = k.rv[a_idx + 1:u + 1]
            leg2 = k.rv2[a_idx + 1:u + 1]
            ref, ref2 = state["info"]["rv"], state["info"]["rv2"]
            ratio = float(np.nanmax(leg) / ref) if np.isfinite(leg).any() and np.isfinite(ref) and ref > 0 else np.nan
            ratio2 = float(np.nanmax(leg2) / ref2) if np.isfinite(leg2).any() and np.isfinite(ref2) and ref2 > 0 else np.nan
            vol_ok = np.isfinite(ratio) and ratio <= st_vol_ratio
            if volume_gate and not vol_ok:
                i += 1
                continue                       # keep waiting: a later, quieter test may qualify
            events.append(Event(names[2], i, u, sgn, float(k.l[u] if acc else k.h[u]),
                                {"vol_ratio": ratio, "vol_ratio2": ratio2}))
            ext = state.get("low_idx", s)      # the cluster extreme (SC low / BC high)
            if acc:
                lo, hi = float(k.l[ext]), float(k.h[a_idx])
                variants = {"lo_st": float(k.l[u]), "lo_min": float(min(k.l[ext], k.l[u]))}
            else:
                lo, hi = float(k.l[a_idx]), float(max(k.h[ext], k.h[u]))
                variants = {"hi_bc": float(k.h[ext]), "hi_st": float(k.h[u])}
            r = TradingRange(side, s, i, lo, hi, {"climax": s, "extreme": ext, "ref": state.get("ref_idx", s),
                                                  "ar": a_idx, "st": u},
                             state["info"]["rv"], state["info"]["rv2"],
                             data={**variants, "pre_low_volume": state["info"]["pre_low_volume"],
                                   "st_vol_ratio": ratio, "st_vol_ratio2": ratio2})
            _close_range(k, r, breakout_atr, max_age)
            ranges.append(r)
            state = None
            if r.end_idx is None:
                break
            i = r.end_idx + 1
            continue
        i += 1
    return events, ranges
