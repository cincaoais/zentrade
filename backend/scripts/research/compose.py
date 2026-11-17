"""Compose the 18 research variants into ZenTrade signal tables.

Usage::

    cd backend
    uv run python scripts/research/compose.py [--variants H1-2B,H3-3B]

Every table is written to ``<data_dir>/research/variants/<variant>/<hash>.parquet``
and its path plus composer log counts are printed as one JSON object per line.
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
from app.research.chan_replay import load_chan_points
from app.research.compose import compose_table, with_atr14
from app.research.hypotheses import VARIANTS, Inputs, variant_path
from app.research.universe import UniverseParams, eligibility, universe_path

logger = logging.getLogger("compose")


def _eligibility(data_dir: Path, bars: pl.DataFrame) -> pl.DataFrame:
    """Point-in-time eligibility for every stored bar, written atomically."""
    params = UniverseParams()
    frame = eligibility(bars, tradable_symbols(data_dir), params)
    path = universe_path(data_dir, params)
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


def _write_table(path: Path, table: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        table.write_parquet(tmp, compression="zstd", statistics=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: list[str] | None = None) -> int:
    # Task 6 owns the Wyckoff replay module; keep the import out of module load.
    from app.research.wyckoff.replay import (
        load_wyckoff_events,
        load_wyckoff_ranges,
    )

    parser = argparse.ArgumentParser(
        description="Compose the 18 research hypotheses into signal tables"
    )
    parser.add_argument(
        "--data-dir",
        default=str(settings.data_dir),
        help="ZenTrade data directory (default: configured data dir)",
    )
    parser.add_argument(
        "--variants",
        default=None,
        help="comma separated variant ids (default: all 18)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(args.data_dir).resolve()

    if args.variants:
        variant_ids = [item.strip() for item in args.variants.split(",") if item.strip()]
        unknown = [item for item in variant_ids if item not in VARIANTS]
        if unknown:
            parser.error(f"unknown variant ids: {', '.join(unknown)}")
    else:
        variant_ids = list(VARIANTS)

    logger.info("loading research inputs from %s", data_dir)
    # ATR is computed once over the full store and reused by every variant.
    bars = with_atr14(load_bars(data_dir))
    inputs = Inputs(
        chan_points=load_chan_points(data_dir),
        wyk_events=load_wyckoff_events(data_dir),
        wyk_ranges=load_wyckoff_ranges(data_dir),
        bars=bars,
        spy=load_bars(data_dir, symbols=["SPY"]),
    )
    elig = _eligibility(data_dir, bars)

    for variant_id in variant_ids:
        signals = VARIANTS[variant_id](inputs)
        table, log = compose_table(signals, bars, elig)
        path = variant_path(data_dir, variant_id, table)
        _write_table(path, table)
        logger.info(
            "%s: %d rows, kept=%d exits=%d -> %s",
            variant_id,
            table.height,
            log["kept"],
            log["exits"],
            path,
        )
        print(json.dumps({"variant": variant_id, "path": str(path), "log": log}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
