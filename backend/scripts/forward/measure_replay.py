"""Measure fresh Chan and Wyckoff point replays on selected symbols.

Copies the specified symbols' bars from a data directory into a temporary directory
(treating the source data directory as read-only), runs both replays fresh,
measures wall time and peak RSS/memory, and outputs a JSON summary with
extrapolations to the forward universe.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl

from app.research.bars import BAR_COLUMNS
from app.research.chan_replay import ENGINE_COLUMNS, _real_snapshot, replay_symbol
from app.research.versions import chan_engine_version, wyckoff_port_version
from app.research.wyckoff.replay import BAR_INPUT_COLUMNS, wyckoff_symbol

logger = logging.getLogger("measure_replay")

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    HAS_PSUTIL = False


class MemoryTimeTracker:
    """Context manager to measure wall time and peak memory (RSS via psutil, or tracemalloc)."""

    def __init__(self) -> None:
        self.wall_time_sec: float = 0.0
        self.peak_memory_mb: float = 0.0
        self.rss_after_mb: float = 0.0
        self._start_time: float = 0.0
        self._proc = psutil.Process() if HAS_PSUTIL and psutil is not None else None

    def __enter__(self) -> MemoryTimeTracker:
        if not HAS_PSUTIL:
            import tracemalloc

            tracemalloc.start()
        self._start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.wall_time_sec = round(time.perf_counter() - self._start_time, 4)
        if HAS_PSUTIL and self._proc is not None:
            mem_info = self._proc.memory_info()
            peak = getattr(mem_info, "peak_wset", mem_info.rss)
            self.peak_memory_mb = round(peak / (1024 * 1024), 2)
            self.rss_after_mb = round(mem_info.rss / (1024 * 1024), 2)
        else:
            import tracemalloc

            _curr, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.peak_memory_mb = round(peak / (1024 * 1024), 2)
            self.rss_after_mb = self.peak_memory_mb


def find_median_symbols(data_dir: Path, n: int = 5) -> list[str]:
    """Scan data_dir (read-only) and return n symbols closest to median bar count."""
    glob_pattern = str(data_dir / "kline_daily_enriched" / "date=*" / "part.parquet")
    lf = pl.scan_parquet(glob_pattern, hive_partitioning=False)
    counts = lf.group_by("symbol").agg(pl.len().alias("count")).sort("count").collect()
    if counts.is_empty():
        return []
    med_idx = len(counts) // 2
    start = max(0, med_idx - n // 2)
    end = min(len(counts), start + n)
    if end - start < n and start > 0:
        start = max(0, end - n)
    return counts.slice(start, end - start)["symbol"].to_list()


def copy_bars_to_temp_dir(
    source_data_dir: Path, temp_dir: Path, symbols: list[str]
) -> pl.DataFrame:
    """Read symbols' bars from source_data_dir (read-only) and write partitioned copies to temp_dir."""
    glob_pattern = str(source_data_dir / "kline_daily_enriched" / "date=*" / "part.parquet")
    lf = pl.scan_parquet(glob_pattern, hive_partitioning=False)
    lf = lf.filter(pl.col("symbol").is_in(symbols))
    bars = lf.select(list(BAR_COLUMNS)).sort(["symbol", "date"]).collect()
    if bars.is_empty():
        raise ValueError(f"No bars found in {source_data_dir} for symbols: {', '.join(symbols)}")

    # Write partitioned into temp_dir / "kline_daily_enriched" / f"date={d}" / "part.parquet"
    for (date_val,), group in bars.group_by(["date"]):
        part_dir = temp_dir / "kline_daily_enriched" / f"date={date_val}"
        part_dir.mkdir(parents=True, exist_ok=True)
        group.write_parquet(part_dir / "part.parquet", compression="zstd")

    return bars


def load_symbol_bars_from_temp(temp_dir: Path, symbol: str) -> pd.DataFrame:
    """Load the single symbol's bars from temp_dir as a pandas DataFrame."""
    glob_pattern = str(temp_dir / "kline_daily_enriched" / "date=*" / "part.parquet")
    return (
        pl.scan_parquet(glob_pattern, hive_partitioning=False)
        .filter(pl.col("symbol") == symbol)
        .select(list(BAR_COLUMNS))
        .sort("date")
        .collect()
        .to_pandas()
    )


def measure_symbol_replay(
    symbol: str,
    bars_pd: pd.DataFrame,
    chan_version: str,
    wyckoff_version: str,
) -> dict[str, Any]:
    """Run fresh Chan and Wyckoff replays on one symbol, recording timing and memory."""
    missing_chan = [col for col in ENGINE_COLUMNS if col not in bars_pd.columns]
    if missing_chan:
        raise ValueError(f"bars frame is missing chan columns: {', '.join(missing_chan)}")
    chan_frame = bars_pd.loc[:, list(ENGINE_COLUMNS)]

    missing_wyk = [col for col in BAR_INPUT_COLUMNS if col not in bars_pd.columns]
    if missing_wyk:
        raise ValueError(f"bars frame is missing wyckoff columns: {', '.join(missing_wyk)}")
    wyckoff_frame = bars_pd.loc[:, list(BAR_INPUT_COLUMNS)]

    # Measure Chan replay
    with MemoryTimeTracker() as chan_tracker:
        points, gone, chan_stats = replay_symbol(
            symbol,
            chan_frame,
            _real_snapshot,
            chan_version,
        )

    # Measure Wyckoff replay
    with MemoryTimeTracker() as wyckoff_tracker:
        events, ranges = wyckoff_symbol(
            symbol,
            wyckoff_frame,
            wyckoff_version,
        )

    dates = bars_pd["date"].tolist()
    start_date = str(dates[0]) if dates else None
    end_date = str(dates[-1]) if dates else None

    return {
        "symbol": symbol,
        "bar_count": len(bars_pd),
        "start_date": start_date,
        "end_date": end_date,
        "chan": {
            "wall_time_sec": chan_tracker.wall_time_sec,
            "peak_memory_mb": chan_tracker.peak_memory_mb,
            "rss_after_mb": chan_tracker.rss_after_mb,
            "points_count": len(points),
            "gone_count": len(gone),
            "error_days": chan_stats.get("error_days", 0),
            "available_days": chan_stats.get("available_days", 0),
        },
        "wyckoff": {
            "wall_time_sec": wyckoff_tracker.wall_time_sec,
            "peak_memory_mb": wyckoff_tracker.peak_memory_mb,
            "rss_after_mb": wyckoff_tracker.rss_after_mb,
            "events_count": len(events),
            "ranges_count": len(ranges),
        },
    }


def compute_summary(
    results: list[dict[str, Any]], universe_size: int = 500
) -> dict[str, Any]:
    """Aggregate per-symbol results and compute extrapolations to universe_size."""
    if not results:
        return {}

    n = len(results)
    total_bars = sum(r["bar_count"] for r in results)
    mean_bars = round(total_bars / n, 1)

    chan_times = [r["chan"]["wall_time_sec"] for r in results]
    chan_total_time = round(sum(chan_times), 4)
    chan_mean_time = round(chan_total_time / n, 4)
    chan_peak_mem = max(r["chan"]["peak_memory_mb"] for r in results)

    chan_extrapolated_sec = round(chan_mean_time * universe_size, 2)
    chan_extrapolated_min_1w = round(chan_extrapolated_sec / 60.0, 2)
    chan_extrapolated_min_8w = round(chan_extrapolated_sec / (60.0 * 8), 2)

    wyk_times = [r["wyckoff"]["wall_time_sec"] for r in results]
    wyk_total_time = round(sum(wyk_times), 4)
    wyk_mean_time = round(wyk_total_time / n, 4)
    wyk_peak_mem = max(r["wyckoff"]["peak_memory_mb"] for r in results)

    wyk_extrapolated_sec = round(wyk_mean_time * universe_size, 2)
    wyk_extrapolated_min_1w = round(wyk_extrapolated_sec / 60.0, 2)
    wyk_extrapolated_min_8w = round(wyk_extrapolated_sec / (60.0 * 8), 2)

    total_combined_extrapolated_min_8w = round(
        chan_extrapolated_min_8w + wyk_extrapolated_min_8w, 2
    )

    return {
        "measured_symbol_count": n,
        "total_bars": total_bars,
        "mean_bars_per_symbol": mean_bars,
        "universe_size": universe_size,
        "chan": {
            "total_wall_time_sec": chan_total_time,
            "mean_wall_time_per_symbol_sec": chan_mean_time,
            "peak_memory_mb": chan_peak_mem,
            "extrapolated_universe_wall_time_sec_1_worker": chan_extrapolated_sec,
            "extrapolated_universe_wall_time_min_1_worker": chan_extrapolated_min_1w,
            "extrapolated_universe_wall_time_min_8_workers": chan_extrapolated_min_8w,
        },
        "wyckoff": {
            "total_wall_time_sec": wyk_total_time,
            "mean_wall_time_per_symbol_sec": wyk_mean_time,
            "peak_memory_mb": wyk_peak_mem,
            "extrapolated_universe_wall_time_sec_1_worker": wyk_extrapolated_sec,
            "extrapolated_universe_wall_time_min_1_worker": wyk_extrapolated_min_1w,
            "extrapolated_universe_wall_time_min_8_workers": wyk_extrapolated_min_8w,
        },
        "combined_extrapolated_min_8_workers": total_combined_extrapolated_min_8w,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure fresh Chan and Wyckoff point replays on selected symbols."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Path to source data directory containing kline_daily_enriched/ (treated read-only)",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Symbols to measure (comma-separated or multiple arguments). If omitted, 5 symbols of median history length are picked.",
    )
    parser.add_argument(
        "--universe-size",
        type=int,
        default=500,
        help="Forward universe size for time extrapolation (default: 500)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress logs on stderr",
    )
    args = parser.parse_args(argv)

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s")

    data_dir = args.data_dir.resolve()
    if not data_dir.is_dir():
        logger.error("Data directory does not exist: %s", data_dir)
        return 1

    symbols: list[str] = []
    if args.symbols:
        for arg in args.symbols:
            for s in arg.split(","):
                s = s.strip()
                if s and s not in symbols:
                    symbols.append(s)

    if not symbols:
        logger.info("Scanning %s for median history symbols...", data_dir)
        symbols = find_median_symbols(data_dir, n=5)
        logger.info("Auto-selected median symbols: %s", ", ".join(symbols))

    if not symbols:
        logger.error("No symbols specified or found in %s", data_dir)
        return 1

    chan_version = chan_engine_version()
    wyckoff_version = wyckoff_port_version()

    with tempfile.TemporaryDirectory() as tmp_str:
        temp_dir = Path(tmp_str)
        logger.info(
            "Copying bars for %d symbols to temp dir %s (read-only on source)...",
            len(symbols),
            temp_dir,
        )
        copy_bars_to_temp_dir(data_dir, temp_dir, symbols)

        results: list[dict[str, Any]] = []
        for symbol in symbols:
            logger.info("Measuring replay for %s...", symbol)
            sym_bars = load_symbol_bars_from_temp(temp_dir, symbol)
            res = measure_symbol_replay(symbol, sym_bars, chan_version, wyckoff_version)
            results.append(res)

    summary = compute_summary(results, universe_size=args.universe_size)
    output = {
        "memory_tracker": "psutil" if HAS_PSUTIL else "tracemalloc",
        "has_psutil": HAS_PSUTIL,
        "chan_version": chan_version,
        "wyckoff_version": wyckoff_version,
        "symbols": symbols,
        "symbol_results": results,
        "summary": summary,
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
