"""Point-in-time universe eligibility (no look-ahead)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl


@dataclass(frozen=True)
class UniverseParams:
    min_bars: int = 250
    min_price: float = 5.0
    min_dollar_vol: float = 10_000_000.0
    top_n: int = 1500

    def hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_ELIGIBILITY_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "n_bars": pl.UInt32,
    "dv_med20": pl.Float64,
    "eligible": pl.Boolean,
    "rank": pl.Int64,
}


def eligibility(
    bars: pl.DataFrame, tradable: set[str], params: UniverseParams
) -> pl.DataFrame:
    """Per-symbol, per-day eligibility using data available on that day.

    ``n_bars`` counts stored bars up to and including ``t``. ``dv_med20`` is the
    rolling median of the unadjusted ``amount`` over the last 20 stored bars.
    ``rank`` is 1-based among symbols that pass the three base rules that day,
    highest dollar volume first and ties broken by symbol ascending.
    """
    if bars.is_empty():
        return pl.DataFrame(schema=_ELIGIBILITY_SCHEMA)

    frame = bars.sort(["symbol", "date"]).with_columns(
        pl.col("date").cum_count().over("symbol").alias("n_bars"),
        pl.col("amount")
        .rolling_median(window_size=20, min_samples=20)
        .over("symbol")
        .alias("dv_med20"),
    )

    # Non-tradable names (SPY/QQQ, ETFs, excluded symbols) must not take top-N
    # slots, so tradability is part of the rule set that is ranked.
    passes = (
        (pl.col("n_bars") >= params.min_bars)
        & (pl.col("raw_close") >= params.min_price)
        & (pl.col("dv_med20") >= params.min_dollar_vol)
        & pl.col("symbol").is_in(sorted(tradable))
    ).fill_null(False)
    frame = frame.with_columns(passes.alias("_passes"))

    # Rank only the symbols that pass the base rules, by dollar volume.
    frame = frame.sort(
        ["date", "dv_med20", "symbol"], descending=[False, True, False], nulls_last=True
    ).with_columns(
        pl.col("_passes").cum_sum().over("date").cast(pl.Int64).alias("_rank")
    )
    frame = frame.with_columns(
        pl.when(pl.col("_passes"))
        .then(pl.col("_rank"))
        .otherwise(None)
        .alias("rank"),
        (pl.col("_passes") & (pl.col("_rank") <= params.top_n))
        .fill_null(False)
        .alias("eligible"),
    )

    return frame.select(list(_ELIGIBILITY_SCHEMA)).sort(["symbol", "date"])


def universe_path(data_dir: Path, params: UniverseParams) -> Path:
    return Path(data_dir) / "research" / "universe" / f"{params.hash()}.parquet"