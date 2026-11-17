# Ported from wyk/detectors/effort.py @ fc142999 (user's own code).
"""Effort vs result divergences and the author's change of character (docs/01 §5.2 rows 'Effort vs result (x4)'
and '特性改变'; full drafts in docs/evidence/raw/notes.md d.15 and d.16; 01 wins where the two disagree).

Rules every function here follows (docs/01 §5.1):
- Causality: a decision at the close of bar t reads arrays at indices <= t only. Every Event has idx (happens@)
  and known_at (>= idx); nothing in Event.data depends on a bar after known_at.
- Normaliser: atrp[t] = Wilder ATR through t-1. Volume enters only as RV (or AE), and cross-bar volume
  comparisons are RV ratios, never raw tick counts.
- Volume never filters unless the caller passes volume_gate=True. Every event stores the metrics for BOTH feeds
  (rv/rv2, ae/ae2, ratios) and gate flags ('effort_rv_ok', 'effort_ae_ok', 'effort_rv2_ok', 'effort_ae2_ok',
  'vol_ok', 'vol2_ok', plus sub-flags) so Stage 0 can count with and without the gates. Flags are tri-state, as the
  Stage 0 runner expects: True / False, or None when the metric is unavailable (NaN RV/AE, no prior pivot); 'or'
  combinations use Kleene logic (True if either is True, None if either is None, else False). Every key ending in
  '_ok' is such a flag, and keys ending in '2_ok' belong to the other feed. A holiday_lowvol bar (CME holiday /
  short session OR an England & Wales bank holiday, S0-3 NSU-1) never satisfies a LOW-volume condition: the flag
  is False (not None) and data['holiday_lowvol'] is True. data['holiday'] is the CME-only flag (the one volume
  references use). volume_gate=True keeps only events whose 'vol_ok' is True.
- Output: list[Event] sorted by (known_at, idx, kind order), deterministic.

effort_events (d.15; every kind is known at the close of t, so idx = known_at = t). An event is emitted for every
bar that meets the PRICE and context conditions; the effort test is stored as flags.
  'churn'      放量滞涨, direction -1, price H_t. Context Up_t or H_t >= high20 - near_atr*ATR. Effort RV >= e_hi or
               AE >= ae_hi. Result (C_t - C_{t-1}) <= result_atr*ATR, or upper wick >= wick_share*range.
  'absorption' 放量不跌, direction +1, price L_t. Context Down_t or L_t <= low20 + near_atr*ATR. Effort as churn.
               Result (C_{t-1} - C_t) <= result_atr*ATR, or lower wick >= wick_share*range.
  'no_supply'  缩量下跌跌不动, direction +1, price = low of the last n_low bars. Context Down_t. Effort mean RV of
               the last n_low bars <= e_lo, False if any of them is a holiday_lowvol bar. Result: the window low is
               not below the reference 20-bar low (the 20 bars before the window) by more than undercut_atr*ATR, and
               mean |C_j - C_{j-1}| <= move_atr*ATR. data['holiday'] / data['holiday_lowvol'] = any such bar among
               the n_low bars ('holiday_t' / 'holiday_lowvol_t' = bar t only).
  'no_demand'  缩量上涨无力 / 缩量创新高, direction -1, price H_t. Context: H_t > high20. Result (01 §5.2, S0-3
               NDE-5; the signed form used for churn): (C_t - C_{t-1}) <= result_atr*ATR, or upper wick >=
               wick_share*range. Effort (S0-3 NDE-2, 明显缩量): RV_t <= e_lo, or RV_t <= e_lo * RV_p where p is the
               previous confirmed pivot high (the latest pivot_high with known_at = idx + lag <= t); the same e_lo
               moves both branches; both are False on a holiday_lowvol bar. The AE mirror flags keep AE_t <= -ae_hi
               or AE_t < AE_p (descriptive, never gating).
  high20 / low20 = max H / min L over the ref_n bars BEFORE t (t-ref_n .. t-1); one convention for all kinds.
  vol_ok: churn/absorption = effort_rv_ok or effort_ae_ok (01: "RV or AE extreme"); no_supply/no_demand =
  effort_rv_ok (the spec gives only RV for them). The AE flags of the two low-effort kinds are OUR mirror of the
  +1 sigma rule (mean AE <= -ae_hi, or AE_t <= -ae_hi or AE_t < AE at the pivot); they are stored, never gating.
  Literal results: churn's (C_t - C_{t-1}) <= 0.2 ATR also admits large down closes (absorption mirrors this);
  data['result_atr'] keeps the signed move so Stage 0 can bound it. Every effort event stores result_atr
  ((C_t - C_{t-1}) / ATR), upper_wick_share and lower_wick_share of bar t, holiday, holiday_lowvol and news. d.15's third churn result "next bar closes
  below C_t" is not used: it is known only at t+1.

choch_events (d.16, the author's usage: price now holds near a prior high instead of dropping from it).
  Level H* = the last confirmed swing high that precedes the hold window (level='last'; pivot idx <= t - max(n_h,
  lag), so it is known by t). Variant level='max': the highest such pivot high inside the last `window` bars.
  History: in [t-window+1, s-1] (s = t-n_h+1) at least min_rejections approaches H_j >= H* - approach_atr*ATR_j,
  each followed within reject_bars bars by a low <= H_j - reject_atr*ATR_j, the decline completed before s (so
  known by t). Rejections are counted as episodes: an approach counts only after the previous counted decline.
  Now: for the n_h bars s..t, every close in [H* - band_atr*ATR_i, H*] (hold_on='close'; 'bar' requires the
  whole bar, H_i <= H* and L_i >= H* - band_atr*ATR_i), and no decline from an earlier hold-bar high to a later
  hold-bar low >= hold_decline_atr*ATR. Emitted at the n_h-th bar of a qualifying hold window (idx = known_at =
  t): at most once per run of consecutive hold bars (even if the level pivot changes during the run), at the
  run's first bar whose history test also passes ('run_hold_start' = first bar of the run's first window), and
  only if its newest rejection is newer than the newest rejection of the previous event (a rejection history
  supports one event: a later stay must not recount the same two rejections against a lower minor pivot formed
  during the first stay). Direction +1, price H*. The spec gives no volume input; hold-window volume is stored
  only.
"""

from collections import deque

import numpy as np
import pandas as pd

from app.research.wyckoff.base import Candles, Event
from app.research.wyckoff.pivots import pivots, trend_context

EFFORT_KINDS = ("churn", "absorption", "no_supply", "no_demand")
_ORDER = {name: i for i, name in enumerate(EFFORT_KINDS + ("choch",))}


def _sort(events: list[Event]) -> list[Event]:
    events.sort(key=lambda e: (e.known_at, e.idx, _ORDER.get(e.kind, 99)))
    return events


def _div(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """num / den with NaN wherever den is not a positive finite number."""
    out = np.full(np.broadcast(num, den).shape, np.nan)
    ok = np.isfinite(den) & (den > 0)
    np.divide(num, den, out=out, where=ok)
    return out


def _pivot_high_idx(k: Candles, lag: int) -> np.ndarray:
    return np.array([e.idx for e in pivots(k, lag) if e.kind == "pivot_high"], dtype=np.int64)


def _flag(cond: np.ndarray, valid: np.ndarray, force_false: np.ndarray | None = None) -> np.ndarray:
    """Tri-state flag as float: 1.0 True, 0.0 False, NaN = metric unavailable. force_false (holidays for
    low-volume conditions) overrides to False."""
    f = np.where(valid, cond.astype(float), np.nan)
    return f if force_false is None else np.where(force_false, 0.0, f)


def _or(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Kleene OR of two tri-state flags."""
    return np.where((x == 1.0) | (y == 1.0), 1.0, np.where(np.isnan(x) | np.isnan(y), np.nan, 0.0))


def _emit(kind: str, direction: int, mask: np.ndarray, price: np.ndarray, cols: dict) -> list[Event]:
    """One Event per True in mask (idx = known_at = t). cols: name -> array of length n. Values become plain
    Python types (.tolist()); columns named '*_ok' are tri-state flags and become True / False / None."""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    names = list(cols)
    values = []
    for nm in names:
        col = np.asarray(cols[nm])[idx].tolist()
        if nm.endswith("_ok"):
            col = [None if x != x else bool(x) for x in col]
        values.append(col)
    prices = price[idx].tolist()
    out = []
    for r, t in enumerate(idx.tolist()):
        out.append(Event(kind, t, t, direction, prices[r], {nm: values[q][r] for q, nm in enumerate(names)}))
    return out


def effort_events(k: Candles, e_hi: float = 2.0, e_lo: float = 0.7, n_low: int = 3, ae_hi: float = 1.0,
                  ref_n: int = 20, near_atr: float = 1.0, result_atr: float = 0.2, wick_share: float = 0.5,
                  undercut_atr: float = 0.25, move_atr: float = 0.3, lag: int = 3, trend_lookback: int = 60,
                  trend_depth_atr: float = 8.0, volume_gate: bool = False,
                  kinds: tuple[str, ...] = EFFORT_KINDS) -> list[Event]:
    """The four bar-level effort-vs-result patterns of d.15 (see the module docstring for the exact rules).
    volume_gate=True keeps only events whose primary-feed 'vol_ok' is True. Vectorised: O(n) apart from the pivot
    scan used by 'no_demand'."""
    unknown = set(kinds) - set(EFFORT_KINDS)
    if unknown:
        raise ValueError(sorted(unknown))
    n = len(k)
    if n == 0:
        return []
    h, l, o, c, a = k.h, k.l, k.o, k.c, k.atrp
    down, up = trend_context(k, lookback=trend_lookback, depth_atr=trend_depth_atr)
    hol = k.holiday.astype(bool)
    hol_lv = np.asarray(k.holiday_lowvol, dtype=bool) | hol          # low-volume conditions only (S0-3 NSU-1)
    hs, ls = pd.Series(h), pd.Series(l)
    fin = np.isfinite
    out: list[Event] = []
    with np.errstate(invalid="ignore"):
        ok_a = fin(a) & (a > 0)
        dc = np.r_[np.nan, np.diff(c)]
        high20 = hs.rolling(ref_n, min_periods=ref_n).max().shift(1).to_numpy()
        low20 = ls.rolling(ref_n, min_periods=ref_n).min().shift(1).to_numpy()
        upper_wick = _div(h - np.maximum(o, c), k.rng)
        lower_wick = _div(np.minimum(o, c) - l, k.rng)
        common = {"rv": k.rv, "rv2": k.rv2, "ae": k.ae, "ae2": k.ae2, "atrp": a,
                  "holiday": hol, "holiday_lowvol": hol_lv, "news": k.news.astype(bool),
                  "result_atr": _div(dc, a), "upper_wick_share": upper_wick, "lower_wick_share": lower_wick}

        # ---- high effort: churn (放量滞涨) and absorption (放量不跌); holidays do not matter here ----
        hi_flags = {"effort_rv_ok": _flag(k.rv >= e_hi, fin(k.rv)), "effort_ae_ok": _flag(k.ae >= ae_hi, fin(k.ae)),
                    "effort_rv2_ok": _flag(k.rv2 >= e_hi, fin(k.rv2)),
                    "effort_ae2_ok": _flag(k.ae2 >= ae_hi, fin(k.ae2))}
        hi_flags["vol_ok"] = _or(hi_flags["effort_rv_ok"], hi_flags["effort_ae_ok"])
        hi_flags["vol2_ok"] = _or(hi_flags["effort_rv2_ok"], hi_flags["effort_ae2_ok"])
        # signed one-sided results (d.15 / 01 §5.2): up-move capped (churn, no_demand), down-move capped (absorption)
        res_up_close = dc <= result_atr * a
        res_up_wick = upper_wick >= wick_share

        if "churn" in kinds:
            near = h >= high20 - near_atr * a
            m = ok_a & (up | near) & (res_up_close | res_up_wick)
            if volume_gate:
                m &= hi_flags["vol_ok"] == 1.0
            out += _emit("churn", -1, m, h, {
                **common, **hi_flags, "ctx_trend": up, "ctx_near_high": near, "high20": high20,
                "dist_high_atr": _div(high20 - h, a), "result_close": res_up_close, "result_wick": res_up_wick})

        if "absorption" in kinds:
            near = l <= low20 + near_atr * a
            res_close = -dc <= result_atr * a
            res_wick = lower_wick >= wick_share
            m = ok_a & (down | near) & (res_close | res_wick)
            if volume_gate:
                m &= hi_flags["vol_ok"] == 1.0
            out += _emit("absorption", +1, m, l, {
                **common, **hi_flags, "ctx_trend": down, "ctx_near_low": near, "low20": low20,
                "dist_low_atr": _div(l - low20, a), "result_close": res_close, "result_wick": res_wick})

        # ---- low effort: no supply (缩量下跌跌不动) ----
        if "no_supply" in kinds:
            w = n_low

            def wmean(x):            # mean over the last w bars; NaN if any of them is NaN
                return pd.Series(x).rolling(w, min_periods=w).mean().to_numpy()

            mrv, mrv2, mae, mae2 = wmean(k.rv), wmean(k.rv2), wmean(k.ae), wmean(k.ae2)
            hol_w = pd.Series(hol.astype(float)).rolling(w, min_periods=1).max().to_numpy() > 0
            hol_lv_w = pd.Series(hol_lv.astype(float)).rolling(w, min_periods=1).max().to_numpy() > 0
            win_low = ls.rolling(w, min_periods=w).min().to_numpy()
            ref_low = ls.rolling(ref_n, min_periods=ref_n).min().shift(w).to_numpy()   # L[t-w-ref_n+1 .. t-w]
            mabs = pd.Series(np.abs(dc)).rolling(w, min_periods=w).mean().to_numpy()
            held = win_low >= ref_low - undercut_atr * a
            quiet = mabs <= move_atr * a
            m = ok_a & down & held & quiet
            lo_flags = {"effort_rv_ok": _flag(mrv <= e_lo, fin(mrv), hol_lv_w),
                        "effort_ae_ok": _flag(mae <= -ae_hi, fin(mae), hol_lv_w),
                        "effort_rv2_ok": _flag(mrv2 <= e_lo, fin(mrv2), hol_lv_w),
                        "effort_ae2_ok": _flag(mae2 <= -ae_hi, fin(mae2), hol_lv_w)}
            lo_flags["vol_ok"] = lo_flags["effort_rv_ok"]
            lo_flags["vol2_ok"] = lo_flags["effort_rv2_ok"]
            if volume_gate:
                m &= lo_flags["vol_ok"] == 1.0
            out += _emit("no_supply", +1, m, win_low, {
                **common, "holiday": hol_w, "holiday_t": hol, "holiday_lowvol": hol_lv_w,
                "holiday_lowvol_t": hol_lv, **lo_flags,
                "mean_rv": mrv, "mean_rv2": mrv2, "mean_ae": mae, "mean_ae2": mae2,
                "window_start": np.arange(n) - (w - 1), "ref_low": ref_low,
                "undercut_atr": _div(ref_low - win_low, a), "mean_abs_dc_atr": _div(mabs, a)})

        # ---- low effort at a new high: no demand (缩量上涨无力 / 缩量创新高) ----
        if "no_demand" in kinds:
            ph = _pivot_high_idx(k, lag)
            last = np.full(n, -1, dtype=np.int64)
            last[ph + lag] = ph                       # each pivot high becomes known at idx + lag
            last = np.maximum.accumulate(last)        # latest pivot high with known_at <= t
            has = last >= 0
            p = np.where(has, last, 0)

            def at_p(x):
                return np.where(has, x[p], np.nan)

            sub = {}
            for name, x in (("rv", k.rv), ("rv2", k.rv2), ("ae", k.ae), ("ae2", k.ae2)):
                xp = at_p(x)
                if name.startswith("rv"):           # NDE-2: 明显缩量 = clearly lower, the same e_lo in both branches
                    low, below = x <= e_lo, x <= e_lo * xp
                else:                               # AE mirror (descriptive, never gating): unchanged
                    low, below = x <= -ae_hi, x < xp
                sub[f"low_{name}_ok"] = _flag(low, fin(x), hol_lv)
                sub[f"below_prev_{name}_ok"] = _flag(below, fin(x) & fin(xp), hol_lv)
                sub[f"{name}_prev_high"] = xp
            nd_flags = {f"effort_{nm}_ok": _or(sub[f"low_{nm}_ok"], sub[f"below_prev_{nm}_ok"])
                        for nm in ("rv", "ae", "rv2", "ae2")}
            nd_flags["vol_ok"] = nd_flags["effort_rv_ok"]
            nd_flags["vol2_ok"] = nd_flags["effort_rv2_ok"]
            m = ok_a & (h > high20) & (res_up_close | res_up_wick)          # NDE-5: 01 §5.2 result clause
            if volume_gate:
                m &= nd_flags["vol_ok"] == 1.0
            out += _emit("no_demand", -1, m, h, {
                **common, **nd_flags, **sub, "high20": high20, "new_high_atr": _div(h - high20, a),
                "result_close": res_up_close, "result_wick": res_up_wick,
                "prev_high_idx": last, "prev_high_price": at_p(h), "prev_high_holiday": has & hol[p],
                "rv_ratio_prev": _div(k.rv, sub["rv_prev_high"]),
                "rv2_ratio_prev": _div(k.rv2, sub["rv2_prev_high"])})
    return _sort(out)


def _level_index(n: int, ph: np.ndarray, h: np.ndarray, m: int, level: str, window: int) -> np.ndarray:
    """p[t] = index of the level pivot for bar t, -1 if none. Only pivot highs with idx <= t - m are eligible
    (m >= lag, so each is known by t, and each precedes the n_h-bar hold window)."""
    p = np.full(n, -1, dtype=np.int64)
    if len(ph) == 0:
        return p
    if level == "last":
        last = np.full(n, -1, dtype=np.int64)
        last[ph] = ph
        last = np.maximum.accumulate(last)
        if n > m:
            p[m:] = last[:n - m]
        return p
    # level == 'max': highest pivot high with idx in [t-window+1, t-m]; the latest one on ties
    dq: deque = deque()
    ptr = 0
    for t in range(n):
        while ptr < len(ph) and ph[ptr] <= t - m:
            j = int(ph[ptr])
            while dq and h[dq[-1]] <= h[j]:
                dq.pop()
            dq.append(j)
            ptr += 1
        while dq and dq[0] < t - window + 1:
            dq.popleft()
        if dq:
            p[t] = dq[0]
    return p


def choch_events(k: Candles, lag: int = 3, window: int = 60, approach_atr: float = 0.5,
                 approach_above_atr: float | None = None, reject_bars: int = 5, reject_atr: float = 2.0,
                 min_rejections: int = 2, band_atr: float = 1.0, n_h: int = 5, hold_decline_atr: float = 1.0,
                 hold_on: str = "close", level: str = "last") -> list[Event]:
    """The author's 特性改变 (d.16), bullish case only. See the module docstring for the exact rule.
    approach_above_atr: None = the literal one-sided approach test (H_j >= H* - approach_atr*ATR); a number also
    caps H_j <= H* + approach_above_atr*ATR (rejections 'at' the level)."""
    if hold_on not in ("close", "bar"):
        raise ValueError(hold_on)
    if level not in ("last", "max"):
        raise ValueError(level)
    n = len(k)
    if n < n_h + 1:
        return []
    h, l, c, a = k.h, k.l, k.c, k.atrp
    m = max(n_h, lag)
    p = _level_index(n, _pivot_high_idx(k, lag), h, m, level, window)
    hstar = np.where(p >= 0, h[np.maximum(p, 0)], np.nan)

    with np.errstate(invalid="ignore"):
        # dec_end[j]: first bar d in (j, j+reject_bars] with L_d <= H_j - reject_atr*ATR_j (else -1). It is read
        # only where dec_end[j] <= s-1 < t, so it never brings a bar after t into a decision at t.
        thr = h - reject_atr * a
        dec_end = np.full(n, -1, dtype=np.int64)
        for off in range(reject_bars, 0, -1):            # far to near, so the nearest hit wins
            if off >= n:                                  # review fix: a series shorter than the offset made the
                continue                                  # slices below disagree in length (ValueError on k.head)
            hit = np.zeros(n, dtype=bool)
            hit[:n - off] = l[off:] <= thr[:n - off]
            dec_end[hit] = np.flatnonzero(hit) + off

        # hold window s..t (rows of the sliding views end at t = row + n_h - 1)
        sw = np.lib.stride_tricks.sliding_window_view
        hw, lw, cw, aw = sw(h, n_h), sw(l, n_h), sw(c, n_h), sw(a, n_h)
        hs = hstar[n_h - 1:, None]
        floor = hs - band_atr * aw
        if hold_on == "close":
            band = (cw <= hs) & (cw >= floor)
        else:
            band = (hw <= hs) & (lw >= floor)
        prev_max = np.maximum.accumulate(hw, axis=1)[:, :-1]          # max H over s..i-1, for i = s+1..t
        no_drop = (prev_max - lw[:, 1:]) < hold_decline_atr * aw[:, 1:]
        ok_rows = band.all(axis=1) & no_drop.all(axis=1) & np.isfinite(aw).all(axis=1) & np.isfinite(hs[:, 0])
    hold = np.zeros(n, dtype=bool)
    hold[n_h - 1:] = ok_rows
    # A run of consecutive hold bars emits at most once. A run continues even if the level pivot changes under it
    # (a minor pivot formed during the stay becomes 'the last swing high').
    # Review fix: the run emits at its FIRST bar whose history test also passes, not only at its first bar. Before,
    # a run whose first bar failed the history test was dropped for good, e.g. when the last rejection's decline
    # ends on the stay's first bar s (it counts only once s moves past it, one bar later).
    out: list[Event] = []
    last_rej = -1                                         # newest rejection already used by an earlier event
    run_start, run_done = -1, False
    for t in np.flatnonzero(hold).tolist():
        if t == 0 or not hold[t - 1]:
            run_start, run_done = t, False
        if run_done:
            continue
        lev = int(p[t])
        s = t - n_h + 1
        H = float(hstar[t])
        js = np.arange(max(0, t - window + 1), s)
        with np.errstate(invalid="ignore"):
            near = h[js] >= H - approach_atr * a[js]
            if approach_above_atr is not None:
                near &= h[js] <= H + approach_above_atr * a[js]
        de = dec_end[js]
        cand = js[near & (de >= 0) & (de <= s - 1)]
        rej, ends, nxt = [], [], -1
        for j in cand.tolist():
            if j > nxt:                                   # a new episode starts only after the last decline
                rej.append(j)
                ends.append(int(dec_end[j]))
                nxt = ends[-1]
        if len(rej) < min_rejections or rej[-1] <= last_rej:
            continue
        last_rej = rej[-1]
        run_done = True
        sl = slice(s, t + 1)
        at = float(a[t])

        def mean(x, sl=sl):
            return float(np.mean(x[sl]))

        drop = float(np.max(np.maximum.accumulate(h[sl])[:-1] - l[s + 1:t + 1])) if n_h > 1 else 0.0
        out.append(Event("choch", t, t, +1, H, {
            "level": H, "level_idx": lev, "level_known_at": lev + lag, "level_mode": level, "hold_on": hold_on,
            "hold_start": s, "run_hold_start": run_start - n_h + 1, "n_rejections": len(rej),
            "rejections": tuple(rej), "rejection_ends": tuple(ends), "n_approach_bars": int(len(cand)),
            "hold_close_min_atr": (H - float(np.min(c[sl]))) / at,
            "hold_low_atr": (H - float(np.min(l[sl]))) / at,
            "hold_high_over_level_atr": (float(np.max(h[sl])) - H) / at,
            "hold_max_decline_atr": drop / at,
            "mean_rv": mean(k.rv), "mean_rv2": mean(k.rv2), "mean_ae": mean(k.ae), "mean_ae2": mean(k.ae2),
            "holiday": bool(k.holiday[sl].any()), "news": bool(k.news[sl].any()), "atrp": at}))
    return _sort(out)
