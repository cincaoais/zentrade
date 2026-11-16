"""Per-day adjustment-event cache for the moomoo daily sync.

``iter_daily`` derives event rows from the raw/QFQ pair it already downloaded
and parks them on disk for the day. The adjustment-factor sync (Task 6) can
then reuse them instead of spending paced ``get_rehab`` calls, and a rerun of
the daily sync merges into the same files (idempotent upsert).

Layout under ``settings.data_dir``::

    moomoo/adj_events_<YYYY-MM-DD>.parquet  # kline_sync._ADJ_EVENT_SCHEMA
    moomoo/adj_covered_<YYYY-MM-DD>.json    # symbols seen that day (bare tickers)
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import polars as pl

from app.services.kline_sync import _ADJ_EVENT_SCHEMA

SUBDIR = "moomoo"
_COLUMNS = list(_ADJ_EVENT_SCHEMA)


def _paths(data_dir: Path, day: date) -> tuple[Path, Path, Path]:
    base = Path(data_dir) / SUBDIR
    stamp = day.isoformat()
    return base, base / f"adj_events_{stamp}.parquet", base / f"adj_covered_{stamp}.json"


def _normalize(events: list[dict]) -> pl.DataFrame:
    """Canonical event frame; an empty list still carries the full schema."""
    if not events:
        return pl.DataFrame(schema=_ADJ_EVENT_SCHEMA)
    return (
        pl.DataFrame(events)
        .select(_COLUMNS)
        .cast(_ADJ_EVENT_SCHEMA, strict=False)
    )


def _atomic_parquet(frame: pl.DataFrame, path: Path) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    frame.write_parquet(tmp)
    os.replace(tmp, path)


def write(data_dir: Path, day: date, events: list[dict], covered: set[str]) -> None:
    """Merge today's events and covered symbols into the on-disk day files."""
    base, events_path, covered_path = _paths(data_dir, day)
    base.mkdir(parents=True, exist_ok=True)

    fresh = _normalize(events)
    if events_path.exists():
        existing = pl.read_parquet(events_path).select(_COLUMNS).cast(
            _ADJ_EVENT_SCHEMA, strict=False
        )
        merged = pl.concat([existing, fresh], how="vertical")
    else:
        merged = fresh
    merged = (
        merged.unique(subset=["symbol", "trade_date"], keep="last")
        .sort(["symbol", "trade_date"])
    )
    _atomic_parquet(merged, events_path)

    previous: set[str] = set()
    if covered_path.exists():
        try:
            previous = {str(item) for item in json.loads(covered_path.read_text(encoding="utf-8"))}
        except (OSError, ValueError, TypeError):
            previous = set()
    all_covered = sorted(previous | {str(symbol) for symbol in covered})
    tmp = covered_path.with_name(f"{covered_path.name}.tmp")
    tmp.write_text(json.dumps(all_covered, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, covered_path)


def read(data_dir: Path, day: date) -> tuple[pl.DataFrame, set[str]] | None:
    """Cached events + covered symbols for ``day``; ``None`` when nothing was written."""
    _base, events_path, covered_path = _paths(data_dir, day)
    if not events_path.exists():
        return None
    frame = pl.read_parquet(events_path).select(_COLUMNS).cast(_ADJ_EVENT_SCHEMA, strict=False)

    covered: set[str] = set()
    if covered_path.exists():
        try:
            covered = {str(item) for item in json.loads(covered_path.read_text(encoding="utf-8"))}
        except (OSError, ValueError, TypeError):
            covered = set()
    return frame, covered