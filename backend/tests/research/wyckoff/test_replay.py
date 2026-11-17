"""Wyckoff replay: causal event/range tables, part layout, resume and loaders."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import polars as pl

from app.research.wyckoff.replay import (
    EVENT_COLUMNS,
    EVENT_KINDS,
    RANGE_COLUMNS,
    load_wyckoff_events,
    load_wyckoff_ranges,
    run_wyckoff_replay,
    wyckoff_dir,
    wyckoff_symbol,
)

from .test_accumulation import _cycle

PORT_VERSION = "test-port"


def _dated_frame() -> tuple[pd.DataFrame, list[date]]:
    """Reuse the ported ``wyk`` accumulation-cycle generator as a dated US frame."""
    candles, _marks, _sc = _cycle()
    dates = [date(2024, 1, 2) + timedelta(days=i) for i in range(len(candles))]
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": candles.o,
            "high": candles.h,
            "low": candles.l,
            "close": candles.c,
            "volume": candles.v,
        }
    )
    return frame, dates


def _active_set(ranges: pl.DataFrame, day: date) -> set[tuple]:
    """Ranges active at ``day``: known on or before it and not yet ended (null = still open)."""
    return {
        (row["range_kind"], row["known_date"], row["lo"], row["hi"])
        for row in ranges.iter_rows(named=True)
        if row["known_date"] <= day and (row["end_date"] is None or day < row["end_date"])
    }


def test_truncation_events_equal() -> None:
    frame, dates = _dated_frame()
    events, _ = wyckoff_symbol("AAA", frame, PORT_VERSION)
    assert events.height > 0

    for fraction in (0.6, 0.75, 0.9):
        cut = int(len(frame) * fraction)
        truncated, _ = wyckoff_symbol("AAA", frame.iloc[:cut], PORT_VERSION)
        expected = events.filter(pl.col("known_date") <= dates[cut - 1])
        assert truncated.equals(expected), f"events differ at cut {cut}"


def test_truncation_ranges_active_equal() -> None:
    frame, dates = _dated_frame()
    _, ranges = wyckoff_symbol("AAA", frame, PORT_VERSION)

    for fraction in (0.6, 0.75, 0.9):
        cut = int(len(frame) * fraction)
        _, truncated = wyckoff_symbol("AAA", frame.iloc[:cut], PORT_VERSION)
        for day in dates[:cut]:
            assert _active_set(truncated, day) == _active_set(ranges, day), (
                f"active ranges differ at {day} (cut {cut})"
            )


def test_columns_and_kinds() -> None:
    frame, _ = _dated_frame()
    events, ranges = wyckoff_symbol("AAA", frame, PORT_VERSION)

    assert events.columns == list(EVENT_COLUMNS)
    assert set(events["kind"].to_list()) <= set(EVENT_KINDS)
    assert "end_idx" not in events.columns
    assert events.height > 0

    assert ranges.columns == list(RANGE_COLUMNS)
    assert set(ranges["range_kind"].to_list()) <= {"accumulation", "distribution", "generic"}
    assert ranges.height > 0


def _write_store(data_dir: Path, symbols: list[str]) -> None:
    """One enriched partition holding every row; ``load_bars`` filters on the date column."""
    frame, _ = _dated_frame()
    rows: list[dict] = []
    for symbol in symbols:
        for row in frame.itertuples(index=False):
            rows.append(
                {
                    "symbol": symbol,
                    "date": row.date,
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                    "volume": row.volume,
                    "amount": row.close * row.volume,
                    "raw_close": row.close,
                }
            )
    path = data_dir / "kline_daily_enriched" / "date=2024-01-02"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path / "part.parquet")


def test_runner_writes_parts_resumes_and_loads(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    _write_store(data_dir, ["AAA", "BBB"])

    first = run_wyckoff_replay(data_dir, ["AAA", "BBB"], workers=1)
    assert first["done"] == 2
    assert first["skipped_existing"] == 0
    assert first["failed"] == []

    base = wyckoff_dir(data_dir)
    for symbol in ("AAA", "BBB"):
        assert (base / "events" / f"{symbol}.parquet").exists()
        assert (base / "ranges" / f"{symbol}.parquet").exists()

    second = run_wyckoff_replay(data_dir, ["AAA", "BBB"], workers=1)
    assert second["done"] == 0
    assert second["skipped_existing"] == 2
    assert second["failed"] == []

    events = load_wyckoff_events(data_dir)
    assert set(events["symbol"].unique().to_list()) == {"AAA", "BBB"}
    assert events.columns == list(EVENT_COLUMNS)

    ranges = load_wyckoff_ranges(data_dir)
    assert set(ranges["symbol"].unique().to_list()) == {"AAA", "BBB"}
    assert ranges.columns == list(RANGE_COLUMNS)