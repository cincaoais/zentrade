"""Pure payload builder for the strategy replay page.

The exporter reads a finished backtest result, a composed signal table and the
stored Chan points and turns them into the JSON-shaped contract loaded by
``docs/research/viz/data/current.js``.  Nothing here touches the data store,
the engine or the network: the CLI in ``scripts/research/export_viz.py`` does
that and passes plain frames in.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.research.metrics import _as_date, _as_iso, _field

_LEG_COLUMNS = ("leg_prev_start", "leg_prev_end", "leg_last_start", "leg_last_end")
_LEG_WINDOW_DAYS = 180


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def _round(value: Any, digits: int) -> float | None:
    """Round a finite number, mapping null/NaN/inf to ``None``."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _price(value: Any) -> float | None:
    return _round(value, 4)


def _ratio(value: Any) -> float | None:
    return _round(value, 6)


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _config_value(config: Any, name: str) -> Any:
    if isinstance(config, dict):
        return config.get(name)
    return getattr(config, name, None)


def _require_date(value: Any, label: str) -> date:
    parsed = _as_date(value)
    if parsed is None:
        raise ValueError(f"missing or invalid {label}: {value!r}")
    return parsed


def _trade_date(trade: Any, name: str) -> date | None:
    value = _field(trade, name)
    if value is None:
        return None
    parsed = _as_date(value)
    if parsed is None:
        raise ValueError(f"invalid {name} on trade {_field(trade, 'symbol')!r}: {value!r}")
    return parsed


# ---------------------------------------------------------------------------
# Slot lanes
# ---------------------------------------------------------------------------


def assign_slots(trades: list[dict], slots: int) -> list[int]:
    """Assign each trade the lowest free lane over ``[entry_date, exit_date)``.

    The result is aligned with ``trades``; processing order is
    ``(entry_date, symbol)``.  A lane becomes free again on a trade's
    ``exit_date`` (half-open), while an open trade (``exit_date`` is null)
    keeps its lane forever.  Raises ``ValueError`` when more than ``slots``
    trades overlap.
    """
    if slots < 1:
        raise ValueError("slots must be at least 1")

    ordered = sorted(
        enumerate(trades),
        key=lambda item: (
            _require_date(_field(item[1], "entry_date"), "entry_date"),
            str(_field(item[1], "symbol") or ""),
            item[0],
        ),
    )

    occupied = [False] * slots
    lane_end: list[date | None] = [None] * slots
    assigned = [-1] * len(trades)

    for index, trade in ordered:
        entry = _require_date(_field(trade, "entry_date"), "entry_date")
        exit_date = _trade_date(trade, "exit_date")

        for lane in range(slots):
            if occupied[lane] and lane_end[lane] is not None and lane_end[lane] <= entry:
                occupied[lane] = False
                lane_end[lane] = None

        chosen = next((lane for lane in range(slots) if not occupied[lane]), None)
        if chosen is None:
            symbol = _field(trade, "symbol")
            raise ValueError(
                f"more than {slots} overlapping trades at {entry.isoformat()} "
                f"(trade {symbol!r} has no free slot)"
            )

        occupied[chosen] = True
        lane_end[chosen] = exit_date
        assigned[index] = chosen

    return assigned


# ---------------------------------------------------------------------------
# Legs
# ---------------------------------------------------------------------------


def _bars_lookup(bars: pl.DataFrame) -> dict[str, tuple[float, float]]:
    """``date iso -> (high, low)`` for one symbol's bars."""
    if bars is None or bars.is_empty() or "date" not in bars.columns:
        return {}
    lookup: dict[str, tuple[float, float]] = {}
    for row in bars.iter_rows(named=True):
        day = _as_iso(row.get("date"))
        high = row.get("high")
        low = row.get("low")
        if day is None or high is None or low is None:
            continue
        lookup[day] = (float(high), float(low))
    return lookup


def _leg_prices(point: Any, bars: pl.DataFrame) -> list[list] | None:
    """Three ``[iso, price]`` vertices for a Chan point, or ``None``."""
    lookup = _bars_lookup(bars)
    if not lookup:
        return None

    dates: list[date] = []
    for column in _LEG_COLUMNS:
        raw = _field(point, column)
        parsed = _as_date(raw)
        if parsed is None:
            return None
        dates.append(parsed)

    iso_dates = [day.isoformat() for day in dates]
    if any(day not in lookup for day in iso_dates):
        return None

    prev_start_high, prev_start_low = lookup[iso_dates[0]]
    prev_end_high, prev_end_low = lookup[iso_dates[1]]
    last_end_high, last_end_low = lookup[iso_dates[3]]
    last_start_high, _ = lookup[iso_dates[2]]

    prev_up = prev_end_high >= prev_start_high
    prev_start_price = prev_start_low if prev_up else prev_start_high
    prev_end_price = prev_end_high if prev_up else prev_end_low

    last_up = last_end_high >= last_start_high
    last_end_price = last_end_high if last_up else last_end_low

    return [
        [iso_dates[0], _price(prev_start_price)],
        [iso_dates[1], _price(prev_end_price)],
        [iso_dates[3], _price(last_end_price)],
    ]


def leg_vertices(point: dict, bars: pl.DataFrame) -> list[list] | None:
    """Vertices of the two Chan legs stored on ``point`` (one symbol's bars)."""
    return _leg_prices(point, bars)


def _point_candidates(
    points: pl.DataFrame | None,
    symbol: str,
    signal_date: str,
    retr_spec: tuple[tuple[str, ...], str],
) -> list[dict]:
    if points is None or points.is_empty():
        return []
    types, date_column = retr_spec
    if "symbol" not in points.columns or date_column not in points.columns:
        return []
    candidates: list[dict] = []
    for row in points.iter_rows(named=True):
        if str(row.get("symbol")) != str(symbol):
            continue
        if row.get("type") not in types:
            continue
        if _as_iso(row.get(date_column)) != signal_date:
            continue
        candidates.append(row)
    return candidates


def _stroke_range(
    lookup: dict[str, tuple[float, float]], start: date, end: date
) -> float | None:
    highs: list[float] = []
    lows: list[float] = []
    for day_iso, (high, low) in lookup.items():
        day = _as_date(day_iso)
        if day is None or day < start or day > end:
            continue
        highs.append(high)
        lows.append(low)
    if not highs:
        return None
    if not all(math.isfinite(value) for value in highs + lows):
        return None
    return max(highs) - min(lows)


def _retr_score(point: Any, lookup: dict[str, tuple[float, float]]) -> float | None:
    dates: list[date] = []
    for column in _LEG_COLUMNS:
        parsed = _as_date(_field(point, column))
        if parsed is None:
            return None
        dates.append(parsed)
    prev_range = _stroke_range(lookup, dates[0], dates[1])
    last_range = _stroke_range(lookup, dates[2], dates[3])
    if prev_range is None or last_range is None or prev_range == 0.0:
        return None
    return -(last_range / prev_range)


def _select_point(
    points: pl.DataFrame | None,
    symbol: str,
    signal_date: str,
    retr_spec: tuple[tuple[str, ...], str],
    bars: pl.DataFrame,
) -> dict | None:
    """The trade's Chan point: largest retracement wins on ties, else first."""
    candidates = _point_candidates(points, symbol, signal_date, retr_spec)
    if not candidates:
        return None
    lookup = _bars_lookup(bars)
    best = candidates[0]
    best_score: float | None = None
    for candidate in candidates:
        score = _retr_score(candidate, lookup)
        if score is not None and (best_score is None or score > best_score):
            best = candidate
            best_score = score
    return best


def _point_leg_dates(point: Any) -> list[date]:
    result: list[date] = []
    for column in _LEG_COLUMNS:
        parsed = _as_date(_field(point, column))
        if parsed is not None:
            result.append(parsed)
    return result


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _entry_rows(table: pl.DataFrame) -> dict[tuple[str, str], dict]:
    lookup: dict[tuple[str, str], dict] = {}
    if table is None or table.is_empty():
        return lookup
    has_entry = "entry" in table.columns
    for row in table.iter_rows(named=True):
        if has_entry and not row.get("entry"):
            continue
        symbol = row.get("symbol")
        signal_date = _as_iso(row.get("date"))
        if symbol is None or signal_date is None:
            continue
        lookup[(str(symbol), signal_date)] = row
    return lookup


def _series_values(bars: pl.DataFrame, symbol: str, column: str) -> dict[str, float]:
    if bars is None or bars.is_empty() or column not in bars.columns:
        return {}
    frame = bars.filter(pl.col("symbol") == symbol)
    result: dict[str, float] = {}
    for row in frame.iter_rows(named=True):
        day = _as_iso(row.get("date"))
        value = row.get(column)
        if day is None or value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result[day] = number
    return result


def _cagr(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    first, last = values[0], values[-1]
    if first <= 0.0 or last <= 0.0:
        return None
    return (last / first) ** (252.0 / (len(values) - 1)) - 1.0


def _bar_series(frame: pl.DataFrame, columns: tuple[str, ...]) -> dict[str, list]:
    series: dict[str, list] = {"d": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    if frame is None or frame.is_empty():
        return series
    ordered = frame.sort("date")
    for row in ordered.iter_rows(named=True):
        day = _as_iso(row.get("date"))
        if day is None:
            continue
        series["d"].append(day)
        series["o"].append(_price(row.get("open")))
        series["h"].append(_price(row.get("high")))
        series["l"].append(_price(row.get("low")))
        series["c"].append(_price(row.get("close")))
        volume = row.get("volume")
        series["v"].append(None if volume is None or not math.isfinite(float(volume)) else volume)
    return series


def build_payload(
    *,
    variant_id: str,
    config: dict,
    period,
    trades: list,
    equity: pl.DataFrame,
    table: pl.DataFrame,
    bars: pl.DataFrame,
    points: pl.DataFrame | None,
    retr_spec: tuple[tuple[str, ...], str] | None,
    stats: dict,
    holdout: bool,
    generated_at: str,
) -> dict:
    """Build the complete ``window.ZT_VIZ`` payload."""
    start: date = _require_date(getattr(period, "start", None), "period.start")
    end: date = _require_date(getattr(period, "end", None), "period.end")
    period_name = getattr(period, "name", None)
    slots = _integer(_config_value(config, "slots"))
    if slots is None or slots < 1:
        slots = 1

    # --- days / equity -----------------------------------------------------
    equity_by_day: dict[str, float] = {}
    if equity is not None and not equity.is_empty() and "date" in equity.columns:
        for row in equity.iter_rows(named=True):
            day = _as_iso(row.get("date"))
            value = row.get("equity", row.get("value"))
            if day is None or value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                equity_by_day[day] = number

    days = sorted(
        day
        for day in equity_by_day
        if start.isoformat() <= day <= end.isoformat()
    )
    equity_values = [equity_by_day[day] for day in days]
    base_equity = equity_values[0] if equity_values else None
    equity_series = [
        _ratio(value / base_equity) if base_equity else None for value in equity_values
    ]

    # --- SPY ---------------------------------------------------------------
    spy_by_day = _series_values(bars, "SPY", "close")
    base_spy = spy_by_day.get(days[0]) if days else None
    spy_series = [
        _ratio(spy_by_day[day] / base_spy)
        if base_spy and day in spy_by_day
        else None
        for day in days
    ]
    spy_closes = [spy_by_day[day] for day in days if day in spy_by_day]
    spy_cagr = _ratio(_cagr(spy_closes))

    # --- entry rows / signals ---------------------------------------------
    entry_lookup = _entry_rows(table)
    traded_pairs = {
        (str(_field(trade, "symbol")), _as_iso(_field(trade, "entry_signal_date")))
        for trade in trades
    }

    rows_by_day: dict[str, list[dict]] = {}
    if table is not None and not table.is_empty() and "entry" in table.columns:
        for row in table.iter_rows(named=True):
            if not row.get("entry"):
                continue
            day = _as_iso(row.get("date"))
            if day is None or day < start.isoformat() or day > end.isoformat():
                continue
            rows_by_day.setdefault(day, []).append(row)

    signals: list[dict] = []
    for day in sorted(rows_by_day):
        items: list[dict] = []
        for row in rows_by_day[day]:
            symbol = row.get("symbol")
            if symbol is None:
                continue
            symbol = str(symbol)
            raw_score = row.get("raw_score")
            score_value = row.get("score")
            try:
                sort_score = float(score_value) if score_value is not None else None
            except (TypeError, ValueError):
                sort_score = None
            if sort_score is not None and not math.isfinite(sort_score):
                sort_score = None
            items.append(
                {
                    "symbol": symbol,
                    "score": _ratio(score_value),
                    "raw_score": _ratio(raw_score),
                    "taken": (symbol, day) in traded_pairs,
                    "_sort": sort_score,
                }
            )
        items.sort(
            key=lambda item: (
                item["_sort"] is None,
                -item["_sort"] if item["_sort"] is not None else 0.0,
                item["symbol"],
            )
        )
        for item in items:
            item.pop("_sort", None)
        signals.append({"date": day, "items": items})

    # --- trades ------------------------------------------------------------
    ordered = sorted(
        enumerate(trades),
        key=lambda item: (
            _require_date(_field(item[1], "entry_date"), "entry_date"),
            str(_field(item[1], "symbol") or ""),
            item[0],
        ),
    )
    ordered_trades = [trade for _index, trade in ordered]
    lane_by_index = assign_slots(ordered_trades, slots)

    symbol_bars: dict[str, pl.DataFrame] = {}
    earliest_leg: date | None = None
    output_trades: list[dict] = []
    traded_symbols: list[str] = []

    for position, trade in enumerate(ordered_trades):
        symbol_raw = _field(trade, "symbol")
        if symbol_raw is None:
            raise ValueError(f"trade #{position} has no symbol")
        symbol = str(symbol_raw)
        if symbol not in traded_symbols:
            traded_symbols.append(symbol)

        signal_date = _as_iso(_field(trade, "entry_signal_date"))
        if signal_date is None:
            raise ValueError(f"trade {symbol} has no entry_signal_date")
        row = entry_lookup.get((symbol, signal_date))
        if row is None:
            raise ValueError(
                f"trade {symbol} signal {signal_date} has no matching entry row"
            )

        entry_date = _trade_date(trade, "entry_date")
        exit_date = _trade_date(trade, "exit_date")
        entry_price_raw = _field(trade, "entry_price")
        exit_price_raw = _field(trade, "exit_price")
        entry_price = _price(entry_price_raw)
        exit_price = _price(exit_price_raw)

        entry_value = _round(entry_price_raw, 12)
        exit_value = _round(exit_price_raw, 12)

        ret = None
        if entry_value is not None and exit_value is not None and entry_value != 0.0:
            ret = _ratio(exit_value / entry_value - 1.0)

        r_value = None
        invalidation_raw = row.get("invalidation")
        atr_raw = row.get("atr14")
        if (
            entry_value is not None
            and exit_value is not None
            and invalidation_raw is not None
        ):
            try:
                invalidation = float(invalidation_raw)
                atr_floor = 0.5 * float(atr_raw) if atr_raw is not None else 0.0
                denominator = max(entry_value - invalidation, atr_floor)
                if math.isfinite(denominator) and denominator > 0.0:
                    r_value = _ratio((exit_value - entry_value) / denominator)
            except (TypeError, ValueError):
                r_value = None

        hold_days = _integer(_field(trade, "duration"))
        if hold_days is None and entry_date is not None and exit_date is not None:
            hold_days = (exit_date - entry_date).days

        legs = None
        if retr_spec is not None and points is not None:
            if symbol not in symbol_bars:
                symbol_bars[symbol] = (
                    bars.filter(pl.col("symbol") == symbol)
                    if bars is not None and not bars.is_empty()
                    else bars
                )
            point = _select_point(
                points, symbol, signal_date, retr_spec, symbol_bars[symbol]
            )
            if point is not None:
                legs = leg_vertices(point, symbol_bars[symbol])
                for leg_date in _point_leg_dates(point):
                    if earliest_leg is None or leg_date < earliest_leg:
                        earliest_leg = leg_date

        output_trades.append(
            {
                "id": position,
                "symbol": symbol,
                "slot": lane_by_index[position],
                "signal_date": signal_date,
                "entry_date": entry_date.isoformat() if entry_date else None,
                "exit_date": exit_date.isoformat() if exit_date else None,
                "entry_price": entry_price,
                "exit_price": exit_price if exit_price_raw is not None else None,
                "ret": ret,
                "r": r_value,
                "exit_reason": _field(trade, "exit_reason"),
                "hold_days": hold_days,
                "score": _ratio(row.get("score")),
                "raw_score": _ratio(row.get("raw_score")),
                "invalidation": _price(invalidation_raw),
                "legs": legs,
            }
        )

    # --- slots used --------------------------------------------------------
    slots_used: list[int] = []
    for day in days:
        count = 0
        for trade in ordered_trades:
            entry = _trade_date(trade, "entry_date")
            exit_date = _trade_date(trade, "exit_date")
            if entry is None or entry.isoformat() > day:
                continue
            if exit_date is None or exit_date.isoformat() > day:
                count += 1
        slots_used.append(count)

    # --- bars window -------------------------------------------------------
    window_start = start - timedelta(days=_LEG_WINDOW_DAYS)
    if earliest_leg is not None and earliest_leg < window_start:
        window_start = earliest_leg

    output_bars: dict[str, dict] = {}
    symbols_for_bars = sorted({*traded_symbols, "SPY"})
    for symbol in symbols_for_bars:
        frame = (
            bars.filter(
                (pl.col("symbol") == symbol)
                & (pl.col("date") >= pl.lit(window_start))
                & (pl.col("date") <= pl.lit(end))
            )
            if bars is not None and not bars.is_empty()
            else bars
        )
        output_bars[symbol] = _bar_series(frame, ("open", "high", "low", "close", "volume"))

    stats = stats or {}
    meta_stats = {
        "cagr": _ratio(stats.get("cagr")),
        "sharpe": _ratio(stats.get("sharpe_ann", stats.get("sharpe"))),
        "max_drawdown": _ratio(stats.get("max_drawdown")),
        "trades": _integer(stats.get("trades")),
        "win_rate": _ratio(stats.get("win_rate")),
        "avg_r": _ratio(stats.get("avg_r")),
    }

    return {
        "meta": {
            "variant": variant_id,
            "base": _config_value(config, "base"),
            "rank": _config_value(config, "rank"),
            "slots": slots,
            "period": period_name,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "generated_at": generated_at,
            "holdout": bool(holdout),
            "stats": meta_stats,
            "spy_cagr": spy_cagr,
        },
        "days": days,
        "equity": equity_series,
        "spy": spy_series,
        "slots_used": slots_used,
        "trades": output_trades,
        "signals": signals,
        "bars": output_bars,
    }


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_data_js(payload: dict, path: Path) -> None:
    """Write ``window.ZT_VIZ = <compact json>;`` as UTF-8 (NaN/inf -> null)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        _json_safe(payload),
        allow_nan=False,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    path.write_text(f"window.ZT_VIZ = {text};\n", encoding="utf-8")


__all__ = ["assign_slots", "build_payload", "leg_vertices", "write_data_js"]