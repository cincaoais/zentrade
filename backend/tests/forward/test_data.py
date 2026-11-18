"""Forward bars loader, universe snapshots and readiness checks."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.forward.data import (
    changed_symbols,
    check_readiness,
    forward_tradable,
    history_hashes,
    load_forward_bars,
    preserve_bars,
    snapshot_universe,
)
from app.research.bars import load_bars


def _bar_row(symbol: str, day: date, close: float = 10.5) -> dict:
    return {
        "symbol": symbol,
        "date": day,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": close,
        "volume": 1_000_000.0,
        "amount": 10_500_000.0,
        "raw_close": close,
        "raw_high": 11.0,
        "raw_low": 9.0,
        "turnover_rate": 1.0,
        "quote_ts": 0,
    }


def _write_partition(data_dir: Path, day: date, rows: list[dict]) -> None:
    path = Path(data_dir) / "kline_daily_enriched" / f"date={day.isoformat()}"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path / "part.parquet")


def _write_instruments(data_dir: Path, symbols: list[str]) -> None:
    path = Path(data_dir) / "instruments"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        [
            {
                "symbol": symbol,
                "asset_type": "stock",
                "instrument_type": "common_stock",
                "is_etf": False,
                "is_preferred": False,
                "is_warrant": False,
                "is_unit": False,
            }
            for symbol in symbols
        ],
        schema_overrides={
            "symbol": pl.String,
            "asset_type": pl.String,
            "instrument_type": pl.String,
            "is_etf": pl.Boolean,
            "is_preferred": pl.Boolean,
            "is_warrant": pl.Boolean,
            "is_unit": pl.Boolean,
        },
    ).write_parquet(path / "instruments.parquet")


def _days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(count)]


def test_preserved_bars_survive_store_deletion(tmp_path) -> None:
    days = _days(date(2026, 11, 12), 2)
    for day in days:
        _write_partition(tmp_path, day, [_bar_row("AAA", day), _bar_row("BBB", day)])

    written = preserve_bars(tmp_path, ["AAA"], through=days[-1])
    assert written == 2

    # The daily pipeline may prune or rewrite the enriched partitions; the
    # forward copy must keep the held symbol's history.
    for day in days:
        _write_partition(tmp_path, day, [_bar_row("BBB", day)])

    bars = load_forward_bars(tmp_path, symbols=["AAA"])
    assert bars["symbol"].to_list() == ["AAA", "AAA"]
    assert bars["date"].to_list() == days


def test_loader_matches_research_before_cutoff(tmp_path) -> None:
    days = _days(date(2026, 11, 12), 6)
    for day in days:
        _write_partition(
            tmp_path, day, [_bar_row("AAA", day), _bar_row("BBB", day, close=20.0)]
        )

    cutoff = date(2026, 11, 13)
    forward = load_forward_bars(tmp_path, end=cutoff)
    research = load_bars(tmp_path, end=cutoff)

    assert forward.equals(research)


def test_loader_reads_past_cutoff(tmp_path) -> None:
    days = _days(date(2026, 11, 12), 6)
    for day in days:
        _write_partition(tmp_path, day, [_bar_row("AAA", day)])

    bars = load_forward_bars(tmp_path)

    assert bars["date"].max() == date(2026, 11, 17)
    assert bars.filter(pl.col("date") == date(2026, 11, 17)).height == 1


def test_forward_tradable_keeps_delisted(tmp_path) -> None:
    day1 = date(2026, 11, 13)
    day2 = date(2026, 11, 16)

    _write_instruments(tmp_path, ["AAA", "BBB"])
    snapshot_universe(tmp_path, day1)
    _write_instruments(tmp_path, ["AAA"])
    snapshot_universe(tmp_path, day2)

    assert forward_tradable(tmp_path, day2) == {"AAA", "BBB"}


def test_snapshot_refuses_different_overwrite(tmp_path) -> None:
    day = date(2026, 11, 13)
    _write_instruments(tmp_path, ["AAA"])
    path = snapshot_universe(tmp_path, day)
    assert snapshot_universe(tmp_path, day) == path

    _write_instruments(tmp_path, ["AAA", "BBB"])
    try:
        snapshot_universe(tmp_path, day)
    except FileExistsError:
        pass
    else:
        raise AssertionError("snapshot_universe must not overwrite a different snapshot")


def test_readiness_missing_required(tmp_path) -> None:
    day = date(2026, 11, 13)
    _write_partition(tmp_path, day, [_bar_row("AAA", day)])

    readiness = check_readiness(
        tmp_path, day, required=["HELD"], universe=["AAA", "HELD"]
    )

    assert readiness.ready is False
    assert readiness.missing == ["HELD"]
    assert isinstance(readiness.checked_at, str)


def test_readiness_coverage(tmp_path) -> None:
    day = date(2026, 11, 13)
    universe = [f"S{i:03d}" for i in range(100)]
    _write_partition(tmp_path, day, [_bar_row(symbol, day) for symbol in universe[:97]])

    assert check_readiness(tmp_path, day, required=[], universe=universe).ready is False

    _write_partition(
        tmp_path,
        day,
        [_bar_row(symbol, day) for symbol in universe[:98]],
    )
    readiness = check_readiness(tmp_path, day, required=[], universe=universe)

    assert readiness.ready is True
    assert readiness.coverage == 0.98


def test_history_hash_detects_revision(tmp_path) -> None:
    days = _days(date(2026, 11, 10), 4)
    rows = []
    for day in days:
        rows.append(_bar_row("AAA", day))
        rows.append(_bar_row("BBB", day))
    for day in days:
        _write_partition(
            tmp_path, day, [row for row in rows if row["date"] == day]
        )

    before = date(2026, 11, 13)
    original = history_hashes(load_forward_bars(tmp_path), before)
    assert original.keys() == {"AAA", "BBB"}

    # A revised close on an earlier bar must change that symbol's hash.
    day = days[0]
    _write_partition(
        tmp_path,
        day,
        [_bar_row("AAA", day, close=99.0), _bar_row("BBB", day)],
    )
    revised = history_hashes(load_forward_bars(tmp_path), before)

    assert changed_symbols(original, revised) == ["AAA"]
    assert changed_symbols(original, original) == []

def test_preserve_keeps_every_enriched_column(tmp_path) -> None:
    # The morning engine store (Task 6) reads full enriched rows, not only the
    # research bar columns, so the preserved copy must keep every column.
    day = date(2026, 11, 12)
    row = {**_bar_row("AAA", day), "ma20": 10.1, "ema12": 10.2}
    _write_partition(tmp_path, day, [row])

    preserve_bars(tmp_path, ["AAA"], through=day)

    path = tmp_path / "forward" / "bars" / f"date={day.isoformat()}" / "part.parquet"
    columns = pl.read_parquet(path).columns
    assert "ma20" in columns
    assert "ema12" in columns
