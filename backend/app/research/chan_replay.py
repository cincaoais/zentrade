"""Point-in-time Chan point replay.

The engine is injected as ``snapshot_fn`` so this module is pure: it never
imports czsc and never reads the store. ``replay_symbol`` walks the bars one
day at a time and records only what was knowable on that day.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl

logger = logging.getLogger(__name__)

# The engine only ever sees these columns; everything else is store metadata.
ENGINE_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")

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
        # Sorted: set order of string tuples varies with PYTHONHASHSEED, which
        # made stored gone parts differ byte-for-byte between runs.
        vanished = sorted(previous_ids - current_ids, key=lambda key: (str(key[0]), str(key[1])))
        for identity in vanished:
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


# ---------------------------------------------------------------------------
# Runner: parts, resume, worker pool
# ---------------------------------------------------------------------------


def chan_dir(data_dir: Path) -> Path:
    """``<data_dir>/research/chan/<chan_engine_version>``."""
    from app.research.versions import chan_engine_version

    return Path(data_dir) / "research" / "chan" / chan_engine_version()


def _real_snapshot(frame: pd.DataFrame) -> dict:
    """The real daily engine, as a module-level (picklable) callable."""
    from app.research.vendor.dsa_chan.engine import build_level_snapshot

    return build_level_snapshot(frame, "1d")


def _write_pair(
    points: pl.DataFrame,
    points_path: Path,
    gone: pl.DataFrame,
    gone_path: Path,
) -> None:
    """Write both parts through ``.tmp`` files; the points part lands last.

    The points file is the resume marker, so it must never exist while its
    sibling is missing. Fixed parquet settings keep the bytes reproducible.
    """
    points_path.parent.mkdir(parents=True, exist_ok=True)
    gone_path.parent.mkdir(parents=True, exist_ok=True)
    points_tmp = points_path.with_name(points_path.name + ".tmp")
    gone_tmp = gone_path.with_name(gone_path.name + ".tmp")
    try:
        gone.write_parquet(gone_tmp, compression="zstd", statistics=False)
        points.write_parquet(points_tmp, compression="zstd", statistics=False)
        os.replace(gone_tmp, gone_path)
        os.replace(points_tmp, points_path)
    finally:
        for tmp in (points_tmp, gone_tmp):
            if tmp.exists():
                tmp.unlink()


def _replay_one(task: tuple[str, str, Callable[[pd.DataFrame], dict] | None, str]) -> dict:
    """Replay one symbol in a child process; never raises."""
    data_dir_s, symbol, snapshot_fn, engine_version = task
    try:
        from app.research.bars import load_bars

        bars = load_bars(Path(data_dir_s), symbols=[symbol])
        frame = bars.to_pandas()
        missing = [column for column in ENGINE_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"bars frame is missing columns: {', '.join(missing)}")
        frame = frame.loc[:, list(ENGINE_COLUMNS)]

        points, gone, stats = replay_symbol(
            symbol,
            frame,
            snapshot_fn if snapshot_fn is not None else _real_snapshot,
            engine_version,
        )
        base = Path(data_dir_s) / "research" / "chan" / engine_version
        _write_pair(
            points,
            base / "points" / f"{symbol}.parquet",
            gone,
            base / "gone" / f"{symbol}.parquet",
        )
    except Exception as exc:
        logger.exception("chan replay failed for %s", symbol)
        return {
            "symbol": symbol,
            "ok": False,
            "error_days": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"symbol": symbol, "ok": True, "error_days": stats["error_days"], "error": None}


def run_chan_replay(
    data_dir: Path,
    symbols: list[str],
    workers: int,
    snapshot_fn: Callable[[pd.DataFrame], dict] | None = None,
) -> dict:
    """Replay ``symbols`` into parquet parts, resuming where parts exist.

    ``snapshot_fn=None`` uses the real engine at timeframe ``"1d"``. Each part
    is written atomically, one process per symbol; a symbol that raises is
    logged and reported in ``failed`` without leaving a part behind.
    """
    from app.research.versions import chan_engine_version

    data_dir = Path(data_dir)
    engine_version = chan_engine_version()
    base = data_dir / "research" / "chan" / engine_version
    points_dir = base / "points"
    points_dir.mkdir(parents=True, exist_ok=True)
    (base / "gone").mkdir(parents=True, exist_ok=True)

    todo: list[str] = []
    skipped = 0
    for symbol in symbols:
        if (points_dir / f"{symbol}.parquet").exists():
            skipped += 1
        else:
            todo.append(symbol)

    result: dict = {
        "done": 0,
        "skipped_existing": skipped,
        "failed": [],
        "error_days": 0,
    }
    if not todo:
        return result

    tasks = [(str(data_dir), symbol, snapshot_fn, engine_version) for symbol in todo]
    total = len(tasks)
    workers = max(1, int(workers))
    started = time.monotonic()
    completed = 0

    def merge(outcome: dict) -> None:
        nonlocal completed
        completed += 1
        if outcome["ok"]:
            result["done"] += 1
            result["error_days"] += outcome["error_days"]
        else:
            result["failed"].append(outcome["symbol"])
        if completed % 50 == 0 and completed < total:
            elapsed = time.monotonic() - started
            eta = elapsed / completed * (total - completed)
            logger.info(
                "chan replay %d/%d symbols, %.0fs elapsed, ETA %.0fs",
                completed,
                total,
                elapsed,
                eta,
            )

    if workers == 1:
        for outcome in map(_replay_one, tasks):
            merge(outcome)
    else:
        context = multiprocessing.get_context("spawn")
        with context.Pool(processes=workers) as pool:
            for outcome in pool.imap_unordered(_replay_one, tasks, chunksize=1):
                merge(outcome)

    result["failed"].sort()
    logger.info(
        "chan replay finished: %d done, %d skipped, %d failed",
        result["done"],
        result["skipped_existing"],
        len(result["failed"]),
    )
    return result


def load_chan_points(data_dir: Path) -> pl.DataFrame:
    """Concatenate every ``points/<SYMBOL>.parquet`` part."""
    base = chan_dir(data_dir) / "points"
    files = sorted(base.glob("*.parquet")) if base.is_dir() else []
    if not files:
        return pl.DataFrame(schema={column: _POINT_SCHEMA[column] for column in POINT_COLUMNS})
    return pl.concat([pl.read_parquet(path) for path in files], how="vertical")


def load_chan_gone(data_dir: Path) -> pl.DataFrame:
    """Concatenate every ``gone/<SYMBOL>.parquet`` part."""
    base = chan_dir(data_dir) / "gone"
    files = sorted(base.glob("*.parquet")) if base.is_dir() else []
    if not files:
        return pl.DataFrame(schema={column: _GONE_SCHEMA[column] for column in GONE_COLUMNS})
    return pl.concat([pl.read_parquet(path) for path in files], how="vertical")