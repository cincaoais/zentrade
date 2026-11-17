"""Point-in-time Chan point replay.

The engine is injected as ``snapshot_fn`` so this module is pure: it never
imports czsc and never reads the store. ``replay_symbol`` walks the bars one
day at a time and records only what was knowable on that day.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import pandas as pd
import polars as pl

POINT_COLUMNS = (
    "symbol",
    "known_date",
    "type",
    "side",
    "point_dt",
    "price",
    "invalidation_price",
    "basis",
    "state_at_known",
    "confirmed_date",
    "leg_prev_start",
    "leg_prev_end",
    "leg_last_start",
    "leg_last_end",
    "engine_version",
)

GONE_COLUMNS = (
    "symbol",
    "type",
    "point_dt",
    "first_known_date",
    "gone_date",
    "window_start",
)

_POINT_SCHEMA = {
    "symbol": pl.String,
    "known_date": pl.Date,
    "type": pl.String,
    "side": pl.String,
    "point_dt": pl.Date,
    "price": pl.Float64,
    "invalidation_price": pl.Float64,
    "basis": pl.String,
    "state_at_known": pl.String,
    "confirmed_date": pl.Date,
    "leg_prev_start": pl.Date,
    "leg_prev_end": pl.Date,
    "leg_last_start": pl.Date,
    "leg_last_end": pl.Date,
    "engine_version": pl.String,
}

_GONE_SCHEMA = {
    "symbol": pl.String,
    "type": pl.String,
    "point_dt": pl.Date,
    "first_known_date": pl.Date,
    "gone_date": pl.Date,
    "window_start": pl.Date,
}


def _as_date(value: Any) -> date | None:
    """Normalise a bar date or an engine ``*_dt`` string to a plain date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.date()


def _daily_level(snapshot: dict) -> dict:
    """Accept either a level snapshot or a multi-level snapshot."""
    levels = snapshot.get("levels")
    if isinstance(levels, dict) and isinstance(levels.get("D"), dict):
        return levels["D"]
    return snapshot


def _earliest_start(recent_bis: list[dict]) -> date | None:
    starts = [_as_date(bi.get("start_dt")) for bi in recent_bis]
    starts = [value for value in starts if value is not None]
    return min(starts) if starts else None


def _legs(point_dt: date | None, recent_bis: list[dict]) -> tuple:
    """Return ``(prev_start, prev_end, last_start, last_end)`` for a point."""
    prev_start = prev_end = last_start = last_end = None
    if point_dt is None:
        return prev_start, prev_end, last_start, last_end

    for index, stroke in enumerate(recent_bis):
        if _as_date(stroke.get("end_dt")) != point_dt:
            continue
        last_start = _as_date(stroke.get("start_dt"))
        last_end = _as_date(stroke.get("end_dt"))
        if index > 0:
            previous = recent_bis[index - 1]
            prev_start = _as_date(previous.get("start_dt"))
            prev_end = _as_date(previous.get("end_dt"))
        break

    return prev_start, prev_end, last_start, last_end


def _frame(rows: list[dict], columns: tuple[str, ...], schema: dict) -> pl.DataFrame:
    if rows:
        return pl.DataFrame(rows, schema=schema).select(list(columns))
    return pl.DataFrame(schema={column: schema[column] for column in columns})


def replay_symbol(
    symbol: str,
    bars: pd.DataFrame,
    snapshot_fn: Callable[[pd.DataFrame], dict],
    engine_version: str,
    start_index: int = 119,
) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """Replay one symbol causally and return ``(points, gone, stats)``.

    For each bar ``t`` from ``start_index`` onwards the engine sees only
    ``bars.iloc[: t + 1]``. An identity is ``(type, point_dt)``.
    """
    point_rows: list[dict] = []
    gone_rows: list[dict] = []
    seen: dict[tuple, dict] = {}
    previous_ids: set[tuple] = set()

    stats = {"days": 0, "available_days": 0, "error_days": 0}
    dates = bars["date"]

    for t in range(start_index, len(bars)):
        stats["days"] += 1
        snapshot = _daily_level(snapshot_fn(bars.iloc[: t + 1]))
        status = snapshot.get("status")
        if status == "error":
            stats["error_days"] += 1
        if status != "available":
            # A non-usable day produces no rows and does not become the
            # reference day for disappearance detection.
            continue
        stats["available_days"] += 1

        current_date = _as_date(dates.iloc[t])
        recent_bis = snapshot.get("recent_bis") or []
        current_points = snapshot.get("buy_sell_points") or []
        current_ids: set[tuple] = set()

        for point in current_points:
            point_dt = _as_date(point.get("dt"))
            identity = (point.get("type"), point_dt)
            current_ids.add(identity)

            row = seen.get(identity)
            if row is None:
                prev_start, prev_end, last_start, last_end = _legs(
                    point_dt, recent_bis
                )
                state = point.get("state")
                row = {
                    "symbol": symbol,
                    "known_date": current_date,
                    "type": point.get("type"),
                    "side": point.get("side"),
                    "point_dt": point_dt,
                    "price": point.get("price"),
                    "invalidation_price": point.get("invalidation_price"),
                    "basis": point.get("basis"),
                    "state_at_known": state,
                    "confirmed_date": (
                        current_date if state == "confirmed" else None
                    ),
                    "leg_prev_start": prev_start,
                    "leg_prev_end": prev_end,
                    "leg_last_start": last_start,
                    "leg_last_end": last_end,
                    "engine_version": engine_version,
                }
                seen[identity] = row
                point_rows.append(row)
            elif row["confirmed_date"] is None and point.get("state") == "confirmed":
                row["confirmed_date"] = current_date

        window_start = _earliest_start(recent_bis)
        for identity in previous_ids - current_ids:
            row = seen[identity]
            gone_rows.append(
                {
                    "symbol": symbol,
                    "type": identity[0],
                    "point_dt": row["point_dt"],
                    "first_known_date": row["known_date"],
                    "gone_date": current_date,
                    "window_start": window_start,
                }
            )

        previous_ids = current_ids

    return (
        _frame(point_rows, POINT_COLUMNS, _POINT_SCHEMA),
        _frame(gone_rows, GONE_COLUMNS, _GONE_SCHEMA),
        stats,
    )