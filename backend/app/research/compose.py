"""Composer core: common entry/exit mechanics for every research variant.

Every signal table is turned into the single ``(symbol, date, entry, exit, ...)``
shape ZenTrade consumes. All inputs are point-in-time frames; any column that
could leak the future is rejected.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

import polars as pl

from app.research.features import atr14

FORBIDDEN_INPUT_COLUMNS = frozenset(
    {"still_valid", "state", "gone_date", "end_idx", "end_reason", "bars_since"}
)

TABLE_COLUMNS = (
    "symbol",
    "date",
    "entry",
    "exit",
    "score",
    "invalidation",
    "atr14",
    "close",
)

_TABLE_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "entry": pl.Boolean,
    "exit": pl.Boolean,
    "score": pl.Float64,
    "invalidation": pl.Float64,
    "atr14": pl.Float64,
    "close": pl.Float64,
}


def assert_no_future_columns(df: pl.DataFrame) -> None:
    """Raise ``ValueError`` naming the first future-leaking column present."""
    for column in sorted(FORBIDDEN_INPUT_COLUMNS):
        if column in df.columns:
            raise ValueError(
                f"input frame carries forbidden future column {column!r}"
            )


@dataclass(frozen=True)
class Signals:
    """Raw signals for one variant: decision dates, not fill dates."""

    entries: pl.DataFrame  # symbol, date, invalidation, source
    sells: pl.DataFrame  # symbol, date


def entry_score(symbol: str, d: date) -> float:
    """Deterministic neutral tie-breaker in ``(0, 1)`` for ``(symbol, d)``."""
    digest = hashlib.sha256(f"{symbol}|{d.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _with_atr(frame: pl.DataFrame) -> pl.DataFrame:
    parts = [
        group.with_columns(
            pl.Series(
                "atr14",
                atr14(group["high"], group["low"], group["close"]),
            )
        )
        for _, group in frame.sort(["symbol", "date"]).group_by(
            ["symbol"], maintain_order=True
        )
    ]
    if not parts:
        return frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("atr14"))
    return pl.concat(parts)


def compose_table(
    signals: Signals, bars: pl.DataFrame, elig: pl.DataFrame
) -> tuple[pl.DataFrame, dict]:
    """Apply the shared entry/exit mechanics and return ``(table, log)``."""
    for frame in (signals.entries, signals.sells, bars, elig):
        assert_no_future_columns(frame)

    # 2. Merge several signals for the same (symbol, date); highest invalidation
    # wins because it is the tighter stop.
    merged = (
        signals.entries.select("symbol", "date", "invalidation", "source")
        .group_by(["symbol", "date"])
        .agg(
            pl.col("invalidation").max().alias("invalidation"),
            pl.col("source").sort().first().alias("source"),
        )
    )
    emitted = merged.height

    # 3. Point-in-time eligibility at the decision date.
    elig_frame = (
        elig.select("symbol", "date", "eligible")
        .unique(subset=["symbol", "date"], keep="first")
    )
    merged = merged.join(elig_frame, on=["symbol", "date"], how="left").with_columns(
        pl.col("eligible").fill_null(False)
    )
    ineligible = merged.filter(~pl.col("eligible")).height

    # 4. Fill-time check on the next stored bar's opening print.
    bars_core = bars.select("symbol", "date", "open", "high", "low", "close").sort(
        ["symbol", "date"]
    )
    next_open = (
        bars_core.select("symbol", "date", "open")
        .with_columns(pl.col("open").shift(-1).over("symbol").alias("next_open"))
        .select("symbol", "date", "next_open")
    )
    merged = merged.join(next_open, on=["symbol", "date"], how="left")

    gapped = pl.col("next_open").is_null() | (
        pl.col("next_open") <= pl.col("invalidation")
    ).fill_null(False)
    kept = merged.filter(pl.col("eligible") & ~gapped)
    gapped_through = emitted - ineligible - kept.height

    # 5/6. Active invalidation and exits on the stored bar grid.
    symbols = signals.sells["symbol"].to_list() + kept["symbol"].to_list()
    grid = bars_core.filter(pl.col("symbol").is_in(symbols))
    if grid.is_empty():
        table = pl.DataFrame(schema=_TABLE_SCHEMA)
        return table, {
            "emitted": emitted,
            "ineligible": ineligible,
            "gapped_through": gapped_through,
            "kept": kept.height,
            "exits": 0,
        }

    grid = _with_atr(grid)
    kept_marker = kept.select(
        "symbol",
        "date",
        pl.col("invalidation").alias("entry_invalidation"),
        pl.lit(True).alias("entry"),
    )
    grid = grid.join(kept_marker, on=["symbol", "date"], how="left").with_columns(
        pl.col("entry").fill_null(False),
        pl.col("entry_invalidation")
        .forward_fill()
        .over("symbol")
        .alias("active_invalidation"),
    )

    sells_marker = signals.sells.select("symbol", "date").unique().with_columns(
        pl.lit(True).alias("_sell")
    )
    grid = grid.join(sells_marker, on=["symbol", "date"], how="left").with_columns(
        pl.col("_sell").fill_null(False),
        (
            pl.col("active_invalidation").is_not_null()
            & (pl.col("close") < pl.col("active_invalidation"))
        ).alias("_stop_exit"),
    )
    grid = grid.with_columns(
        (pl.col("_stop_exit") | pl.col("_sell")).alias("exit")
    )

    # 7. Keep only signal rows, attach the neutral score, sort by (date, symbol).
    table = (
        grid.filter(pl.col("entry") | pl.col("exit"))
        .select(
            "symbol",
            "date",
            "entry",
            "exit",
            pl.col("active_invalidation").alias("invalidation"),
            "atr14",
            "close",
        )
        .with_columns(
            pl.struct(["symbol", "date", "entry"])
            .map_elements(
                lambda row: entry_score(row["symbol"], row["date"])
                if row["entry"]
                else 0.0,
                return_dtype=pl.Float64,
            )
            .alias("score")
        )
        .select(list(TABLE_COLUMNS))
        .sort(["date", "symbol"])
    )

    log = {
        "emitted": emitted,
        "ineligible": ineligible,
        "gapped_through": gapped_through,
        "kept": kept.height,
        "exits": table.filter(pl.col("exit")).height,
    }
    return table, log