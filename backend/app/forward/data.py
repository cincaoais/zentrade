"""Forward bars, universe snapshots and readiness checks.

The research loader (``app.research.bars.load_bars``) clips at
``RESEARCH_LAST_DATE``. The forward runner needs the same frames without that
clip, plus a private copy of held symbols that the daily pipeline may prune.

Step 0 discovery (2026-11-18, reading the daily pipeline):

* ``jobs/daily_pipeline._prune_partial_enriched_partitions`` (line 38) and
  ``_prune_stale_price_partitions`` (line 77) delete whole ``date=*`` enriched
  partitions, never individual symbols; the next incremental pass rebuilds the
  partition for every symbol present in that day's ``kline_daily`` rows.
* ``services.instrument_sync`` only overwrites ``instruments/instruments.parquet``
  (``atomic_write_parquet``, line 113); it never deletes kline rows.
* Consequently a delisted symbol's historical rows normally survive: the
  instrument list drops it, but past partitions keep its bars. Only a pruned
  partition that is rebuilt without the symbol could lose rows, so
  ``preserve_bars`` keeps a copy for held symbols and the night step calls it
  only for those symbols.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NamedTuple

import polars as pl

from app.research.bars import BAR_COLUMNS, tradable_symbols


class Readiness(NamedTuple):
    ready: bool
    missing: list[str]
    coverage: float
    checked_at: str

def _has_parquet(pattern: Path) -> bool:
    # ``pattern`` looks like ``<root>/date=*/part.parquet``; glob from its
    # grandparent so the literal ``date=*`` directory is still expanded.
    return any(pattern.parent.parent.glob(f"{pattern.parent.name}/{pattern.name}"))

def _scan_bars(
    glob: Path,
    symbols: list[str] | None,
    start: date | None,
    end: date | None,
) -> pl.DataFrame:
    lf = pl.scan_parquet(str(glob), hive_partitioning=False)
    if end is not None:
        lf = lf.filter(pl.col("date") <= pl.lit(end))
    if start is not None:
        lf = lf.filter(pl.col("date") >= pl.lit(start))
    if symbols is not None:
        lf = lf.filter(pl.col("symbol").is_in(list(symbols)))

    return lf.select(list(BAR_COLUMNS)).sort(["symbol", "date"]).collect()

def _store_glob(data_dir: Path) -> Path:
    return data_dir / "kline_daily_enriched" / "date=*" / "part.parquet"

def _forward_glob(data_dir: Path) -> Path:
    return data_dir / "forward" / "bars" / "date=*" / "part.parquet"

def _atomic_write_parquet(frame: pl.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        frame.write_parquet(tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()

def load_forward_bars(
    data_dir: Path,
    symbols: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pl.DataFrame:
    """Load enriched bars without the research cutoff, including preserved rows.

    The source store is ``kline_daily_enriched/date=*/part.parquet``. Rows copied
    by :func:`preserve_bars` live under ``forward/bars/date=*/part.parquet``; on
    a duplicate ``(symbol, date)`` the store row wins. ``end=None`` means no
    upper bound.
    """
    data_dir = Path(data_dir)
    bars = _scan_bars(_store_glob(data_dir), symbols, start, end)

    forward_glob = _forward_glob(data_dir)
    if not _has_parquet(forward_glob):
        return bars

    preserved = _scan_bars(forward_glob, symbols, start, end)
    if preserved.is_empty():
        return bars

    combined = pl.concat(
        [
            bars.with_columns(pl.lit(0, dtype=pl.UInt8).alias("_source")),
            preserved.with_columns(pl.lit(1, dtype=pl.UInt8).alias("_source")),
        ],
        how="vertical",
    )
    return (
        combined.sort(["symbol", "date", "_source"])
        .unique(subset=["symbol", "date"], keep="first", maintain_order=True)
        .select(list(BAR_COLUMNS))
        .sort(["symbol", "date"])
    )

def snapshot_universe(data_dir: Path, d: date) -> Path:
    """Append today's tradable list as ``forward/universe/<d>.parquet``.

    The snapshot is write-once: re-running with the identical list returns the
    path, while a different list raises ``FileExistsError`` because a forward
    day's universe must never be rewritten.
    """
    data_dir = Path(data_dir)
    path = data_dir / "forward" / "universe" / f"{d.isoformat()}.parquet"
    symbols = sorted(tradable_symbols(data_dir))
    frame = pl.DataFrame({"symbol": symbols}, schema={"symbol": pl.String})

    if path.exists():
        existing = pl.read_parquet(path, columns=["symbol"])["symbol"].to_list()
        if existing == symbols:
            return path
        raise FileExistsError(
            f"universe snapshot {path} already exists with a different symbol list"
        )

    _atomic_write_parquet(frame, path)
    return path

def forward_tradable(data_dir: Path, d: date) -> set[str]:
    """Union of every snapshot dated on or before ``d``.

    A symbol that was ever in a forward snapshot stays tradable for the record,
    so a later delisting does not drop it from the forward universe.
    """
    snapshot_dir = Path(data_dir) / "forward" / "universe"
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"no forward universe snapshots under {snapshot_dir}")

    symbols: set[str] = set()
    found = False
    for path in snapshot_dir.glob("*.parquet"):
        try:
            snapshot_day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if snapshot_day <= d:
            found = True
            symbols.update(pl.read_parquet(path, columns=["symbol"])["symbol"].to_list())

    if not found:
        raise FileNotFoundError(f"no forward universe snapshot on or before {d}")
    return symbols

def check_readiness(
    data_dir: Path,
    t: date,
    required: list[str],
    universe: list[str],
    min_coverage: float = 0.98,
) -> Readiness:
    """Bars on ``t`` must cover every required symbol and ``min_coverage`` of the universe."""
    bars = load_forward_bars(Path(data_dir), start=t, end=t)
    present = set(bars["symbol"].to_list()) if not bars.is_empty() else set()

    missing = sorted(set(required) - present)
    universe_set = set(universe)
    coverage = (
        len(universe_set & present) / len(universe_set) if universe_set else 1.0
    )
    return Readiness(
        ready=not missing and coverage >= min_coverage,
        missing=missing,
        coverage=coverage,
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )

def history_hashes(bars: pl.DataFrame, before: date) -> dict[str, str]:
    """Per-symbol sha256 of the canonical rows dated before ``before``."""
    columns = [column for column in BAR_COLUMNS if column in bars.columns]
    canonical = (
        bars.filter(pl.col("date") < pl.lit(before))
        .select(columns)
        .sort(["symbol", "date"])
    )

    hashes: dict[str, str] = {}
    for (symbol,), group in canonical.group_by("symbol", maintain_order=True):
        payload = group.drop("symbol").write_csv()
        hashes[symbol] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return hashes

def changed_symbols(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Symbols whose history hash differs or is missing on either side."""
    return sorted(
        symbol
        for symbol in set(old) | set(new)
        if old.get(symbol) != new.get(symbol)
    )

def preserve_bars(data_dir: Path, symbols: list[str], through: date) -> int:
    """Copy ``symbols``' enriched rows through ``through`` into the forward store.

    Rows are appended per date partition (other symbols in an existing forward
    partition are kept) and the copy is idempotent. Returns the number of source
    rows written.
    """
    data_dir = Path(data_dir)
    # Full enriched rows (not only BAR_COLUMNS): the morning engine store reads them.
    source = (
        pl.scan_parquet(str(_store_glob(data_dir)), hive_partitioning=False)
        .filter(pl.col("date") <= pl.lit(through))
        .filter(pl.col("symbol").is_in(list(symbols)))
        .sort(["symbol", "date"])
        .collect()
    )
    if source.is_empty():
        return 0

    written = 0
    for (day,), part in source.group_by("date", maintain_order=True):
        target = (
            data_dir / "forward" / "bars" / f"date={day.isoformat()}" / "part.parquet"
        )
        if target.exists():
            existing = pl.read_parquet(target).filter(
                ~pl.col("symbol").is_in(list(symbols))
            )
            part = pl.concat([existing, part], how="vertical_relaxed")

        _atomic_write_parquet(part.sort(["symbol", "date"]), target)
        written += source.filter(pl.col("date") == day).height

    return written