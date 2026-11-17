# Ported from wyk/detectors/accumulation.py @ fc142999 (user's own code).
"""Accumulation Phase C/D detectors (docs/01 §5.2-5.3; full drafts in docs/evidence/raw/notes.md d.5-d.8, d.19).

Public functions (each returns list[Event] sorted by (known_at, idx, kind, range identity), deterministic):

springs        d.5. On an active, S0-1-qualified accumulation or generic range: break bar b, recovery = the FIRST
               close back above TR_lo within J bars (gate S0-3 B1). Kinds 'spring' (that close has CLV >= q; known at
               it) and 'spring_failed' (fail_reason 'weak_close': that first close has CLV < q, known at it;
               'no_recovery': no close back above TR_lo by b+J, known at b+J; 'too_deep': deeper than P_max ATR,
               known at that bar). At most one 'spring' per range; no sequence after a 'no_recovery' / 'too_deep'.
spring_tests   d.6 and d.19. Kinds 'spring_test' (known at the close above the test bar's high), 'entry_aggressive'
               (反包: close above the high of the largest bear bar of the spring sequence) and 'entry_steady'
               (>= 2 closes back inside the range, then the Test-of-Spring trigger).
sos_events     d.7. Kinds 'sos' (SOS_break / JAC, 1-3 bar aggregate allowed; after a spring on the same range, or
               the no-spring type after Phase B >= B_min) and 'sos_failed' (a close back below TR_hi - eps ATR
               within M_fail bars).
lps_events     d.8. Kind 'lps' (post-SOS pullback holding TR_hi, small-step bars, trigger = close above the max
               high of the last n_s bars; expiry M_lps bars -> no event).
accumulation_events  runs all of the above and returns the merged sorted list.

Rules every function here follows:
- Causality. A decision at the close of bar i reads arrays at indices <= i only. Every Event has idx (happens@)
  and known_at (the close that makes it knowable, >= idx); nothing in Event.data depends on bars after known_at.
  A range's lifetime is read only through r.active_at(i) at the bar being decided (never r.end_idx, which
  ranges.py computes over the whole series). 'sos_failed' and 'lps' run after the range has usually ended, so
  they use the range bounds copied into the 'sos' event. A setup still open at the end of the data emits nothing.
- Normalisers. ATR = k.atrp (ATR through i-1). An m-bar SOS aggregate starting at s0 is normalised by atrp[s0], so
  the group's own range never scales its own test. CLV = (C-L)/(H-L); NaN when H == L (fails every CLV test).
- Volume. Never a hard filter unless volume_gate=True. Metrics are stored for both feeds (rv/rv2, ae/ae2, ratios)
  with flags vol_ok (selected feed) and vol_ok2 (other feed), so Stage 0 can count with and without gates.
  Flag names follow the Stage-0 runner contract (wyk/stage0/runner.py): every gate flag is a bool under a key
  ending in '_ok' (selected feed) or '_ok2' (other feed). A spring's gate is the AND of its two selected-feed flags
  vol_ok and break_vol_ok (review fix: the RV_b >= 1.5 veto was not a flag, so Stage 0's flag-based count kept
  springs the gated run drops), so "all selected-feed flags True" on the ungated run selects exactly the springs
  the gated run keeps.
  Cross-bar comparisons are RV ratios, never raw volumes. AE is a z-score and is stored raw, never ratioed.
  Holidays: a low-volume condition over one or more bars is False if ANY bar in its window is a holiday_lowvol
  bar (k.holiday_lowvol: CME holiday / short session OR an England & Wales bank holiday, S0-3 NSU-1). The event
  records 'holiday' (CME-only k.holiday) and 'holiday_lowvol' for that window. This applies to the spring break
  bars, the test-of-spring bar and the LPS pullback. High-volume conditions (SOS) only record 'holiday'.
  With volume_gate=True: a spring that fails its volume condition (vol_ok and not high_volume_break) is
  suppressed while the sequence scanning stays identical to the ungated run; for SOS bars, test-bar candidates and
  LPS triggers the gated event is "the first bar meeting every condition including volume". 'spring_failed' and
  'sos_failed' are never volume-gated. The gated run only ever removes springs (tested); downstream events follow
  from the springs and SOS events they are given.
- Range identity. Every event carries range_kind, range_start, range_known_at, range_lo, range_hi so consumers
  can match springs to SOS events and de-duplicate overlapping accumulation / generic ranges. Distribution ranges
  are ignored.

Choices the spec leaves open (documented here; Stage 0 may revisit them without looking at outcomes):
- Break bar: L_b < TR_lo - delta ATR with the previous close >= TR_lo (a break starts from inside the range, so a
  stay below the range is one break, not a cascade). delta = 0 and no minimum reclaim (gate S0-3 SPR-7, as-is).
- Recovery (gate S0-3 B1, decided; notes d.5 features 2-3): for u = b..b+J in order, (1) deeper than P_max ATR ->
  'too_deep' at u; (2) else the first u with C_u > TR_lo decides: 'spring' if CLV_u >= q, else 'weak_close' at u.
  A weak first reclaim is never relabelled a spring by a later bar (the old rule accepted any close within J).
- Spring eligibility (gate S0-3 SPR-2/3/8/4, TRG-3, SPR-6; decided, no switches). On each range a sequence may
  start only at a break bar t with (a) t > qualified_at(range) (S0-1 folded into the scan; wyk.detectors.quality
  .qualification_map), (b) t >= known_at + B_min, (c) w_min ATR <= TR_hi - TR_lo <= w_max ATR at atrp[t] (3-10,
  QUALITY_PARAMS; pivots and slope are not re-run), (d) on a generic range, no accumulation range in `ranges`
  active at t (S0-2 folded in; r.active_at(t) only). A break failing (a)-(d) is skipped and does not consume the
  range. After a sequence resolves the scan resumes at the next bar, except that it ENDS for that range after
  (e) a 'spring_failed' with 'no_recovery' or 'too_deep' (d.5 "the event is a breakdown"; d.1 invalidation read
  with X = 0 beyond the spring window) and (f) the first 'spring' (one Phase C per range; later lows near it are
  Tests, d.6). 'weak_close' closed back inside, so the scan goes on. The range itself is not ended: ranges.py's
  end_idx (and WH7's breakout) are untouched. (e) and (f) count on the scan, so the volume-gated run, which only
  suppresses emission, stops exactly where the ungated run stops. apply_s0 still annotates springs and
  de-duplicates them across ranges; it no longer drops any (the fold already did).
- Penetration is measured on the deepest low of the sequence so far, in atrp[b] units. A low deeper than P_max ATR
  before recovery ends the sequence as 'spring_failed' with fail_reason 'too_deep' (the breakdown branch).
- The sequence, once started on an active range, runs to its resolution even if ranges.py ends the range inside
  the J window (d.1: the breakdown invalidation applies "beyond the spring window").
- Bars outside the range = [b, r-1] for a spring recovered at r > b, [b] when r == b, [b, known_at] for a failure.
  Accumulation ranges: vol_ratio = max RV over those bars / RV_SC (r.ref_rv). Generic ranges (no SC): the gate is
  rv_break_max <= 0.7 (the break bar's own RV is stored too). Veto flag: RV_b >= 1.5 -> 'high_volume_break';
  break_vol_ok = not high_volume_break; the gated spring needs vol_ok and break_vol_ok. 'spring_failed' is never
  volume-gated.
- B_min (springs, and the no-spring SOS type, d.7 "Phase B >= B_min"): default max(10, Phase-A length) on
  accumulation ranges, the notes d.5 default (Phase A = SC bar -> ST confirmation). b_min_phase_a=False gives a
  flat b_min. Generic ranges have no Phase A and use b_min_generic = 10, the max()'s floor (gate S0-3 TRG-3; was
  0), in springs() and sos_events().
- Test of Spring: candidate bars u in (r, r+M]; a low <= the spring low ends the search; the most recent zone bar is
  the candidate; the trigger is the first later close above its high, inside the same window. Only the close
  trigger is implemented (not the alternative pivot confirmation at u+k), as in 01 §5.2. The volume reference is
  the break bar's RV.
- entry_aggressive: the largest bear bar is the largest (O - C) body in [b, r]; if the sequence has no bear bar the
  break bar's high is used ('bear_bar_found': False). The trigger is the first close above that high at a bar
  t > spring-low bar, t >= the recovery bar (the spring must be known), and t - spring-low bar <= j_agg.
- entry_steady counts closes inside [TR_lo, TR_hi] strictly between the recovery bar and the test trigger. The
  alternative limit order at TR_lo + 0.25 ATR (d.19) is an execution rule and is not emitted here.
- SOS: one SOS per range at a time. After an 'sos_failed' the range is re-scanned while it is still active; after
  M_fail bars without failure the range is done. Aggregate RV = mean RV of the group's bars.
- LPS: the pullback is the bars after the running peak (max high since the SOS bar). The trigger needs n_s small-step
  pullback bars before it, a pullback bar (the trigger bar included) with its low inside
  [TR_hi - eps ATR, TR_hi + 1 ATR], and no close below TR_hi - eps ATR since the SOS (that close ends the setup).
  Pullback volume = mean RV of the pullback bars before the trigger. One LPS per SOS.
"""

import numpy as np

from app.research.wyckoff.base import Candles, Event
from app.research.wyckoff.quality import QUALITY_PARAMS, qualification_map
from app.research.wyckoff.ranges import TradingRange

ACC_KINDS = ("accumulation", "generic")


# ---------------------------------------------------------------- helpers

def _f(x) -> float:
    """Plain float (NaN for None / non-finite input), so Event.data holds plain values only."""
    if x is None:
        return float("nan")
    x = float(x)
    return x if np.isfinite(x) else float("nan")


def _clv(k: Candles, i: int) -> float:
    r = k.h[i] - k.l[i]
    return float((k.c[i] - k.l[i]) / r) if r > 0 else float("nan")


def _ratio(a, b) -> float:
    a, b = _f(a), _f(b)
    return a / b if np.isfinite(a) and np.isfinite(b) and b > 0 else float("nan")


def _nanmax(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    return float(np.nanmax(x)) if np.isfinite(x).any() else float("nan")


def _nanmean(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    return float(np.nanmean(x)) if np.isfinite(x).any() else float("nan")


def _bar_vol(k: Candles, i: int) -> dict:
    return {"rv": _f(k.rv[i]), "rv2": _f(k.rv2[i]), "ae": _f(k.ae[i]), "ae2": _f(k.ae2[i])}


def _le(x: float, thr: float) -> bool:
    return bool(np.isfinite(x) and x <= thr)


def _ge(x: float, thr: float) -> bool:
    return bool(np.isfinite(x) and x >= thr)


def range_key(r: TradingRange) -> tuple:
    return (r.kind, int(r.start_idx), int(r.known_at))


def _range_info(r: TradingRange) -> dict:
    return {"range_kind": r.kind, "range_start": int(r.start_idx), "range_known_at": int(r.known_at),
            "range_lo": float(r.lo), "range_hi": float(r.hi)}


def _event_range_key(e: Event) -> tuple:
    return (e.data.get("range_kind"), e.data.get("range_start"), e.data.get("range_known_at"))


def _sort(events: list[Event]) -> list[Event]:
    def key(e: Event):
        d = e.data
        return (e.known_at, e.idx, e.kind, str(d.get("range_kind", "")), d.get("range_start", -1),
                d.get("range_known_at", -1), d.get("spring_idx", -1), d.get("sos_idx", -1))
    return sorted(events, key=key)


# ---------------------------------------------------------------- d.5 Spring

def springs(k: Candles, ranges: list[TradingRange], delta: float = 0.0, p_max: float = 1.5, b_min: int = 10,
            b_min_generic: int = 10, b_min_phase_a: bool = True, j: int = 2, clv_min: float = 0.5,
            vol_ratio_max: float = 0.7, rv_break_max: float = 0.7, rv_veto: float = 1.5,
            w_min: float = QUALITY_PARAMS["w_min"], w_max: float = QUALITY_PARAMS["w_max"],
            volume_gate: bool = False, qual: dict | None = None) -> list[Event]:
    """Springs and failed springs on every accumulation / generic range.

    b_min: Phase-B bars required after the ST confirmation (range known_at) for accumulation ranges;
    b_min_generic (gate S0-3 TRG-3: 10, d.5's floor; was 0) for generic ranges. b_min_phase_a=True (default;
    review fix, was False) uses max(b_min, Phase-A length), the notes d.5 default that 01 §5 defers to.
    j: recovery window (sweep 0-5). Volume thresholds: vol_ratio_max (RV ratio to the SC, accumulation ranges),
    rv_break_max (generic ranges), rv_veto (break-bar RV for the 'high_volume_break' flag).
    w_min / w_max: the 3-10 ATR height re-checked at the break bar (gate S0-3 SPR-6; atrp[b]).
    qual: range key -> S0-1 qualified_at (None = not qualified). Default None computes it with
    wyk.detectors.quality.qualification_map(k, ranges) at QUALITY_PARAMS; pass a precomputed map only to reuse
    it (it must be the map for these k and ranges, or the fold is not S0-1).

    Eligibility (gate S0-3, see the module docstring): a sequence starts only at t > qualified_at (S0-1 fold), at
    t >= known_at + B_min, with the range 3-10 ATR at the break bar, and on a generic range only if no
    accumulation (Phase-A) range in `ranges` is active at the break bar (S0-2 fold). Recovery is the FIRST close
    above TR_lo (B1). The range's scan ends after its first 'spring' (one Phase C per range) and after a
    'spring_failed' with fail_reason 'no_recovery' or 'too_deep' (a d.5 breakdown); after 'weak_close' it goes on.
    The range itself is not ended (ranges.py's end_idx and WH7 are untouched)."""
    out: list[Event] = []
    n = len(k)
    if qual is None:
        qual = qualification_map(k, [r for r in ranges if r.kind in ACC_KINDS])
    phase_a_ranges = [r for r in ranges if r.kind == "accumulation"]
    for r in ranges:
        if r.kind not in ACC_KINDS:
            continue
        q = qual.get(range_key(r))
        if q is None:
            continue                                       # S0-1: never qualified (within the data)
        acc = r.kind == "accumulation"
        phase_a = int(r.known_at - r.start_idx) if acc else -1      # SC bar -> ST confirmation
        bmin = (max(b_min, phase_a) if b_min_phase_a else b_min) if acc else b_min_generic
        lo = float(r.lo)
        height = float(r.hi - r.lo)
        info = _range_info(r)
        t = max(int(r.known_at) + bmin, int(q) + 1, 1)
        seq_no = n_springs = 0
        while t < n and r.active_at(t):
            a = k.atrp[t]
            if not (np.isfinite(a) and a > 0 and k.l[t] < lo - delta * a and k.c[t - 1] >= lo):
                t += 1
                continue
            # S0-2 fold: a generic-range break under an active Phase-A range starts nothing (read at bar t only).
            # SPR-6: the declared 3-10 ATR context must hold at the break bar. Neither consumes the range.
            if (not acc and any(p.active_at(t) for p in phase_a_ranges)) or not (w_min * a <= height <= w_max * a):
                t += 1
                continue
            b = t
            kind = end = reason = None
            for u in range(b, min(b + j, n - 1) + 1):
                if (lo - k.l[b:u + 1].min()) / a > p_max:
                    kind, end, reason = "spring_failed", u, "too_deep"
                    break
                if k.c[u] > lo:                            # B1: the FIRST close back above TR_lo decides (d.5)
                    if _ge(_clv(k, u), clv_min):
                        kind, end = "spring", u
                    else:
                        kind, end, reason = "spring_failed", u, "weak_close"
                    break
            if kind is None:
                if b + j > n - 1:
                    break                                  # unresolved at the end of the data: nothing known yet
                kind, end, reason = "spring_failed", b + j, "no_recovery"
            low_idx = b + int(np.argmin(k.l[b:end + 1]))
            if kind == "spring":
                outside = list(range(b, end)) if end > b else [b]
            else:
                outside = list(range(b, end + 1))
            vb = _bar_vol(k, b)
            rv_max, rv2_max = _nanmax(k.rv[outside]), _nanmax(k.rv2[outside])
            hol = bool(k.holiday[outside].any())
            hol_lv = bool(k.holiday_lowvol[outside].any())   # S0-3 NSU-1: low-volume conditions read holiday_lowvol
            if acc:
                ratio, ratio2 = _ratio(rv_max, r.ref_rv), _ratio(rv2_max, r.ref_rv2)
                vol_ok, vol_ok2 = _le(ratio, vol_ratio_max), _le(ratio2, vol_ratio_max)
                basis = "rv_ratio_to_sc"
            else:
                ratio = ratio2 = float("nan")
                vol_ok, vol_ok2 = _le(rv_max, rv_break_max), _le(rv2_max, rv_break_max)
                basis = "rv_break"
            vol_ok, vol_ok2 = vol_ok and not hol_lv, vol_ok2 and not hol_lv
            hvb, hvb2 = _ge(vb["rv"], rv_veto), _ge(vb["rv2"], rv_veto)
            bear = [i for i in range(b, end + 1) if k.o[i] > k.c[i]]
            bear_idx = max(bear, key=lambda i: (k.o[i] - k.c[i], -i)) if bear else b
            data = {**info, "break_idx": b, "recovery_idx": end if kind == "spring" else -1,
                    "fail_reason": reason, "seq_no": seq_no, "n_prior_springs": n_springs,
                    "phase_a_bars": phase_a, "phase_b_bars": int(b - r.known_at), "b_min": int(bmin),
                    "atr": float(a), "height_atr": float(height / a), "break_depth_atr": float((lo - k.l[b]) / a),
                    "penetration_atr": float((lo - k.l[low_idx]) / a), "bars_outside": len(outside),
                    "clv": _clv(k, end), **vb,
                    "ae_break_max": _nanmax(k.ae[outside]), "ae2_break_max": _nanmax(k.ae2[outside]),
                    "rv_break_max": rv_max, "rv2_break_max": rv2_max,
                    "ref_rv": _f(r.ref_rv), "ref_rv2": _f(r.ref_rv2),
                    "vol_ratio": ratio, "vol_ratio2": ratio2, "vol_basis": basis,
                    "vol_ok": vol_ok, "vol_ok2": vol_ok2, "holiday": hol, "holiday_lowvol": hol_lv,
                    "high_volume_break": hvb, "high_volume_break2": hvb2,
                    # Review fix: the veto as a Stage-0 gate flag (before, Stage 0 saw only vol_ok, so its
                    # flag-based "gated" count kept springs with RV_b in [1.5, 0.7 * RV_SC] that the gate drops).
                    "break_vol_ok": not hvb, "break_vol_ok2": not hvb2,
                    "bear_bar_idx": int(bear_idx), "bear_bar_found": bool(bear),
                    "bear_bar_high": float(k.h[bear_idx])}
            if kind == "spring":
                if not volume_gate or (vol_ok and not hvb):
                    out.append(Event("spring", end, low_idx, +1, float(k.l[low_idx]), data))
                n_springs += 1
            else:
                out.append(Event("spring_failed", end, low_idx, -1, float(k.l[low_idx]), data))
            seq_no += 1
            t = end + 1
            # SPR-2/3/8: a d.5 breakdown ends the range's spring eligibility; the first spring is its one Phase C.
            # Counted on the scan, not on emission, so the volume-gated run scans exactly like the ungated one.
            if kind == "spring" or reason in ("no_recovery", "too_deep"):
                break
    return _sort(out)


# ---------------------------------------------------------------- d.6 Test of Spring, d.19 entries

def spring_tests(k: Candles, spring_events: list[Event], m_test: int = 15, z_t: float = 1.0,
                 z_lo: float | None = 0.5, min_inside: int = 2, j_agg: int = 2, stop_atr: float = 0.25,
                 volume_gate: bool = False) -> list[Event]:
    """Test of Spring ('spring_test'), aggressive entry ('entry_aggressive') and steady entry ('entry_steady') for
    every 'spring' in spring_events (other kinds are ignored).

    Test bar u in (r, r+m_test]: L_sp < L_u <= L_sp + z_t ATR (01 §5.2), or TR_lo <= L_u <= TR_lo + z_lo ATR
    (notes d.6 weekly-case branch; z_lo=None switches it off; data 'zone' says which branch fired); volume
    RV_u < RV of the spring break bar (stored; gate only with volume_gate). Known at the first later close above H_u
    within the window. entry_steady: the same trigger with >= min_inside closes inside the range between the
    recovery bar and the trigger. entry_aggressive: see the module docstring; j_agg is counted from the spring-low
    bar. stop_ref = spring low - stop_atr ATR; target = TR_hi (01 §5.3)."""
    out: list[Event] = []
    n = len(k)
    for s in spring_events:
        if s.kind != "spring":
            continue
        d = s.data
        r, sp_idx, l_sp = s.known_at, s.idx, float(s.price)
        lo, hi = d["range_lo"], d["range_hi"]
        info = {key: d[key] for key in ("range_kind", "range_start", "range_known_at", "range_lo", "range_hi")}
        base = {**info, "spring_idx": sp_idx, "spring_known_at": r, "spring_low": l_sp,
                "spring_vol_ok": d["vol_ok"], "spring_vol_ok2": d["vol_ok2"],
                "spring_break_vol_ok": d["break_vol_ok"], "spring_break_vol_ok2": d["break_vol_ok2"]}

        # entry_aggressive (反包)
        lvl = d["bear_bar_high"]
        for t in range(max(r, sp_idx + 1), min(sp_idx + j_agg, n - 1) + 1):
            if k.c[t] > lvl:
                a = k.atrp[t]
                out.append(Event("entry_aggressive", t, t, +1, float(k.c[t]),
                                 {**base, "bear_bar_idx": d["bear_bar_idx"], "bear_bar_found": d["bear_bar_found"],
                                  "trigger_level": float(lvl), "trigger_close": float(k.c[t]),
                                  "stop_ref": _f(l_sp - stop_atr * a), "target": float(hi), **_bar_vol(k, t),
                                  "holiday": bool(k.holiday[t])}))
                break

        # Test of Spring and entry_steady
        rv_b, rv2_b = d["rv"], d["rv2"]
        cand = None
        cand_data = None
        for t in range(r + 1, min(r + m_test, n - 1) + 1):
            if k.l[t] <= l_sp:
                break                                      # new low under the spring: no test of this spring
            if cand is not None and k.c[t] > k.h[cand]:
                inside = int(np.sum((k.c[r + 1:t] >= lo) & (k.c[r + 1:t] <= hi)))
                test = Event("spring_test", t, cand, +1, float(k.l[cand]),
                             {**base, **cand_data, "trigger_level": float(k.h[cand]),
                              "trigger_close": float(k.c[t]), "bars_inside_before_trigger": inside})
                out.append(test)
                if inside >= min_inside:
                    a = k.atrp[t]
                    out.append(Event("entry_steady", t, t, +1, float(k.c[t]),
                                     {**test.data, "test_idx": cand, "stop_ref": _f(l_sp - stop_atr * a),
                                      "target": float(hi)}))
                break
            a = k.atrp[t]
            if not (np.isfinite(a) and a > 0):
                continue
            near_sp = l_sp < k.l[t] <= l_sp + z_t * a
            near_lo = z_lo is not None and lo <= k.l[t] <= lo + z_lo * a
            if not (near_sp or near_lo):
                continue
            vb = _bar_vol(k, t)
            ratio, ratio2 = _ratio(vb["rv"], rv_b), _ratio(vb["rv2"], rv2_b)
            hol = bool(k.holiday[t])
            hol_lv = bool(k.holiday_lowvol[t])            # S0-3 NSU-1
            vol_ok = bool(np.isfinite(ratio) and ratio < 1.0 and not hol_lv)
            vol_ok2 = bool(np.isfinite(ratio2) and ratio2 < 1.0 and not hol_lv)
            if volume_gate and not vol_ok:
                continue
            cand = t
            cand_data = {"test_low": float(k.l[t]), "zone": "near_spring_low" if near_sp else "near_tr_lo",
                         "test_vs_spring_low_atr": float((k.l[t] - l_sp) / a),
                         "bars_after_recovery": int(t - r), **vb,
                         "rv_ratio_to_break": ratio, "rv2_ratio_to_break": ratio2,
                         "leg_rv_mean": _nanmean(k.rv[r + 1:t + 1]), "leg_rv2_mean": _nanmean(k.rv2[r + 1:t + 1]),
                         "vol_ok": vol_ok, "vol_ok2": vol_ok2, "holiday": hol, "holiday_lowvol": hol_lv}
    return _sort(out)


# ---------------------------------------------------------------- d.7 SOS / JAC

def sos_events(k: Candles, ranges: list[TradingRange], spring_events: list[Event], delta_b: float = 0.0,
               body_min: float = 0.8, clv_min: float = 0.7, rv_min: float = 1.5, max_agg: int = 3,
               b_min: int = 10, b_min_generic: int = 10, b_min_phase_a: bool = True, eps_fail: float = 0.25,
               m_fail: int = 10, volume_gate: bool = False) -> list[Event]:
    """SOS_break / JAC ('sos') and its invalidation ('sos_failed') on every accumulation / generic range.

    At bar t (range active): C_t > TR_hi + delta_b ATR_t; for the smallest m in 1..max_agg, the group [t-m+1, t]
    has bull body C_t - O_{t-m+1} >= body_min ATR and CLV >= clv_min (ATR = atrp[t-m+1]); mean RV of the group
    >= rv_min is stored as vol_ok. Eligible after a 'spring' on the same range (known by t) or, for the
    no-spring type, when t >= range known_at + B_min (B_min as in springs(): max(b_min, Phase-A length) on
    accumulation ranges unless b_min_phase_a=False). 'sos_failed': the first close < TR_hi - eps_fail ATR within
    m_fail bars after the SOS, known at that close."""
    out: list[Event] = []
    n = len(k)
    by_range: dict[tuple, list[Event]] = {}
    for e in spring_events:
        if e.kind == "spring":
            by_range.setdefault(_event_range_key(e), []).append(e)
    for r in ranges:
        if r.kind not in ACC_KINDS:
            continue
        acc = r.kind == "accumulation"
        phase_a = int(r.known_at - r.start_idx) if acc else -1      # review fix: same B_min rule as springs()
        bmin = (max(b_min, phase_a) if b_min_phase_a else b_min) if acc else b_min_generic
        hi = float(r.hi)
        info = _range_info(r)
        sp = sorted(by_range.get(range_key(r), []), key=lambda e: (e.known_at, e.idx))
        t = int(r.known_at)
        while t < n and r.active_at(t):
            known = [e for e in sp if e.known_at <= t]
            a_t = k.atrp[t]
            eligible = bool(known) or t >= r.known_at + bmin
            if not (eligible and np.isfinite(a_t) and k.c[t] > hi + delta_b * a_t):
                t += 1
                continue
            pick = None
            for m in range(1, max_agg + 1):
                s0 = t - m + 1
                if s0 < r.known_at:
                    break
                a0 = k.atrp[s0]
                if not (np.isfinite(a0) and a0 > 0):
                    continue
                hh, ll = k.h[s0:t + 1].max(), k.l[s0:t + 1].min()
                clv = float((k.c[t] - ll) / (hh - ll)) if hh > ll else float("nan")
                body = float((k.c[t] - k.o[s0]) / a0)
                rv_g, rv2_g = _nanmean(k.rv[s0:t + 1]), _nanmean(k.rv2[s0:t + 1])
                vol_ok = _ge(rv_g, rv_min)
                if body >= body_min and _ge(clv, clv_min) and (vol_ok or not volume_gate):
                    pick = (m, s0, a0, body, clv, rv_g, rv2_g, vol_ok)
                    break
            if pick is None:
                t += 1
                continue
            m, s0, a0, body, clv, rv_g, rv2_g, vol_ok = pick
            last = known[-1] if known else None
            sos = Event("sos", t, t, +1, float(k.c[t]),
                        {**info, "sos_type": "after_spring" if known else "no_spring",
                         "spring_idx": last.idx if last else -1, "spring_known_at": last.known_at if last else -1,
                         "n_springs": len(known), "phase_b_bars": int(t - r.known_at), "b_min": int(bmin),
                         "agg_bars": m,
                         "agg_start": s0, "atr": float(a0), "body_atr": body, "clv": clv,
                         "close_vs_hi_atr": float((k.c[t] - hi) / a_t), **_bar_vol(k, t),
                         "rv_agg": rv_g, "rv2_agg": rv2_g, "ae_agg": _nanmean(k.ae[s0:t + 1]),
                         "ae2_agg": _nanmean(k.ae2[s0:t + 1]), "vol_ok": vol_ok, "vol_ok2": _ge(rv2_g, rv_min),
                         "holiday": bool(k.holiday[s0:t + 1].any())})
            out.append(sos)
            failed = None
            for f in range(t + 1, t + m_fail + 1):
                if f > n - 1:
                    break
                a_f = k.atrp[f]
                if np.isfinite(a_f) and k.c[f] < hi - eps_fail * a_f:
                    failed = f
                    break
            if failed is None:
                break            # held for m_fail bars (done with this range) or still open at the end of the data
            out.append(Event("sos_failed", failed, failed, -1, float(k.c[failed]),
                             {**info, "sos_idx": t, "sos_known_at": t, "bars_after_sos": int(failed - t),
                              "close_vs_hi_atr": float((k.c[failed] - hi) / k.atrp[failed]),
                              **_bar_vol(k, failed), "holiday": bool(k.holiday[failed])}))
            t = failed + 1
    return _sort(out)


# ---------------------------------------------------------------- d.8 LPS

def lps_events(k: Candles, sos_list: list[Event], n_s: int = 3, eps: float = 0.25, zone_up: float = 1.0,
               step_range: float = 0.6, step_body: float = 0.4, rv_max: float = 0.8, m_lps: int = 20,
               stop_atr: float = 0.25, volume_gate: bool = False) -> list[Event]:
    """LPS ('lps') after every 'sos' in sos_list (other kinds are ignored).

    Trigger at bar t in (t0, t0 + m_lps]: the last n_s bars are pullback bars (after the running peak), each with
    range <= step_range ATR and |C-O| <= step_body ATR; C_t > max H of those n_s bars; some pullback bar up to t
    has its low in [TR_hi - eps ATR, TR_hi + zone_up ATR]. A close < TR_hi - eps ATR ends the setup. Mean RV of
    the pullback bars before t <= rv_max is stored as vol_ok. idx = pullback low bar, price = pullback low,
    known_at = trigger bar. stop_ref = min(pullback low, TR_hi - 1 ATR) - stop_atr ATR (01 §5.3)."""
    out: list[Event] = []
    n = len(k)
    for s in sos_list:
        if s.kind != "sos":
            continue
        t0 = s.known_at
        hi = s.data["range_hi"]
        info = {key: s.data[key] for key in ("range_kind", "range_start", "range_known_at", "range_lo", "range_hi")}
        peak = t0
        for t in range(t0 + 1, min(t0 + m_lps, n - 1) + 1):
            a = k.atrp[t]
            ok_a = bool(np.isfinite(a) and a > 0)
            if ok_a and k.c[t] < hi - eps * a:
                break                                      # TR_hi lost: no LPS for this SOS
            p0 = peak + 1
            if ok_a and t - n_s >= p0:
                w = np.arange(t - n_s, t)
                aw = k.atrp[w]
                small = bool(np.all(np.isfinite(aw)) and np.all(k.rng[w] <= step_range * aw)
                             and np.all(np.abs(k.c[w] - k.o[w]) <= step_body * aw))
                lvl = float(k.h[w].max())
                pb = np.arange(p0, t + 1)                  # pullback bars incl. the trigger bar
                ap = k.atrp[pb]
                in_zone = (k.l[pb] >= hi - eps * ap) & (k.l[pb] <= hi + zone_up * ap)
                if small and k.c[t] > lvl and bool(in_zone.any()):
                    pre = np.arange(p0, t)                 # pullback bars before the trigger
                    rv_m, rv2_m = _nanmean(k.rv[pre]), _nanmean(k.rv2[pre])
                    hol = bool(k.holiday[pre].any())
                    hol_lv = bool(k.holiday_lowvol[pre].any())   # S0-3 NSU-1
                    vol_ok = _le(rv_m, rv_max) and not hol_lv
                    vol_ok2 = _le(rv2_m, rv_max) and not hol_lv
                    if vol_ok or not volume_gate:
                        low_idx = p0 + int(np.argmin(k.l[pb]))
                        low = float(k.l[low_idx])
                        rngs = k.h[pre] - k.l[pre]
                        wick = np.minimum(k.o[pre], k.c[pre]) - k.l[pre]
                        hammer = bool(np.any((k.c[pre] > k.o[pre]) & (rngs > 0) & (wick >= 0.5 * rngs)))
                        out.append(Event("lps", t, low_idx, +1, low,
                                         {**info, "sos_idx": s.idx, "sos_known_at": t0, "sos_type": s.data["sos_type"],
                                          "peak_idx": int(peak), "peak_high": float(k.h[peak]),
                                          "pullback_start": int(p0), "pullback_bars": int(t - p0),
                                          "pullback_low": low, "pullback_low_idx": int(low_idx),
                                          "pullback_low_vs_hi_atr": float((low - hi) / a),
                                          "trigger_level": lvl, "trigger_close": float(k.c[t]),
                                          "bars_since_sos": int(t - t0), "rv_mean": rv_m, "rv2_mean": rv2_m,
                                          "ae_mean": _nanmean(k.ae[pre]), "ae2_mean": _nanmean(k.ae2[pre]),
                                          **_bar_vol(k, t), "vol_ok": vol_ok, "vol_ok2": vol_ok2,
                                          "holiday": hol, "holiday_lowvol": hol_lv,
                                          "lower_wick_bull_bar": hammer,
                                          "stop_ref": float(min(low, hi - a) - stop_atr * a)}))
                        break
            if k.h[t] > k.h[peak]:
                peak = t
    return _sort(out)


# ---------------------------------------------------------------- pipeline

def accumulation_events(k: Candles, ranges: list[TradingRange], volume_gate: bool = False,
                        spring_kw: dict | None = None, test_kw: dict | None = None, sos_kw: dict | None = None,
                        lps_kw: dict | None = None) -> list[Event]:
    """springs -> spring_tests -> sos_events -> lps_events on the same Candles and ranges, merged and sorted.
    *_kw override each detector's defaults (parameter sweeps)."""
    sp = springs(k, ranges, volume_gate=volume_gate, **(spring_kw or {}))
    st = spring_tests(k, sp, volume_gate=volume_gate, **(test_kw or {}))
    so = sos_events(k, ranges, sp, volume_gate=volume_gate, **(sos_kw or {}))
    lp = lps_events(k, so, volume_gate=volume_gate, **(lps_kw or {}))
    return _sort(sp + st + so + lp)
