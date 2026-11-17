# Ported from wyk/detectors/distribution.py @ fc142999 (user's own code).
"""Distribution Phase C/D detectors (docs/01 §5.2-5.3 and §4 X2-X5; full drafts in docs/evidence/raw/notes.md
d.12 UT/UTAD, d.13 SOW, d.14 LPSY, d.19 three-level exit).

The author uses these events only to EXIT longs (下 00:22:10-26; d.19): L1 = UT fall-back (tighten the stop, no new
longs), L2 = SOW (exit most), L3 = LPSY_D trigger (exit the rest); variant: exit everything at LPSY_C. Shorting on
them is WH5b, an extension that needs its own spec (DEC-5). So these are side-agnostic signals with direction -1
(bearish / lower side). The two "failed" kinds say the bearish reading failed and carry direction +1.

Public functions (each returns list[Event] sorted by (known_at, idx, kind, range identity), deterministic):

upthrusts            d.12, X2, X3. On an active distribution or generic range: break bar b with
                     H_b > TR_hi + delta ATR (previous close <= TR_hi). Kind 'ut' = first close < TR_hi within J_u
                     bars ((r - b + 1) <= J_u), idx = argmax H over [b, r], known at r. Kind 'ut_failed' = no
                     fall-back by b + J_u - 1 (the breakout held), idx = b, known at b + J_u - 1. J_u = 3 is the
                     author's 1-3 bars (N6); J_u = 10 is the long-window variant (X3, weekly case) - call twice.
                     X2 variants are both stored: 'hv' (RV of the break bar >= 1.5) and 'lv' (RV of the break bar
                     < 1, d.12 "RV_b < 1", or RV at the UT high < RV at the previous confirmed pivot high inside the
                     range); also as gate flags hv_ok / lv_ok (+ '2' feed versions) for Stage 0.
utad_confirmations   d.12 feature 3, causal proxy for "反弹无力,无法再创新高": after a 'ut' known at r, a pivot
                     high x > r with H_x < UT high, confirmed at x + lag, then a close below L_{x+1}. Kind
                     'utad_confirmed', idx = x, known at that close. Cancelled if any bar after r reaches the UT
                     high; expires `expiry` (30) bars after r.
sow_events           d.13, X5. Kind 'sow' (SOW_break: close < ice - delta ATR; bear body >= 0.8 ATR; CLV <= 0.3;
                     RV >= 1.5 stored; 1-3 bar aggregate), kind 'sow_failed' (d.13 invalidation: a close
                     > ice + eps ATR within m_fail bars) and kind 'sow_minor' (mSOW: a wide 1-3 bar down-leg whose
                     low reaches within 0.5 ATR of the ice without any close below it). Ice = TR_lo (horizontal).
lpsy_events          d.14, X4. Kind 'lpsy_d' (after a 'sow': a rally with H >= ice - z ATR and no close
                     > ice + eps ATR, then a close below the min low of the last 2 bars; up to 2 retests; expiry
                     20 bars after the SOW) and kind 'lpsy_c' (after a 'ut' fall-back on a distribution range: a
                     rally into [H_bc - 0.5 ATR, H_ut) on an effort-without-result bar).
distribution_events  runs all of the above and returns the merged sorted list.

Rules every function here follows:
- Causality. A decision at the close of bar i reads arrays at indices <= i only. Every Event has idx (happens@)
  and known_at (the close that makes it knowable, >= idx); nothing in Event.data depends on bars after known_at.
  A range's lifetime is read only through r.active_at(i) at the bar being decided (never r.end_idx /
  r.end_reason, which ranges.py computes over the whole series). Every event copies its range's identity and
  levels (range_kind, range_start, range_known_at, range_lo, range_hi, hi_bc = r.data['hi_bc'] or TR_hi when
  the range has no BC), so the follow-on detectors (utad_confirmations, LPSY_D) never need the live range: the
  LPSY_D retest usually comes after the range has ended by breakdown. Pivots are used only once confirmed (pivot idx + lag <= the deciding bar). A setup still
  open at the end of the data emits nothing.
- Normalisers. ATR = k.atrp (ATR through i-1). An m-bar aggregate starting at s0 is normalised by atrp[s0], so the
  group's own ranges never scale its own test. CLV = (C-L)/(H-L); NaN when H == L (fails every CLV test).
- Volume. Never a hard filter unless volume_gate=True. Metrics are stored for both feeds (rv/rv2, ae/ae2, ratios)
  with flags vol_ok (selected feed) and vol_ok2 (other feed). Cross-bar comparisons are RV ratios, never raw
  volumes. AE is a z-score and is stored raw. Holidays (k.holiday, CME calendar + short session): a LOW-volume
  condition (UT 'lv', LPSY_D rally RV) is False if any bar in its window is a holiday or short session, and the
  event records 'holiday': True. High-volume conditions (UT 'hv', SOW RV, LPSY_C effort) only record the flag.
  The UT volume-reference pivot skips k.holiday bars. s03-fix F1: gate S0-3 NSU-1 lists the readers of
  k.holiday_lowvol (CME OR England & Wales bank holiday) exhaustively and its UT row says "No change to
  upthrusts()", so UT and LPSY_D keep the CME-only veto; 'holiday_lowvol' is stored on both as a DESCRIPTIVE
  field only (it gates nothing here).
  With volume_gate=True an event that fails its volume condition is suppressed and scanning continues, i.e. the
  gated event is "the first candidate meeting every condition including volume" (as in accumulation.py and the
  ST gate of ranges.event_ranges). The 'ut' gate uses `variant` ('hv' default, or 'lv'); 'ut_failed',
  'sow_failed' and 'utad_confirmed' have no volume condition and are never gated.

Choices the spec leaves open (documented here; Stage 0 may revisit them without looking at outcomes):
- UT break bar: H_b > TR_hi + delta ATR_b with C_{b-1} <= TR_hi (a break starts from inside, so a stay above the
  range is one break, not a cascade). One break at a time per range; after a 'ut' or 'ut_failed' scanning resumes
  at the next bar while the range is active. A sequence that started on an active range runs to its resolution
  even if ranges.py ends the range inside the J_u window (the X3 long window needs exactly that).
- UT preconditions (d.12 "bars since ST >= B_min"): event ranges need b - known_at >= max(b_min, Phase-A length)
  (b_min_phase_a=True, the d.5 default B_min = max(10, Phase-A length)); generic ranges need b_min_generic (0)
  because their window is already a range. Note: accumulation.springs defaults to b_min_phase_a=False.
- UT 'lv' second clause: the previous pivot high is the latest confirmed pivot high (confirmed by the break bar)
  with idx in [range start, b) that is not a holiday (a holiday is never a volume reference). Its RV is compared
  with the RV of the UT-high bar. Holidays: the first clause is False if the break bar is a holiday, the second if
  the UT-high bar is; 'holiday' = either (holiday_break / holiday_high stored).
- UTAD confirmation: the lower high must come strictly after the fall-back bar; the most recent confirmed one is
  used; the trigger may fire on the pivot's confirmation bar. A bar with H >= UT high cancels it.
- SOW: one SOW per range at a time; smallest aggregate first; aggregate RV = mean RV of the group's bars. After a
  SOW the next m_fail bars are watched for 'sow_failed'; if it fails, the range is re-scanned (while still
  active); if it holds, the range is done. Sloping ice lines (d.13 option) are not implemented.
- SOW minor (X5): same body / CLV / RV tests as SOW_break; the group's low must reach within near_atr ATR of the
  ice (an intraday pierce is allowed) and no close of the group may be below the ice. After one fires, the next
  needs a close >= ice + rearm_atr ATR first (otherwise every bar hovering at the ice would fire).
- LPSY_D: the touch is the first bar after the anchor (the SOW, then the previous trigger) with H >= ice - z ATR
  whose low is above the lowest low since the anchor, i.e. a bar of a rally, not a bar still extending the
  decline (without this, the bar after a SOW that closed just under the ice "touches" and then "triggers"). The
  rally runs from that lowest low to the highest high up to the trigger; idx = the rally high. The touch bar
  itself may be the trigger. A close > ice + eps ATR ends the setup for that SOW. The 20-bar expiry is counted
  from the SOW for both retests.
- LPSY_C: needs a close below the zone (H_bc - 0.5 ATR) after the fall-back close before a bar can "rally into"
  the zone; a bar with H >= UT high cancels it; expiry 20 bars after the fall-back; one per UT. Without the volume
  gate only the result condition is required (stall or upper wick); the effort condition is the gate.
"""

import numpy as np

from app.research.wyckoff.base import Candles, Event
from app.research.wyckoff.pivots import pivots
from app.research.wyckoff.ranges import TradingRange

DIST_KINDS = ("distribution", "generic")
PHASE_A_KINDS = ("accumulation", "distribution")        # event ranges (have a Phase A and a climax)
RANGE_FIELDS = ("range_kind", "range_start", "range_known_at", "range_lo", "range_hi", "hi_bc")


# ---------------------------------------------------------------- helpers

def _f(x) -> float:
    """Plain float (NaN for None / non-finite input), so Event.data holds plain values only."""
    if x is None:
        return float("nan")
    x = float(x)
    return x if np.isfinite(x) else float("nan")


def _ratio(a, b) -> float:
    a, b = _f(a), _f(b)
    return a / b if np.isfinite(a) and np.isfinite(b) and b > 0 else float("nan")


def _nanmean(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    return float(np.nanmean(x)) if np.isfinite(x).any() else float("nan")


def _bar_vol(k: Candles, i: int, suffix: str = "") -> dict:
    return {f"rv{suffix}": _f(k.rv[i]), f"rv2{suffix}": _f(k.rv2[i]),
            f"ae{suffix}": _f(k.ae[i]), f"ae2{suffix}": _f(k.ae2[i])}


def _le(x: float, thr: float) -> bool:
    return bool(np.isfinite(x) and x <= thr)


def _lt(x: float, thr: float) -> bool:
    return bool(np.isfinite(x) and x < thr)


def _ge(x: float, thr: float) -> bool:
    return bool(np.isfinite(x) and x >= thr)


def _ok_atr(a) -> bool:
    return bool(np.isfinite(a) and a > 0)


def _which(**flags: bool) -> str:
    """Names of the conditions that fired, joined by '+' ('none' if none did)."""
    return "+".join(name for name, on in flags.items() if on) or "none"


def range_key(r: TradingRange) -> tuple:
    return (r.kind, int(r.start_idx), int(r.known_at))


def _range_info(r: TradingRange) -> dict:
    return {"range_kind": r.kind, "range_start": int(r.start_idx), "range_known_at": int(r.known_at),
            "range_lo": float(r.lo), "range_hi": float(r.hi), "hi_bc": float(r.data.get("hi_bc", r.hi))}


def _event_range_key(e: Event) -> tuple:
    return (e.data.get("range_kind"), e.data.get("range_start"), e.data.get("range_known_at"))


def _sort(events: list[Event]) -> list[Event]:
    def key(e: Event):
        d = e.data
        return (e.known_at, e.idx, e.kind, str(d.get("range_kind", "")), d.get("range_start", -1),
                d.get("range_known_at", -1), d.get("ut_idx", -1), d.get("sow_idx", -1))
    return sorted(events, key=key)


# ---------------------------------------------------------------- d.12 UT / UTAD

def upthrusts(k: Candles, ranges: list[TradingRange], J_u: int = 3, delta: float = 0.0, rv_hv: float = 1.5,
              rv_lv: float = 1.0, b_min: int = 10, b_min_generic: int = 0, b_min_phase_a: bool = True,
              lag: int = 3, kinds: tuple = DIST_KINDS, variant: str = "hv",
              volume_gate: bool = False) -> list[Event]:
    """Upthrusts ('ut') and held breakouts ('ut_failed') on every range whose kind is in `kinds`.

    J_u: fall-back window, (r - b + 1) <= J_u (3 = author, 10 = long-window variant; sweep 1-5).
    rv_hv: break-bar RV for the 'hv' flag (UTAD_hv). rv_lv: break-bar RV for the 'lv' flag (UT_lv, d.12 RV_b < 1),
    which is also set when the RV at the UT high is below the RV of the previous confirmed pivot high inside the range.
    variant: which flag is vol_ok / the gate ('hv' or 'lv'). lag: pivot confirmation lag for that pivot."""
    if J_u < 1:
        raise ValueError("J_u must be >= 1")
    if variant not in ("hv", "lv"):
        raise ValueError(variant)
    out: list[Event] = []
    n = len(k)
    ph = np.array(sorted(e.idx for e in pivots(k, lag) if e.kind == "pivot_high"), dtype=int)
    for r in ranges:
        if r.kind not in kinds:
            continue
        ev_range = r.kind in PHASE_A_KINDS
        phase_a = int(r.known_at - r.start_idx) if ev_range else -1
        bmin = (max(b_min, phase_a) if b_min_phase_a else b_min) if ev_range else b_min_generic
        hi = float(r.hi)
        info = _range_info(r)
        t = max(int(r.known_at) + max(bmin, 1), 1)
        seq_no = 0
        while t < n and r.active_at(t):
            a = k.atrp[t]
            if not (_ok_atr(a) and k.h[t] > hi + delta * a and k.c[t - 1] <= hi):
                t += 1
                continue
            b = t
            end = None
            for u in range(b, min(b + J_u - 1, n - 1) + 1):
                if k.c[u] < hi:
                    end = u
                    break
            if end is None:
                if b + J_u - 1 > n - 1:
                    break                                  # unresolved at the end of the data: nothing known yet
                kind, end = "ut_failed", b + J_u - 1
            else:
                kind = "ut"
            x = b + int(np.argmax(k.h[b:end + 1]))         # the UT high
            hmax = float(k.h[x])
            # review fix: a holiday / short session is never a volume reference (base.py), so a holiday pivot high
            # is skipped and the latest confirmed non-holiday pivot high is the reference
            prev = ph[(ph >= r.start_idx) & (ph < b) & (ph + lag <= b) & ~k.holiday[ph]]
            p = int(prev[-1]) if len(prev) else -1
            vb, vx = _bar_vol(k, b), _bar_vol(k, x, "_high")
            rv_p = _f(k.rv[p]) if p >= 0 else float("nan")
            rv2_p = _f(k.rv2[p]) if p >= 0 else float("nan")
            ratio, ratio2 = _ratio(vx["rv_high"], rv_p), _ratio(vx["rv2_high"], rv2_p)
            hol_b, hol_x = bool(k.holiday[b]), bool(k.holiday[x])
            hol = hol_b or hol_x
            # s03-fix F1: UT low-volume clauses keep the CME-only veto (gate NSU-1 does not list UT; UT-1/2/3/5 row:
            # "No change to upthrusts()"); holiday_lowvol is stored below as a descriptive field only.
            lv_b, lv_x = bool(k.holiday_lowvol[b]), bool(k.holiday_lowvol[x])
            # review fix: d.12 UT_lv clause 1 is "new high with RV_b < 1", i.e. the BREAK bar's RV (it read the UT-high
            # bar's RV, which differs whenever the high comes after the break bar). The UT-high-bar version is kept
            # as lv_rv_high (stored only, not part of lv). Clause 2 compares the UT-high bar with the previous pivot.
            lv_rv, lv_rv2 = _lt(vb["rv"], rv_lv) and not hol_b, _lt(vb["rv2"], rv_lv) and not hol_b
            lv_rh, lv_rh2 = _lt(vx["rv_high"], rv_lv) and not hol_x, _lt(vx["rv2_high"], rv_lv) and not hol_x
            lv_pv, lv_pv2 = _lt(ratio, 1.0) and not hol_x, _lt(ratio2, 1.0) and not hol_x
            hv, hv2 = _ge(vb["rv"], rv_hv), _ge(vb["rv2"], rv_hv)
            lv, lv2 = lv_rv or lv_pv, lv_rv2 or lv_pv2
            vol_ok, vol_ok2 = (hv, hv2) if variant == "hv" else (lv, lv2)
            data = {**info, "J_u": int(J_u), "break_idx": b, "fallback_idx": end if kind == "ut" else -1,
                    "high_idx": x, "ut_high": hmax, "seq_no": seq_no, "phase_a_bars": phase_a,
                    "phase_b_bars": int(b - r.known_at), "b_min": int(bmin), "atr": float(a),
                    "penetration_atr": float((hmax - hi) / a), "break_close_vs_hi_atr": float((k.c[b] - hi) / a),
                    "bars_outside": int(end - b + 1), "end_close_vs_hi_atr": float((k.c[end] - hi) / a),
                    **vb, **vx, "prev_pivot_idx": p, "rv_prev_pivot": rv_p, "rv2_prev_pivot": rv2_p,
                    "rv_ratio_prev_pivot": ratio, "rv2_ratio_prev_pivot": ratio2,
                    "hv": hv, "hv2": hv2, "lv": lv, "lv2": lv2, "lv_rv": lv_rv, "lv_rv2": lv_rv2,
                    "lv_rv_high": lv_rh, "lv_rv2_high": lv_rh2,
                    "lv_pivot": lv_pv, "lv_pivot2": lv_pv2, "variant": variant,
                    # review fix: both X2 variants as '_ok' gate flags, the naming Stage 0 counts gates by
                    # (stage0/runner.py is_flag); 'hv' / 'lv' alone were invisible to its selectivity table
                    "hv_ok": hv, "hv_ok2": hv2, "lv_ok": lv, "lv_ok2": lv2,
                    "vol_ok": vol_ok, "vol_ok2": vol_ok2, "holiday": hol, "holiday_break": hol_b,
                    "holiday_high": hol_x, "holiday_lowvol": lv_b or lv_x}
            if kind == "ut":
                if vol_ok or not volume_gate:
                    out.append(Event("ut", end, x, -1, hmax, data))
            else:
                out.append(Event("ut_failed", end, b, +1, float(k.h[b]), data))
            seq_no += 1
            t = end + 1
    return _sort(out)


def utad_confirmations(k: Candles, uts: list[Event], lag: int = 3, expiry: int = 30) -> list[Event]:
    """'utad_confirmed' after every 'ut' in `uts` (other kinds are ignored): a pivot high x after the fall-back
    bar r with H_x < UT high, known at x + lag, then the first close below L_{x+1} within `expiry` bars of r.
    idx = x, price = H_x, known_at = the close below. No volume condition (the RV at x and at the trigger are
    stored)."""
    out: list[Event] = []
    n = len(k)
    is_ph = np.zeros(n, dtype=bool)
    for e in pivots(k, lag):
        if e.kind == "pivot_high":
            is_ph[e.idx] = True
    for u in uts:
        if u.kind != "ut":
            continue
        r0, hut = int(u.known_at), float(u.price)
        x = -1
        for t in range(r0 + 1, min(r0 + expiry, n - 1) + 1):
            if k.h[t] >= hut:
                break                                      # the rebound reached the UT high: no lower high
            p = t - lag
            if p > r0 and is_ph[p]:
                x = p                                      # H_p < hut: no bar after r0 reached hut
            if x >= 0 and k.c[t] < k.l[x + 1]:
                d = u.data
                ax = k.atrp[x]
                out.append(Event("utad_confirmed", t, x, -1, float(k.h[x]),
                                 {**{f: d[f] for f in RANGE_FIELDS}, "ut_idx": u.idx, "ut_known_at": r0,
                                  "ut_high": hut, "J_u": d["J_u"], "ut_hv": d["hv"], "ut_lv": d["lv"],
                                  "ut_vol_ok": d["vol_ok"], "lower_high_idx": x, "lower_high": float(k.h[x]),
                                  "lower_high_gap_atr": float((hut - k.h[x]) / ax) if _ok_atr(ax) else float("nan"),
                                  "trigger_level": float(k.l[x + 1]), "trigger_close": float(k.c[t]),
                                  "bars_after_ut": int(t - r0), **_bar_vol(k, x, "_high"), **_bar_vol(k, t),
                                  "holiday": bool(k.holiday[t])}))
                break
    return _sort(out)


# ---------------------------------------------------------------- d.13 SOW and ice

def _group(k: Candles, t: int, first: int, max_agg: int, body_min: float, clv_max: float, rv_min: float,
           volume_gate: bool, ice: float | None = None, near_atr: float = 0.0):
    """Smallest bearish aggregate [s0, t] (m = 1..max_agg, s0 >= first) with body O_{s0} - C_t >= body_min ATR,
    CLV <= clv_max and, if volume_gate, mean RV >= rv_min (ATR = atrp[s0]). With `ice` set (minor SOW) the group
    must also have no close below the ice and a low within near_atr ATR of it. Returns a dict or None."""
    for m in range(1, max_agg + 1):
        s0 = t - m + 1
        if s0 < first:
            break
        if ice is not None and bool((k.c[s0:t + 1] < ice).any()):
            break                                          # a close below the ice: not a minor SOW (nor larger m)
        a0 = k.atrp[s0]
        if not _ok_atr(a0):
            continue
        hh, ll = float(k.h[s0:t + 1].max()), float(k.l[s0:t + 1].min())
        if ice is not None and ll > ice + near_atr * a0:
            continue
        clv = float((k.c[t] - ll) / (hh - ll)) if hh > ll else float("nan")
        body = float((k.o[s0] - k.c[t]) / a0)
        rv_g, rv2_g = _nanmean(k.rv[s0:t + 1]), _nanmean(k.rv2[s0:t + 1])
        vol_ok = _ge(rv_g, rv_min)
        if body >= body_min and _le(clv, clv_max) and (vol_ok or not volume_gate):
            return {"agg_bars": m, "agg_start": s0, "atr": float(a0), "body_atr": body, "clv": clv,
                    "leg_low": ll, "low_vs_ice_atr": float("nan") if ice is None else float((ll - ice) / a0),
                    "rv_agg": rv_g, "rv2_agg": rv2_g, "ae_agg": _nanmean(k.ae[s0:t + 1]),
                    "ae2_agg": _nanmean(k.ae2[s0:t + 1]), "vol_ok": vol_ok, "vol_ok2": _ge(rv2_g, rv_min),
                    "holiday": bool(k.holiday[s0:t + 1].any())}
    return None


def sow_events(k: Candles, ranges: list[TradingRange], delta: float = 0.0, body_min: float = 0.8,
               clv_max: float = 0.3, rv_min: float = 1.5, max_agg: int = 3, eps_fail: float = 0.25,
               m_fail: int = 10, minor: bool = True, near_atr: float = 0.5, rearm_atr: float = 1.0,
               kinds: tuple = DIST_KINDS, volume_gate: bool = False) -> list[Event]:
    """SOW_break ('sow'), its invalidation ('sow_failed') and the minor SOW ('sow_minor') on every range whose kind
    is in `kinds`. Ice = TR_lo.

    'sow' at bar t (range active): C_t < ice - delta ATR_t and the smallest bearish group [t-m+1, t] (m <= max_agg,
    starting at or after the range's known_at) passes body_min / clv_max; mean RV >= rv_min is vol_ok.
    idx = known_at = t, price = C_t. 'sow_failed': the first close > ice + eps_fail ATR within m_fail bars.
    'sow_minor' (minor=True): the same group tests, no close of the group below the ice, group low <= ice +
    near_atr ATR; re-armed by a close >= ice + rearm_atr ATR."""
    out: list[Event] = []
    n = len(k)
    for r in ranges:
        if r.kind not in kinds:
            continue
        ice = float(r.lo)
        info = {**_range_info(r), "ice": ice}
        first = int(r.known_at)
        armed_minor = True
        t = first
        while t < n and r.active_at(t):
            a_t = k.atrp[t]
            if not _ok_atr(a_t):
                t += 1
                continue
            if not armed_minor and k.c[t] >= ice + rearm_atr * a_t:
                armed_minor = True
            if k.c[t] < ice - delta * a_t:
                g = _group(k, t, first, max_agg, body_min, clv_max, rv_min, volume_gate)
                if g is None:
                    t += 1
                    continue
                out.append(Event("sow", t, t, -1, float(k.c[t]),
                                 {**info, **g, "phase_b_bars": int(t - r.known_at),
                                  "close_vs_ice_atr": float((k.c[t] - ice) / a_t), **_bar_vol(k, t)}))
                failed = None
                for f in range(t + 1, t + m_fail + 1):
                    if f > n - 1:
                        break
                    a_f = k.atrp[f]
                    if _ok_atr(a_f) and k.c[f] > ice + eps_fail * a_f:
                        failed = f
                        break
                if failed is None:
                    break            # held for m_fail bars (done with this range) or still open at the end of the data
                out.append(Event("sow_failed", failed, failed, +1, float(k.c[failed]),
                                 {**info, "sow_idx": t, "sow_known_at": t, "bars_after_sow": int(failed - t),
                                  "close_vs_ice_atr": float((k.c[failed] - ice) / k.atrp[failed])}))
                t = failed + 1
                continue
            if minor and armed_minor and k.c[t] >= ice:
                g = _group(k, t, first, max_agg, body_min, clv_max, rv_min, volume_gate, ice=ice, near_atr=near_atr)
                if g is not None:
                    out.append(Event("sow_minor", t, t, -1, float(k.c[t]),
                                     {**info, **g, "phase_b_bars": int(t - r.known_at),
                                      "close_vs_ice_atr": float((k.c[t] - ice) / a_t), **_bar_vol(k, t)}))
                    armed_minor = False
            t += 1
    return _sort(out)


# ---------------------------------------------------------------- d.14 LPSY

def _lpsy_d(k: Candles, sows: list[Event], z: float, eps: float, rv_max: float, max_retests: int, expiry: int,
            volume_gate: bool) -> list[Event]:
    out: list[Event] = []
    n = len(k)
    for s in sows:
        if s.kind != "sow":
            continue
        t0, ice = int(s.known_at), float(s.data["ice"])
        info = {f: s.data[f] for f in RANGE_FIELDS}
        anchor, touch, count = t0, -1, 0
        for t in range(t0 + 1, min(t0 + expiry, n - 1) + 1):
            a = k.atrp[t]
            if not _ok_atr(a):
                continue
            if k.c[t] > ice + eps * a:
                break                                      # back above the ice: no LPSY_D for this SOW
            # the touch must be a rally bar: its low is above the lowest low since the anchor (a bar that is still
            # extending the decline, e.g. the bar after a SOW that opens just under the ice, is not a retest)
            if touch < 0 and k.h[t] >= ice - z * a and k.l[t] > k.l[anchor:t].min():
                touch = t
            if touch < 0 or t < 2:
                continue
            lvl = float(min(k.l[t - 1], k.l[t - 2]))
            if not k.c[t] < lvl:
                continue
            lo_i = anchor + int(np.argmin(k.l[anchor:touch]))          # rally low, strictly before the touch
            pk = lo_i + 1 + int(np.argmax(k.h[lo_i + 1:t + 1]))       # rally high, up to the trigger
            rally = np.arange(lo_i + 1, pk + 1)
            rv_m, rv2_m = _nanmean(k.rv[rally]), _nanmean(k.rv2[rally])
            hol = bool(k.holiday[rally].any())
            hol_lv = bool(k.holiday_lowvol[rally].any())   # s03-fix F1: stored, descriptive only (not in NSU-1)
            vol_ok, vol_ok2 = _le(rv_m, rv_max) and not hol, _le(rv2_m, rv_max) and not hol
            if vol_ok or not volume_gate:
                count += 1
                a_pk = k.atrp[pk]
                out.append(Event("lpsy_d", t, pk, -1, float(k.h[pk]),
                                 {**info, "ice": ice, "sow_idx": s.idx, "sow_known_at": t0, "retest": count,
                                  "rally_low_idx": lo_i, "rally_low": float(k.l[lo_i]), "touch_idx": touch,
                                  "rally_high": float(k.h[pk]),
                                  "high_vs_ice_atr": float((k.h[pk] - ice) / a_pk) if _ok_atr(a_pk) else float("nan"),
                                  "rally_atr": float((k.h[pk] - k.l[lo_i]) / a_pk) if _ok_atr(a_pk) else float("nan"),
                                  "rally_bars": int(len(rally)), "rv_mean": rv_m, "rv2_mean": rv2_m,
                                  "ae_mean": _nanmean(k.ae[rally]), "ae2_mean": _nanmean(k.ae2[rally]),
                                  "trigger_level": lvl, "trigger_close": float(k.c[t]),
                                  "bars_since_sow": int(t - t0), **_bar_vol(k, t),
                                  "vol_ok": vol_ok, "vol_ok2": vol_ok2, "holiday": hol,
                                  "holiday_lowvol": hol_lv}))
            anchor, touch = t, -1
            if count >= max_retests:
                break
    return out


def _lpsy_c(k: Candles, uts: list[Event], ranges: list[TradingRange], zone_atr: float, ae_min: float,
            rv_min: float, stall_atr: float, wick_min: float, expiry: int, kinds: tuple,
            volume_gate: bool) -> list[Event]:
    out: list[Event] = []
    n = len(k)
    rmap = {range_key(r): r for r in ranges}
    for u in uts:
        if u.kind != "ut":
            continue
        r = rmap.get(_event_range_key(u))
        if r is None or r.kind not in kinds:
            continue
        hi_bc = float(r.data.get("hi_bc", r.hi))
        hut, t0 = float(u.price), int(u.known_at)
        below = False
        for t in range(t0, min(t0 + expiry, n - 1) + 1):
            a = k.atrp[t]
            ok = _ok_atr(a)
            if t > t0:
                if k.h[t] >= hut:
                    break                                  # UT high taken out: no LPSY_C for this UT
                if below and ok and k.h[t] >= hi_bc - zone_atr * a:
                    dc = float((k.c[t] - k.c[t - 1]) / a)
                    rng = float(k.h[t] - k.l[t])
                    wick_share = float((k.h[t] - max(k.o[t], k.c[t])) / rng) if rng > 0 else float("nan")
                    stall, wick = dc <= stall_atr, _ge(wick_share, wick_min)
                    v = _bar_vol(k, t)
                    e_ae, e_rv = _ge(v["ae"], ae_min), _ge(v["rv"], rv_min)
                    e_ae2, e_rv2 = _ge(v["ae2"], ae_min), _ge(v["rv2"], rv_min)
                    vol_ok, vol_ok2 = e_ae or e_rv, e_ae2 or e_rv2
                    if (stall or wick) and (vol_ok or not volume_gate):
                        d = u.data
                        out.append(Event("lpsy_c", t, t, -1, float(k.h[t]),
                                         {**{f: d[f] for f in RANGE_FIELDS}, "hi_bc": hi_bc, "ut_idx": u.idx,
                                          "ut_known_at": t0, "ut_high": hut, "J_u": d["J_u"], "ut_hv": d["hv"],
                                          "ut_lv": d["lv"], "ut_vol_ok": d["vol_ok"],
                                          "zone_lo": float(hi_bc - zone_atr * a), "bars_after_ut": int(t - t0),
                                          "dc_atr": dc, "stall": bool(stall), "wick_share": wick_share,
                                          "upper_wick": wick, "result": _which(stall=stall, wick=wick),
                                          "effort_ae": e_ae, "effort_rv": e_rv, "effort_ae2": e_ae2,
                                          "effort_rv2": e_rv2, "effort": _which(ae=e_ae, rv=e_rv),
                                          "effort2": _which(ae=e_ae2, rv=e_rv2),
                                          **v, "vol_ok": vol_ok, "vol_ok2": vol_ok2,
                                          "holiday": bool(k.holiday[t])}))
                        break
            if ok and k.c[t] < hi_bc - zone_atr * a:
                below = True
    return out


def lpsy_events(k: Candles, sows: list[Event], uts: list[Event], ranges: list[TradingRange], z: float = 0.75,
                eps: float = 0.25, rv_max: float = 0.8, max_retests: int = 2, expiry: int = 20,
                zone_atr: float = 0.5, ae_min: float = 1.0, rv_min: float = 1.5, stall_atr: float = 0.2,
                wick_min: float = 0.5, expiry_c: int = 20, c_kinds: tuple = ("distribution",),
                volume_gate: bool = False) -> list[Event]:
    """LPSY_D ('lpsy_d', after each 'sow' in `sows`) and LPSY_C ('lpsy_c', after each 'ut' in `uts` whose range in
    `ranges` has a kind in c_kinds).

    LPSY_D: within `expiry` bars of the SOW, a bar with H >= ice - z ATR (the touch), no close > ice + eps ATR (that
    ends the setup), then the first close below min(L_{t-1}, L_{t-2}). Mean RV of the rally bars <= rv_max is
    vol_ok. Up to max_retests per SOW. idx = rally high, price = its high, known_at = trigger close.
    LPSY_C: within expiry_c bars of the UT fall-back, after a close below H_bc - zone_atr ATR, a bar with
    H in [H_bc - zone_atr ATR, H_ut) and result C_t - C_{t-1} <= stall_atr ATR ('stall') or upper wick >= wick_min
    of its range ('wick'); effort AE >= ae_min or RV >= rv_min is vol_ok. H_bc = range.data['hi_bc'] (else
    range.hi). idx = known_at = that bar, price = its high."""
    d = _lpsy_d(k, sows, z, eps, rv_max, max_retests, expiry, volume_gate)
    c = _lpsy_c(k, uts, ranges, zone_atr, ae_min, rv_min, stall_atr, wick_min, expiry_c, c_kinds, volume_gate)
    return _sort(d + c)


# ---------------------------------------------------------------- pipeline

def distribution_events(k: Candles, ranges: list[TradingRange], J_u: int = 3, volume_gate: bool = False,
                        ut_kw: dict | None = None, utad_kw: dict | None = None, sow_kw: dict | None = None,
                        lpsy_kw: dict | None = None) -> list[Event]:
    """upthrusts -> utad_confirmations -> sow_events -> lpsy_events on the same Candles and ranges, merged and
    sorted. One J_u per call (every 3-bar UT is also a 10-bar UT, so the X3 variants are separate calls).
    With volume_gate=True the follow-on detectors see only the gated 'ut' / 'sow' events.
    *_kw override each detector's defaults (parameter sweeps); pass the same lag in ut_kw and utad_kw."""
    ut = upthrusts(k, ranges, J_u=J_u, volume_gate=volume_gate, **(ut_kw or {}))
    uc = utad_confirmations(k, ut, **(utad_kw or {}))
    sw = sow_events(k, ranges, volume_gate=volume_gate, **(sow_kw or {}))
    lp = lpsy_events(k, sw, ut, ranges, volume_gate=volume_gate, **(lpsy_kw or {}))
    return _sort(ut + uc + sw + lp)
