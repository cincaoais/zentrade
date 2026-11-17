# Ported from wyk/detectors/quality.py @ fc142999 (user's own code).
"""Stage 0 calibration S0-1 / S0-2 (docs/research/stage0-report.md §6-7), applied to Phase C/D events.

S0-1  Range quality (notes.md d.1: "require the generic criteria above even when the range came from events").
      A range QUALIFIES at the first bar i >= its known_at at which, using only bars <= i:
        - height (hi - lo) is between w_min and w_max ATR (atrp[i]);
        - >= 2 confirmed pivot highs (known by i, idx in [start, i - lag]) within tol*ATR of hi, and >= 2 confirmed
          pivot lows within tol*ATR of lo;
        - |OLS slope of closes over [start, i]| * length <= slope_max * height.
      An event is quality-OK only if its range qualified strictly BEFORE the event's sequence began (the break bar
      of a spring/upthrust, the first bar of an SOS/SOW aggregate, or the parent's for tests/entries/LPS/LPSY), so
      an event can never certify its own range.
S0-2  One range per side. The rule ACTUALLY implemented (gate review, required change 12): an event on a generic
      range is dropped whenever ANY event-built (Phase-A) range of the module's side is active at the event's
      sequence start, whether or not that Phase-A range has qualified under S0-1. So under an unqualified Phase-A
      range both populations are dropped (the generic event by S0-2, the Phase-A event by S0-1). This is stricter
      than the stage0-report §7 wording ("when they overlap, use the event range") and lowers counts; it is causal
      and truncation-safe. Then events are de-duplicated on (kind, idx, known_at), keeping event-range events first.

Both rules are causal: they read only bars and pivots known by the event's sequence start (<= known_at), and a
range's activity at that bar (r.active_at), which truncation cannot change once the cut is past known_at.

Gate S0-3 (SPR-2/3/8, T3): for springs both rules are also folded into accumulation.springs()'s scan (a sequence
starts only at t > qualified_at, and a generic-range break bar under an active Phase-A range starts nothing), so a
sequence apply_s0 would drop can no longer consume the range's one Phase C. apply_s0 keeps its role for every kind:
it annotates range_qualified_at / range_quality_ok / seq_start and drops / de-duplicates; on springs it now only
annotates and de-duplicates (the fold already removed what S0-1 / S0-2 would drop).
"""

import numpy as np

from app.research.wyckoff.base import Candles, Event
from app.research.wyckoff.pivots import pivots
from app.research.wyckoff.ranges import TradingRange

QUALITY_PARAMS = {"lag": 3, "w_min": 3.0, "w_max": 10.0, "tol": 0.5, "slope_max": 0.3}

# event kind -> (parent id field, parent kinds) for events whose sequence starts at their parent's
_PARENT = {
    "spring_test": ("spring_idx", ("spring",)), "entry_aggressive": ("spring_idx", ("spring",)),
    "entry_steady": ("spring_idx", ("spring",)), "lps": ("sos_idx", ("sos",)), "sos_failed": ("sos_idx", ("sos",)),
    "utad_confirmed": ("ut_idx", ("ut",)), "lpsy_c": ("ut_idx", ("ut",)), "lpsy_d": ("sow_idx", ("sow", "sow_minor")),
    "sow_failed": ("sow_idx", ("sow", "sow_minor")),
}


def _rkey_r(r: TradingRange) -> tuple:
    return (r.kind, int(r.start_idx), int(r.known_at))


def _rkey_e(e: Event) -> tuple | None:
    d = e.data
    if "range_kind" not in d:
        return None
    return (d["range_kind"], int(d["range_start"]), int(d["range_known_at"]))


def range_quality_at(k: Candles, r: TradingRange, i: int, ph: np.ndarray, pl: np.ndarray, lag: int = 3,
                     w_min: float = 3.0, w_max: float = 10.0, tol: float = 0.5, slope_max: float = 0.3) -> bool:
    a = k.atrp[i]
    if not np.isfinite(a) or a <= 0:
        return False
    h = r.hi - r.lo
    if not (w_min * a <= h <= w_max * a):
        return False
    lo_i, hi_i = r.start_idx, i - lag
    hs = ph[(ph >= lo_i) & (ph <= hi_i)]
    ls = pl[(pl >= lo_i) & (pl <= hi_i)]
    if int(np.sum(np.abs(k.h[hs] - r.hi) <= tol * a)) < 2 or int(np.sum(np.abs(k.l[ls] - r.lo) <= tol * a)) < 2:
        return False
    y = k.c[r.start_idx:i + 1]
    m = len(y)
    if m < 3:
        return False
    x = np.arange(m, dtype=float)
    slope = np.polyfit(x, y, 1)[0]
    return bool(abs(slope) * m <= slope_max * h)


def qualified_at(k: Candles, r: TradingRange, ph: np.ndarray, pl: np.ndarray, **params) -> int | None:
    """First bar i in [known_at, end] at which the range passes range_quality_at; None if never (within the data)."""
    last = (r.end_idx if r.end_idx is not None else len(k) - 1)
    for i in range(r.known_at, last + 1):
        if range_quality_at(k, r, i, ph, pl, **params):
            return i
    return None


def pivot_index(k: Candles, lag: int) -> tuple[np.ndarray, np.ndarray]:
    """(pivot-high idx, pivot-low idx) arrays of wyk.detectors.pivots at `lag` (confirmed at idx + lag)."""
    piv = pivots(k, lag)
    ph = np.array([e.idx for e in piv if e.kind == "pivot_high"], dtype=int)
    pl = np.array([e.idx for e in piv if e.kind == "pivot_low"], dtype=int)
    return ph, pl


def qualification_map(k: Candles, ranges: list[TradingRange], params: dict | None = None) -> dict:
    """range key (kind, start_idx, known_at) -> qualified_at (S0-1) or None, for every range given.

    Used by apply_s0 and, since gate S0-3 (T3), by accumulation.springs(), which folds S0-1 into its scan: a spring
    sequence may start only at t > qualified_at. Causal for that use: qualified_at(r) = q is decided on bars <= q,
    so a scan reading it at t > q reads nothing after t; if r qualifies only at or after a cut T, the truncated run
    returns None and the full run's sequences on r all start after T."""
    params = {**QUALITY_PARAMS, **(params or {})}
    ph, pl = pivot_index(k, params["lag"])
    return {_rkey_r(r): qualified_at(k, r, ph, pl, **params) for r in ranges}


def _seq_start(e: Event, parents: dict) -> int:
    d = e.data
    if e.kind in _PARENT:
        field, kinds = _PARENT[e.kind]
        p = parents.get((_rkey_e(e), kinds[0], d.get(field)))
        if p is None and len(kinds) > 1:
            p = parents.get((_rkey_e(e), kinds[1], d.get(field)))
        if p is not None:
            return p
        pidx = d.get(field)
        return int(pidx) if isinstance(pidx, (int, np.integer)) and pidx >= 0 else e.idx
    for f in ("break_idx", "agg_start"):
        v = d.get(f)
        if isinstance(v, (int, np.integer)) and v >= 0:
            return int(v)
    return e.idx


def apply_s0(k: Candles, events: list[Event], ranges: list[TradingRange], drop_unqualified: bool = True,
             one_range_per_side: bool = True, params: dict | None = None) -> list[Event]:
    """Annotate every range-based event with range_qualified_at / range_quality_ok / seq_start, then apply S0-1
    (drop unqualified) and S0-2 (prefer event-built ranges; de-duplicate on (kind, idx)). Events without range
    identity pass through untouched."""
    rmap = {_rkey_r(r): r for r in ranges}
    qa = qualification_map(k, list(rmap.values()), params)
    # parents: (range key, kind, idx) -> sequence start of that parent event
    parents: dict = {}
    for e in events:
        if e.kind in ("spring", "sos", "ut", "sow", "sow_minor"):
            parents[(_rkey_e(e), e.kind, e.idx)] = _seq_start(e, {})
    event_ranges = [r for r in ranges if r.kind != "generic"]
    out: list[Event] = []
    for e in events:
        key = _rkey_e(e)
        if key is None:
            out.append(e)
            continue
        s = _seq_start(e, parents)
        q = qa.get(key)
        ok = q is not None and q < s
        d = {**e.data, "range_qualified_at": -1 if q is None else int(q), "range_quality_ok": bool(ok),
             "seq_start": int(s)}
        if drop_unqualified and not ok:
            continue
        if one_range_per_side and key[0] == "generic" and any(r.active_at(s) for r in event_ranges):
            continue
        out.append(Event(e.kind, e.known_at, e.idx, e.direction, e.price, d))
    if one_range_per_side:
        # De-duplicate on (kind, idx, known_at): two copies of one signal from different ranges. known_at is part of
        # the key so the choice never depends on an event that is not yet known (truncation-safe).
        seen = set()
        prio = sorted(range(len(out)), key=lambda j: (out[j].data.get("range_kind") == "generic", j))
        keep = set()
        for j in prio:
            kk = (out[j].kind, out[j].idx, out[j].known_at)
            if kk in seen and "range_kind" in out[j].data:
                continue
            seen.add(kk)
            keep.add(j)
        out = [out[j] for j in range(len(out)) if j in keep]
    return sorted(out, key=lambda e: (e.known_at, e.idx, e.kind))
