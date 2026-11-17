"""Replay point-in-time Wyckoff events and ranges for every symbol in the research universe.

Usage::

    cd backend
    uv run python scripts/research/replay_wyckoff.py [--workers N] [--symbols A,B] [--limit N]

Without ``--symbols`` the CLI rebuilds the point-in-time universe parquet and
replays the union of symbols eligible on any date (the same universe as the
Chan CLI). Existing parts are skipped, so re-running resumes an interrupted
replay.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import polars as pl

from app.config import settings
from app.research.bars import load_bars, tradable_symbols
from app.research.universe import UniverseParams, eligibility, universe_path
from app.research.wyckoff.replay import run_wyckoff_replay

logger = logging.getLogger("replay_wyckoff")


def _default_workers() -> int:
    return max(1, min(12, (os.cpu_count() or 1) - 2))


def _write_universe(data_dir: Path) -> pl.DataFrame:
    """Rebuild the universe parquet from the current store (about 3 s).

    The path is keyed by the threshold params only, not by the data, so reusing
    an existing file could silently replay a universe from older data.
    """
    params = UniverseParams()
    path = universe_path(data_dir, params)
    logger.info("building universe from %s", data_dir)
    bars = load_bars(data_dir)
    frame = eligibility(bars, tradable_symbols(data_dir), params)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        frame.write_parquet(tmp, compression="zstd", statistics=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    logger.info("wrote universe %s", path)
    return frame


def _universe_symbols(data_dir: Path, limit: int | None) -> list[str]:
    frame = _write_universe(data_dir)
    symbols = frame.filter(pl.col("eligible"))["symbol"].unique().sort().to_list()
    if limit is not None:
        symbols = symbols[:limit]
    return symbols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay Wyckoff events and ranges into parquet parts"
    )
    parser.add_argument(
        "--data-dir",
        default=str(settings.data_dir),
        help="ZenTrade data directory (default: configured data dir)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=_default_workers(),
        help="worker processes (default: min(12, cpu_count - 2))",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="comma separated symbols (default: union of the universe)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the number of symbols, applied after sorting",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(args.data_dir).resolve()

    if args.symbols:
        symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
        if args.limit is not None:
            symbols = symbols[: args.limit]
    else:
        symbols = _universe_symbols(data_dir, args.limit)

    logger.info("wyckoff replay: %d symbols, %d workers", len(symbols), args.workers)
    result = run_wyckoff_replay(data_dir, symbols, args.workers)
    print(json.dumps(result, indent=1))
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())