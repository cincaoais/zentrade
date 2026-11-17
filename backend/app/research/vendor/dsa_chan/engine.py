# Vendored verbatim from daily_stock_analysis/src/chan/engine.py at commit e9b0d57127ed107df6730cc48ce2338e5ca1546e (MIT).
# Do not edit below this header; deviations go in app/research/DEVIATIONS.md.
# -*- coding: utf-8 -*-
"""Chan theory structure snapshot built on the ``czsc`` library.

``czsc`` supplies 包含处理 / 分型 / 笔 / 中枢. This module adds the parts czsc
does not expose as plain data: MACD-area 背驰, 一二三类买卖点 with invalidation
prices, trend classification, key levels and a multi-level summary.

Rules implemented here (all deterministic, no look-ahead beyond the frame):

* 背驰 (divergence): a stroke makes a new extreme versus the previous stroke of
  the same direction while its MACD histogram area is smaller.
* 一买/一卖 (1B/1S): a diverging stroke that ends outside the latest established
  中枢 (below ZD for a buy, above ZG for a sell). ``basis`` says whether the two
  latest 中枢 were trending (``trend_divergence``) or not (``range_divergence``).
* 二买/二卖 (2B/2S): the first pullback after a 1B/1S that does not break it.
* 三买/三卖 (3B/3S): a stroke leaves a 中枢 and the next pullback stays outside
  it (low above ZG for a buy, high below ZD for a sell).

线段 is not computed in this version and is reported as ``not_supported``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "chan_snapshot/1.1"  # 1.1: `state` replaces `confirmed`; adds as_of, level_roles
ENGINE_NAME = "czsc"

# Repo timeframe -> czsc frequency label / snapshot level key, coarse to fine.
_TIMEFRAME_FREQ = {"1w": "周线", "1d": "日线", "60m": "60分钟", "30m": "30分钟"}
LEVEL_KEYS = {"1w": "W", "1d": "D", "60m": "60M", "30m": "30M"}
SUPPORTED_TIMEFRAMES = tuple(_TIMEFRAME_FREQ)
_INTRADAY_TIMEFRAMES = frozenset({"60m", "30m"})

MIN_BARS_AVAILABLE = 120  # below this a level is reported as partial
MIN_BARS_REQUIRED = 30  # below this no structure is attempted
RECENT_BI_COUNT = 7
RECENT_ZHONGSHU_COUNT = 3
POINT_LOOKBACK_BIS = 9  # only report buy/sell points ending within these strokes
KEY_LEVEL_COUNT = 3
DIVERGENCE_RATIO_MAX = 1.0  # MACD area ratio below this counts as divergence
MIN_FORMING_BARS = 5  # a still-forming stroke shorter than this is not judged for divergence
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
_PRICE_DECIMALS = 4

_REQUIRED_COLUMNS = ("date", "open", "high", "low", "close")


@dataclass(frozen=True)
class Stroke:
    """One 笔. ``confirmed`` is False for the still-forming last stroke."""

    direction: str  # "up" | "down"
    start_dt: pd.Timestamp
    end_dt: pd.Timestamp
    high: float
    low: float
    macd_area: float
    bar_count: int
    confirmed: bool = True

    @property
    def start_price(self) -> float:
        return self.low if self.direction == "up" else self.high

    @property
    def end_price(self) -> float:
        return self.high if self.direction == "up" else self.low


@dataclass(frozen=True)
class Pivot:
    """One 中枢. ``last_stroke_index`` indexes the stroke list (None if trimmed)."""

    zg: float
    zd: float
    gg: float
    dd: float
    start_dt: pd.Timestamp
    end_dt: pd.Timestamp
    formed_dt: pd.Timestamp
    stroke_count: int
    last_stroke_index: Optional[int]


@dataclass(frozen=True)
class TradePoint:
    """One 买卖点 located at the end of ``stroke_index``."""

    kind: str  # 1B / 2B / 3B / 1S / 2S / 3S
    stroke_index: int
    dt: pd.Timestamp
    price: float
    invalidation_price: float
    confirmed: bool
    basis: str


# ---------------------------------------------------------------------------
# Pure rules (no czsc dependency; unit-tested directly)
# ---------------------------------------------------------------------------

def stroke_divergence(strokes: Sequence[Stroke], index: int) -> Optional[Dict[str, Any]]:
    """Return divergence facts for ``strokes[index]`` or None when not diverging."""
    if index < 2 or index >= len(strokes):
        return None
    cur, prev = strokes[index], strokes[index - 2]
    if cur.direction != prev.direction or prev.macd_area <= 0 or cur.macd_area <= 0:
        return None
    if not cur.confirmed and cur.bar_count < MIN_FORMING_BARS:
        return None
    if cur.direction == "down":
        new_extreme = cur.low < prev.low
    else:
        new_extreme = cur.high > prev.high
    if not new_extreme:
        return None
    ratio = cur.macd_area / prev.macd_area
    if ratio >= DIVERGENCE_RATIO_MAX:
        return None
    return {
        "type": "bottom" if cur.direction == "down" else "top",
        "strength_ratio": round(ratio, 3),
        "stroke_index": index,
        "compared_stroke_index": index - 2,
        "confirmed": cur.confirmed,
    }


def _context_pivot_index(pivots: Sequence[Pivot], stroke: Stroke) -> Optional[int]:
    """Latest pivot already established when ``stroke`` started."""
    for i in range(len(pivots) - 1, -1, -1):
        if pivots[i].formed_dt <= stroke.start_dt:
            return i
    return None


def detect_trade_points(strokes: Sequence[Stroke], pivots: Sequence[Pivot]) -> List[TradePoint]:
    """Detect 一二三类买卖点 over the whole stroke list, oldest first."""
    points: List[TradePoint] = []

    first_points: Dict[int, TradePoint] = {}
    for i, stroke in enumerate(strokes):
        if stroke_divergence(strokes, i) is None:
            continue
        ctx = _context_pivot_index(pivots, stroke)
        if ctx is None:
            continue
        pivot = pivots[ctx]
        prev = pivots[ctx - 1] if ctx > 0 else None
        if stroke.direction == "down" and stroke.low < pivot.zd:
            trending = prev is not None and pivot.zg < prev.zd
            kind = "1B"
        elif stroke.direction == "up" and stroke.high > pivot.zg:
            trending = prev is not None and pivot.zd > prev.zg
            kind = "1S"
        else:
            continue
        point = TradePoint(
            kind=kind,
            stroke_index=i,
            dt=stroke.end_dt,
            price=stroke.end_price,
            invalidation_price=stroke.end_price,
            confirmed=stroke.confirmed,
            basis="trend_divergence" if trending else "range_divergence",
        )
        first_points[i] = point
        points.append(point)

    for i, first in first_points.items():
        j = i + 2
        if j >= len(strokes):
            continue
        pullback = strokes[j]
        if first.kind == "1B" and pullback.direction == "down" and pullback.low > first.price:
            kind = "2B"
        elif first.kind == "1S" and pullback.direction == "up" and pullback.high < first.price:
            kind = "2S"
        else:
            continue
        points.append(TradePoint(
            kind=kind,
            stroke_index=j,
            dt=pullback.end_dt,
            price=pullback.end_price,
            invalidation_price=first.price,
            confirmed=pullback.confirmed,
            basis="after_%s" % first.kind,
        ))

    for pivot in pivots:
        leave_idx = pivot.last_stroke_index
        if leave_idx is None or leave_idx + 1 >= len(strokes):
            continue
        leave, pullback = strokes[leave_idx], strokes[leave_idx + 1]
        if leave.direction == "up" and leave.high > pivot.zg and pullback.low > pivot.zg:
            kind, invalidation = "3B", pivot.zg
        elif leave.direction == "down" and leave.low < pivot.zd and pullback.high < pivot.zd:
            kind, invalidation = "3S", pivot.zd
        else:
            continue
        points.append(TradePoint(
            kind=kind,
            stroke_index=leave_idx + 1,
            dt=pullback.end_dt,
            price=pullback.end_price,
            invalidation_price=invalidation,
            confirmed=pullback.confirmed,
            basis="pivot_exit",
        ))

    points.sort(key=lambda p: (p.stroke_index, p.kind))
    return points


def classify_trend(pivots: Sequence[Pivot]) -> str:
    """上涨/下跌 need two non-overlapping 中枢 in the same direction; else 盘整."""
    if not pivots:
        return "undetermined"
    if len(pivots) < 2:
        return "consolidation"
    last, prev = pivots[-1], pivots[-2]
    if last.zd > prev.zg:
        return "up"
    if last.zg < prev.zd:
        return "down"
    return "consolidation"


# ---------------------------------------------------------------------------
# Frame preparation
# ---------------------------------------------------------------------------

def _prepare_frame(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(_REQUIRED_COLUMNS) + ["volume", "amount"])
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError("kline frame is missing columns: %s" % ", ".join(missing))
    out = pd.DataFrame({"date": pd.to_datetime(df["date"], errors="coerce")})
    if getattr(out["date"].dt, "tz", None) is not None:
        out["date"] = out["date"].dt.tz_localize(None)
    for col in ("open", "high", "low", "close"):
        out[col] = pd.to_numeric(df[col].to_numpy(), errors="coerce")
    volume = df["volume"].to_numpy() if "volume" in df.columns else 0.0
    out["volume"] = pd.to_numeric(pd.Series(volume, index=out.index), errors="coerce").fillna(0.0)
    if "amount" in df.columns:
        amount = pd.to_numeric(pd.Series(df["amount"].to_numpy(), index=out.index), errors="coerce")
        out["amount"] = amount.fillna(out["volume"] * out["close"])
    else:
        out["amount"] = out["volume"] * out["close"]
    out = out.dropna(subset=list(_REQUIRED_COLUMNS))
    out = out.sort_values("date").drop_duplicates(subset="date", keep="last").reset_index(drop=True)
    return out


def _macd_histogram(close: pd.Series) -> pd.Series:
    dif = close.ewm(span=MACD_FAST, adjust=False).mean() - close.ewm(span=MACD_SLOW, adjust=False).mean()
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return (dif - dea) * 2.0


def _macd_area(frame: pd.DataFrame, hist: pd.Series, start: pd.Timestamp, end: pd.Timestamp, direction: str) -> float:
    """Sum of same-sign MACD histogram bars over a stroke (start bar excluded)."""
    mask = (frame["date"] > start) & (frame["date"] <= end)
    values = hist[mask]
    if direction == "up":
        return float(values[values > 0].sum())
    return float(-values[values < 0].sum())


# ---------------------------------------------------------------------------
# czsc adapter
# ---------------------------------------------------------------------------

def _load_czsc():
    import czsc  # heavy optional dependency: import lazily

    return czsc


def _direction_label(direction: Any) -> str:
    return "up" if str(direction) in ("向上", "Up", "Direction.Up") else "down"


def _run_czsc(frame: pd.DataFrame, timeframe: str, symbol: str):
    czsc = _load_czsc()
    bars_df = pd.DataFrame({
        "dt": frame["date"],
        "symbol": symbol,
        "open": frame["open"],
        "close": frame["close"],
        "high": frame["high"],
        "low": frame["low"],
        "vol": frame["volume"],
        "amount": frame["amount"],
    })
    bars = czsc.format_standard_kline(bars_df, freq=_TIMEFRAME_FREQ[timeframe])
    return czsc.CZSC(bars), getattr(czsc, "__version__", "unknown")


def _extract_strokes(analysis: Any, frame: pd.DataFrame, hist: pd.Series) -> List[Stroke]:
    strokes: List[Stroke] = []
    for bi in analysis.bi_list:
        direction = _direction_label(bi.direction)
        start, end = pd.Timestamp(bi.sdt), pd.Timestamp(bi.edt)
        strokes.append(Stroke(
            direction=direction,
            start_dt=start,
            end_dt=end,
            high=float(bi.high),
            low=float(bi.low),
            macd_area=_macd_area(frame, hist, start, end, direction),
            bar_count=int(getattr(bi, "length", 0) or 0),
            confirmed=True,
        ))

    forming = _forming_stroke(analysis, frame, hist, strokes[-1] if strokes else None)
    if forming is not None:
        strokes.append(forming)
    return strokes


def _forming_stroke(analysis: Any, frame: pd.DataFrame, hist: pd.Series, last: Optional[Stroke]) -> Optional[Stroke]:
    """The unfinished stroke after the last confirmed 分型, if it has moved at all."""
    if last is None:
        return None
    tail = frame[frame["date"] > last.end_dt]
    if tail.empty:
        return None
    direction = "down" if last.direction == "up" else "up"
    if direction == "up":
        pos = tail["high"].idxmax()
        high, low = float(tail["high"].loc[pos]), last.low
        if high <= low:
            return None
    else:
        pos = tail["low"].idxmin()
        high, low = last.high, float(tail["low"].loc[pos])
        if low >= high:
            return None
    end = pd.Timestamp(tail["date"].loc[pos])
    return Stroke(
        direction=direction,
        start_dt=last.end_dt,
        end_dt=end,
        high=high,
        low=low,
        macd_area=_macd_area(frame, hist, last.end_dt, end, direction),
        bar_count=int((tail["date"] <= end).sum()) + 1,
        confirmed=False,
    )


def _extract_pivots(analysis: Any, strokes: Sequence[Stroke]) -> List[Pivot]:
    index_by_start = {s.start_dt: i for i, s in enumerate(strokes) if s.confirmed}
    pivots: List[Pivot] = []
    for zs in analysis.zs_list:
        bis = list(zs.bis)
        zg, zd = float(zs.zg), float(zs.zd)
        if len(bis) < 3 or not zg > zd:
            continue
        pivots.append(Pivot(
            zg=zg,
            zd=zd,
            gg=float(zs.gg),
            dd=float(zs.dd),
            start_dt=pd.Timestamp(zs.sdt),
            end_dt=pd.Timestamp(zs.edt),
            formed_dt=pd.Timestamp(bis[2].edt),
            stroke_count=len(bis),
            last_stroke_index=index_by_start.get(pd.Timestamp(bis[-1].sdt)),
        ))
    return pivots


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------

def _price(value: float) -> float:
    return round(float(value), _PRICE_DECIMALS)


def _fmt_dt(value: pd.Timestamp, timeframe: str) -> str:
    fmt = "%Y-%m-%d %H:%M" if timeframe in _INTRADAY_TIMEFRAMES else "%Y-%m-%d"
    return pd.Timestamp(value).strftime(fmt)


def _last_finished_index(strokes: Sequence[Stroke]) -> Optional[int]:
    for i in range(len(strokes) - 1, -1, -1):
        if strokes[i].confirmed:
            return i
    return None


def stroke_state(strokes: Sequence[Stroke], index: int) -> str:
    """Stability of ``strokes[index]``: forming / candidate / confirmed.

    czsc may still redraw or remove the newest finished stroke; strokes that already
    have a newer finished stroke after them were never revised in the repaint audit,
    so only those are ``confirmed``.
    """
    if not strokes[index].confirmed:
        return "forming"
    if index == _last_finished_index(strokes):
        return "candidate"
    return "confirmed"


def _stroke_dict(stroke: Stroke, level: str, timeframe: str, state: str) -> Dict[str, Any]:
    start, end = stroke.start_price, stroke.end_price
    return {
        "id": "%s.bi.%s" % (level, _fmt_dt(stroke.end_dt, timeframe)),
        "direction": stroke.direction,
        "start_dt": _fmt_dt(stroke.start_dt, timeframe),
        "end_dt": _fmt_dt(stroke.end_dt, timeframe),
        "start_price": _price(start),
        "end_price": _price(end),
        "amplitude_pct": round((end - start) / start * 100.0, 2) if start else None,
        "macd_area": round(stroke.macd_area, 4),
        "state": state,
    }


def _pivot_dict(pivot: Pivot, timeframe: str) -> Dict[str, Any]:
    return {
        "zg": _price(pivot.zg),
        "zd": _price(pivot.zd),
        "gg": _price(pivot.gg),
        "dd": _price(pivot.dd),
        "start_dt": _fmt_dt(pivot.start_dt, timeframe),
        "end_dt": _fmt_dt(pivot.end_dt, timeframe),
        "stroke_count": pivot.stroke_count,
    }


def _point_dict(
    point: TradePoint, frame: pd.DataFrame, level: str, timeframe: str, state: str
) -> Dict[str, Any]:
    after = frame[frame["date"] > point.dt]
    is_buy = point.kind.endswith("B")
    if after.empty:
        still_valid = True
    elif is_buy:
        still_valid = bool(after["low"].min() >= point.invalidation_price)
    else:
        still_valid = bool(after["high"].max() <= point.invalidation_price)
    return {
        "evidence_id": "%s.%s.%s" % (level, point.kind, _fmt_dt(point.dt, timeframe)),
        "type": point.kind,
        "side": "buy" if is_buy else "sell",
        "level": level,
        "dt": _fmt_dt(point.dt, timeframe),
        "price": _price(point.price),
        "invalidation_price": _price(point.invalidation_price),
        "basis": point.basis,
        "state": state if still_valid else "invalidated",
        "still_valid": still_valid,
        "bars_since": int(len(after)),
    }


def _key_levels(pivots: Sequence[Pivot], strokes: Sequence[Stroke], last_close: float) -> Dict[str, List[float]]:
    candidates = set()
    for pivot in pivots[-2:]:
        candidates.update((pivot.zg, pivot.zd, pivot.gg, pivot.dd))
    for stroke in strokes[-3:]:
        candidates.update((stroke.high, stroke.low))
    support = sorted((c for c in candidates if c < last_close), reverse=True)[:KEY_LEVEL_COUNT]
    resistance = sorted(c for c in candidates if c > last_close)[:KEY_LEVEL_COUNT]
    return {"support": [_price(v) for v in support], "resistance": [_price(v) for v in resistance]}


def _latest_divergence(strokes: Sequence[Stroke], level: str, timeframe: str) -> Dict[str, Any]:
    """Divergence on the forming stroke, else on the last confirmed stroke."""
    for index in range(len(strokes) - 1, max(len(strokes) - 3, -1), -1):
        found = stroke_divergence(strokes, index)
        if found is None:
            continue
        return {
            "present": True,
            "type": found["type"],
            "method": "macd_area",
            "strength_ratio": found["strength_ratio"],
            "stroke_id": "%s.bi.%s" % (level, _fmt_dt(strokes[index].end_dt, timeframe)),
            "compared_stroke_id": "%s.bi.%s" % (level, _fmt_dt(strokes[index - 2].end_dt, timeframe)),
            "state": stroke_state(strokes, index),
        }
    return {"present": False, "method": "macd_area"}


def _empty_level(timeframe: str, status: str, bar_count: int, warning: str) -> Dict[str, Any]:
    return {
        "status": status,
        "timeframe": timeframe,
        "bar_count": bar_count,
        "warnings": [warning],
    }


def build_level_snapshot(df: Optional[pd.DataFrame], timeframe: str, symbol: str = "") -> Dict[str, Any]:
    """Build the Chan structure facts of one level from a standard K-line frame.

    ``df`` uses the repo's standard columns (date/open/high/low/close/volume/amount),
    ascending or not. Never raises for bad data: problems are reported in ``status``
    (available / partial / missing / engine_unavailable / error) and ``warnings``.
    """
    if timeframe not in _TIMEFRAME_FREQ:
        raise ValueError("unsupported timeframe: %r" % (timeframe,))
    level = LEVEL_KEYS[timeframe]

    try:
        frame = _prepare_frame(df)
    except ValueError as exc:
        return _empty_level(timeframe, "error", 0, str(exc))
    bar_count = len(frame)
    if bar_count < MIN_BARS_REQUIRED:
        return _empty_level(
            timeframe, "missing", bar_count,
            "only %d bars; at least %d are needed for any structure" % (bar_count, MIN_BARS_REQUIRED),
        )

    try:
        analysis, engine_version = _run_czsc(frame, timeframe, symbol or "UNKNOWN")
    except ImportError as exc:
        return _empty_level(timeframe, "engine_unavailable", bar_count, "czsc is not installed: %s" % exc)
    except Exception as exc:  # noqa: BLE001 - engine failure must not break the analysis run
        logger.warning("[chan] czsc failed for %s %s: %s", symbol, timeframe, exc)
        return _empty_level(timeframe, "error", bar_count, "czsc failed: %s" % exc)

    hist = _macd_histogram(frame["close"])
    strokes = _extract_strokes(analysis, frame, hist)
    pivots = _extract_pivots(analysis, strokes)
    last_close = float(frame["close"].iloc[-1])

    warnings: List[str] = []
    status = "available"
    if bar_count < MIN_BARS_AVAILABLE:
        status = "partial"
        warnings.append("only %d bars; structure may be incomplete below %d" % (bar_count, MIN_BARS_AVAILABLE))
    confirmed = [s for s in strokes if s.confirmed]
    if not confirmed:
        status = "partial"
        warnings.append("no confirmed stroke (笔) yet")
    if not pivots:
        status = "partial"
        warnings.append("no valid pivot (中枢) yet")

    first_reported = max(len(strokes) - POINT_LOOKBACK_BIS, 0)
    points = [
        _point_dict(p, frame, level, timeframe, stroke_state(strokes, p.stroke_index))
        for p in detect_trade_points(strokes, pivots)
        if p.stroke_index >= first_reported
    ]
    last_finished = _last_finished_index(strokes)

    position = None
    if pivots:
        last_pivot = pivots[-1]
        if last_close > last_pivot.zg:
            position = "above"
        elif last_close < last_pivot.zd:
            position = "below"
        else:
            position = "inside"

    forming = strokes[-1] if strokes and not strokes[-1].confirmed else None
    return {
        "status": status,
        "timeframe": timeframe,
        "engine_version": engine_version,
        "bar_count": bar_count,
        "first_bar": _fmt_dt(frame["date"].iloc[0], timeframe),
        "last_bar": _fmt_dt(frame["date"].iloc[-1], timeframe),
        "last_close": _price(last_close),
        "trend_type": classify_trend(pivots),
        "position_vs_last_zhongshu": position,
        "current_bi": _stroke_dict(forming, level, timeframe, "forming") if forming else None,
        "last_confirmed_bi": (
            _stroke_dict(strokes[last_finished], level, timeframe, "candidate")
            if last_finished is not None else None
        ),
        "recent_bis": [
            _stroke_dict(s, level, timeframe, stroke_state(strokes, i))
            for i, s in enumerate(strokes)
            if i >= len(strokes) - RECENT_BI_COUNT
        ],
        "segment": {"status": "not_supported"},
        "zhongshu": [_pivot_dict(p, timeframe) for p in pivots[-RECENT_ZHONGSHU_COUNT:]],
        "divergence": _latest_divergence(strokes, level, timeframe),
        "buy_sell_points": points,
        "key_levels": _key_levels(pivots, strokes, last_close),
        "warnings": warnings,
    }


def _cross_level(levels: Mapping[str, Dict[str, Any]]) -> Dict[str, Any]:
    directions: Dict[str, str] = {}
    for key, level in levels.items():
        stroke = level.get("current_bi") or level.get("last_confirmed_bi")
        if stroke:
            directions[key] = stroke["direction"]
    if len(directions) < 2:
        return {"resonance": "unknown", "aligned_direction": None, "bi_directions": directions}
    ups = sum(1 for d in directions.values() if d == "up")
    downs = len(directions) - ups
    if ups == 0 or downs == 0:
        resonance = "full"
    else:
        resonance = "partial" if max(ups, downs) > min(ups, downs) else "none"
    aligned = None
    if ups != downs:
        aligned = "up" if ups > downs else "down"
    return {"resonance": resonance, "aligned_direction": aligned, "bi_directions": directions}


_LEVEL_ORDER = ("W", "D", "60M", "30M")


def _level_roles(levels: Mapping[str, Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Which level sets direction, which one carries the trade points, which one times entries.

    Daily is the operating level when usable; the next higher usable level gives direction
    and the next lower usable level confirms. Unusable (missing/error) levels are skipped.
    """
    usable = [k for k in _LEVEL_ORDER if levels.get(k, {}).get("status") in ("available", "partial")]
    if not usable:
        return {"direction": None, "operating": None, "confirmation": None}
    operating = "D" if "D" in usable else usable[0]
    pos = usable.index(operating)
    return {
        "direction": usable[pos - 1] if pos > 0 else None,
        "operating": operating,
        "confirmation": usable[pos + 1] if pos + 1 < len(usable) else None,
    }


def build_chan_snapshot(
    frames: Mapping[str, Optional[pd.DataFrame]],
    symbol: str,
    *,
    market: Optional[str] = None,
    name: Optional[str] = None,
    sources: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Build the multi-level ``chan_snapshot`` handed to the LLM.

    ``frames`` maps repo timeframes ("1w", "1d", "60m", "30m") to standard K-line
    frames; unknown keys are ignored and absent ones are simply not reported.
    ``sources`` optionally maps the same keys to the data source name.
    """
    levels: Dict[str, Dict[str, Any]] = {}
    for timeframe in SUPPORTED_TIMEFRAMES:
        if timeframe not in frames:
            continue
        level = build_level_snapshot(frames[timeframe], timeframe, symbol)
        if sources and sources.get(timeframe):
            level["source"] = sources[timeframe]
        levels[LEVEL_KEYS[timeframe]] = level

    statuses = [lv["status"] for lv in levels.values()]
    if statuses and all(s == "available" for s in statuses):
        overall = "available"
    elif any(s in ("available", "partial") for s in statuses):
        overall = "partial"
    else:
        overall = "missing"
    versions = sorted({lv["engine_version"] for lv in levels.values() if lv.get("engine_version")})
    last_bars = [lv["last_bar"] for lv in levels.values() if lv.get("last_bar")]
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "name": name,
        "market": market,
        "as_of": max(last_bars) if last_bars else None,
        "level_roles": _level_roles(levels),
        "data_quality": {
            "status": overall,
            "engine": ENGINE_NAME,
            "engine_version": versions[0] if versions else None,
            "unusable_levels": [k for k, lv in levels.items() if lv["status"] not in ("available", "partial")],
            "warnings": ["%s: %s" % (k, w) for k, lv in levels.items() for w in lv.get("warnings", [])],
        },
        "levels": levels,
        "cross_level": _cross_level(levels),
    }
