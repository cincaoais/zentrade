"""Wyckoff event and range replay.

The ported ``wyk`` detectors are causal by ``known_at``: running them once on
the full candle series yields, for every cut, exactly the rows a truncated run
would produce. This module turns those detectors into per-symbol parquet parts
(``events`` and ``ranges``) and a resumable worker-pool runner.
"""

from __future__ import annotations

import copy
import logging
import multiprocessing
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl

from app.research.wyckoff.accumulation import accumulation_events
from app.research.wyckoff.distribution import distribution_events
from app.research.wyckoff.ranges import event_ranges, generic_ranges
from app.research.wyckoff.us_bars import us_candles

logger = logging.getLogger(__name__)

EVENT_KINDS = ("entry_steady", "sos", "sos_failed", "lps", "sow", "utad_confirmed")

EVENT_COLUMNS = (
    "symbol",
    "known_date",
    "kind",
    "idx_date",
    "price",
    "stop_ref",
    "range_kind",
    "range_lo",
    "range_hi",
    "atr_known",
    "port_version",
)

RANGE_COLUMNS = ("symbol", "range_kind", "known_date", "end_date", "lo", "hi")

# Columns the detectors read; everything else is store metadata.
BAR_INPUT_COLUMNS = ("date", "open", "high", "low", "close", "volume")

_EVENT_KIND_SET = frozenset(EVENT_KINDS)

_EVENT_SCHEMA = {
    "symbol": pl.String,
    "known_date": pl.Date,
    "kind": pl.String,
    "idx_date": pl.Date,
    "price": pl.Float64,
    "stop_ref": pl.Float64,
    "range_kind": pl.String,
    "range_lo": pl.Float64,
    "range_hi": pl.Float64,
    "atr_known": pl.Float64,
    "port_version": pl.String,
}

_RANGE_SCHEMA = {
    "symbol": pl.String,
    "range_kind": pl.String,
    "known_date": pl.Date,
    "end_date": pl.Date,
    "lo": pl.Float64,
    "hi": pl.Float64,
}


def _frame(rows: list[dict], columns: tuple[str, ...], schema: dict) -> pl.DataFrame:
    if rows:
        return pl.DataFrame(rows, schema=schema).select(list(columns))
    return pl.DataFrame(schema={column: schema[column] for column in columns})


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def wyckoff_symbol(
    symbol: str, bars: pd.DataFrame, port_version: str
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Replay one symbol and return ``(events, ranges)``.

    ``bars`` carries ``date, open, high, low, close, volume``. The detectors run
    once on the full series; ``known_date`` / ``idx_date`` map their candle
    indices back to the dates ``us_candles`` kept.
    """
    candles, kept_dates = us_candles(bars)
    if len(candles) == 0:
        return (
            _frame([], EVENT_COLUMNS, _EVENT_SCHEMA),
            _frame([], RANGE_COLUMNS, _RANGE_SCHEMA),
        )

    _, acc_ranges = event_ranges(candles, "accumulation")
    _, dist_ranges = event_ranges(candles, "distribution")
    generic = generic_ranges(candles)
    all_ranges = acc_ranges + dist_ranges + generic

    # As wyk.stage0.runner._ranges_for: each side gets its own event ranges plus the
    # generic ranges, sorted by (known_at, start_idx, kind) and deep-copied per call.
    def side_ranges(event_side: list) -> list:
        merged = list(event_side) + list(generic)
        return copy.deepcopy(sorted(merged, key=lambda r: (r.known_at, r.start_idx, r.kind)))

    # accumulation_events runs springs -> spring_tests (entry_steady) -> sos -> lps.
    detected = accumulation_events(candles, side_ranges(acc_ranges)) + distribution_events(
        candles, side_ranges(dist_ranges)
    )

    event_rows: list[dict] = []
    for event in detected:
        if event.kind not in _EVENT_KIND_SET:
            continue
        data = event.data
        event_rows.append(
            {
                "symbol": symbol,
                "known_date": kept_dates[event.known_at],
                "kind": event.kind,
                "idx_date": kept_dates[event.idx],
                "price": _opt_float(event.price),
                "stop_ref": _opt_float(data.get("stop_ref")),
                "range_kind": data.get("range_kind"),
                "range_lo": _opt_float(data.get("range_lo")),
                "range_hi": _opt_float(data.get("range_hi")),
                "atr_known": float(candles.atrp[event.known_at]),
                "port_version": port_version,
            }
        )

    range_rows: list[dict] = []
    for trading_range in all_ranges:
        range_rows.append(
            {
                "symbol": symbol,
                "range_kind": trading_range.kind,
                "known_date": kept_dates[trading_range.known_at],
                "end_date": (
                    kept_dates[trading_range.end_idx]
                    if trading_range.end_idx is not None
                    else None
                ),
                "lo": float(trading_range.lo),
                "hi": float(trading_range.hi),
            }
        )

    events = _frame(event_rows, EVENT_COLUMNS, _EVENT_SCHEMA).sort(
        ["known_date", "idx_date", "kind", "range_kind", "price", "range_lo", "range_hi"],
        nulls_last=True,
    )
    ranges = _frame(range_rows, RANGE_COLUMNS, _RANGE_SCHEMA).sort(
        ["range_kind", "known_date", "end_date", "lo", "hi"], nulls_last=True
    )
    return events, ranges


# ---------------------------------------------------------------------------
# Runner: parts, resume, worker pool
# ---------------------------------------------------------------------------


def wyckoff_dir(data_dir: Path) -> Path:
    """``<data_dir>/research/wyckoff/<wyckoff_port_version>``."""
    from app.research.versions import wyckoff_port_version

    return Path(data_dir) / "research" / "wyckoff" / wyckoff_port_version()


def _write_pair(
    events: pl.DataFrame,
    events_path: Path,
    ranges: pl.DataFrame,
    ranges_path: Path,
) -> None:
    """Write both parts through ``.tmp`` files; the events part lands last.

    The events file is the resume marker, so it must never exist while its
    sibling is missing. Fixed parquet settings keep the bytes reproducible.
    """
    events_path.parent.mkdir(parents=True, exist_ok=True)
    ranges_path.parent.mkdir(parents=True, exist_ok=True)
    events_tmp = events_path.with_name(events_path.name + ".tmp")
    ranges_tmp = ranges_path.with_name(ranges_path.name + ".tmp")
    try:
        ranges.write_parquet(ranges_tmp, compression="zstd", statistics=False)
        events.write_parquet(events_tmp, compression="zstd", statistics=False)
        os.replace(ranges_tmp, ranges_path)
        os.replace(events_tmp, events_path)
    finally:
        for tmp in (events_tmp, ranges_tmp):
            if tmp.exists():
                tmp.unlink()


def _replay_one(task: tuple[str, str, str]) -> dict:
    """Replay one symbol in a child process; never raises."""
    data_dir_s, symbol, port_version = task
    try:
        from app.research.bars import load_bars

        bars = load_bars(Path(data_dir_s), symbols=[symbol])
        frame = bars.to_pandas()
        missing = [column for column in BAR_INPUT_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"bars frame is missing columns: {', '.join(missing)}")
        frame = frame.loc[:, list(BAR_INPUT_COLUMNS)]

        events, ranges = wyckoff_symbol(symbol, frame, port_version)
        base = Path(data_dir_s) / "research" / "wyckoff" / port_version
        _write_pair(
            events,
            base / "events" / f"{symbol}.parquet",
            ranges,
            base / "ranges" / f"{symbol}.parquet",
        )
    except Exception as exc:
        logger.exception("wyckoff replay failed for %s", symbol)
        return {
            "symbol": symbol,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"symbol": symbol, "ok": True, "error": None}


def run_wyckoff_replay(data_dir: Path, symbols: list[str], workers: int) -> dict:
    """Replay ``symbols`` into parquet parts, resuming where parts exist.

    Each part is written atomically, one process per symbol; a symbol that
    raises is logged and reported in ``failed`` without leaving a part behind.
    """
    from app.research.versions import wyckoff_port_version

    data_dir = Path(data_dir)
    port_version = wyckoff_port_version()
    base = data_dir / "research" / "wyckoff" / port_version
    events_dir = base / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (base / "ranges").mkdir(parents=True, exist_ok=True)

    todo: list[str] = []
    skipped = 0
    for symbol in symbols:
        if (events_dir / f"{symbol}.parquet").exists():
            skipped += 1
        else:
            todo.append(symbol)

    result: dict = {"done": 0, "skipped_existing": skipped, "failed": []}
    if not todo:
        return result

    tasks = [(str(data_dir), symbol, port_version) for symbol in todo]
    total = len(tasks)
    workers = max(1, int(workers))
    started = time.monotonic()
    completed = 0

    def merge(outcome: dict) -> None:
        nonlocal completed
        completed += 1
        if outcome["ok"]:
            result["done"] += 1
        else:
            result["failed"].append(outcome["symbol"])
        if completed % 50 == 0 and completed < total:
            elapsed = time.monotonic() - started
            eta = elapsed / completed * (total - completed)
            logger.info(
                "wyckoff replay %d/%d symbols, %.0fs elapsed, ETA %.0fs",
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
        "wyckoff replay finished: %d done, %d skipped, %d failed",
        result["done"],
        result["skipped_existing"],
        len(result["failed"]),
    )
    return result


def load_wyckoff_events(data_dir: Path) -> pl.DataFrame:
    """Concatenate every ``events/<SYMBOL>.parquet`` part."""
    base = wyckoff_dir(data_dir) / "events"
    files = sorted(base.glob("*.parquet")) if base.is_dir() else []
    if not files:
        return _frame([], EVENT_COLUMNS, _EVENT_SCHEMA)
    return pl.concat([pl.read_parquet(path) for path in files], how="vertical")


def load_wyckoff_ranges(data_dir: Path) -> pl.DataFrame:
    """Concatenate every ``ranges/<SYMBOL>.parquet`` part."""
    base = wyckoff_dir(data_dir) / "ranges"
    files = sorted(base.glob("*.parquet")) if base.is_dir() else []
    if not files:
        return _frame([], RANGE_COLUMNS, _RANGE_SCHEMA)
    return pl.concat([pl.read_parquet(path) for path in files], how="vertical")