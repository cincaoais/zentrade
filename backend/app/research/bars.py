"""Read enriched daily bars and the instrument filter from the local store."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.research import RESEARCH_LAST_DATE

BAR_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "raw_close",
)

# moomoo reported a non-zero ``forward_adj_factorB`` for these, so their
# adjustment events were skipped and their prices are unadjusted.
EXCLUDED_SYMBOLS = frozenset({"DPM", "GLDE", "SMX", "STAFQ", "SVA", "VLPIX"})

_FLAG_COLUMNS = ("is_etf", "is_preferred", "is_warrant", "is_unit")


def load_bars(
    data_dir: Path,
    symbols: list[str] | None = None,
    start: date | None = None,
    end: date = RESEARCH_LAST_DATE,
) -> pl.DataFrame:
    """Load enriched daily bars, clipped to ``RESEARCH_LAST_DATE``.

    Partitions are ``kline_daily_enriched/date=YYYY-MM-DD/part.parquet``. The
    files carry their own ``date`` column, so the hive partition is not read.
    """
    data_dir = Path(data_dir)
    end = min(end, RESEARCH_LAST_DATE)
    glob = str(data_dir / "kline_daily_enriched" / "date=*" / "part.parquet")

    lf = pl.scan_parquet(glob, hive_partitioning=False)
    lf = lf.filter(pl.col("date") <= pl.lit(end))
    if start is not None:
        lf = lf.filter(pl.col("date") >= pl.lit(start))
    if symbols is not None:
        lf = lf.filter(pl.col("symbol").is_in(list(symbols)))

    return (
        lf.select(list(BAR_COLUMNS))
        .sort(["symbol", "date"])
        .collect()
    )


def tradable_symbols(data_dir: Path) -> set[str]:
    """Symbols the harness may trade, from the broker instrument reference."""
    path = Path(data_dir) / "instruments" / "instruments.parquet"
    df = pl.read_parquet(path)

    is_stock = pl.col("asset_type") == "stock"
    for column in _FLAG_COLUMNS:
        is_stock = is_stock & ~pl.col(column).fill_null(False)
    not_right = (pl.col("instrument_type") != "right").fill_null(True)

    symbols = df.filter(is_stock & not_right)["symbol"].to_list()
    return set(symbols) - EXCLUDED_SYMBOLS