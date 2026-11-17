"""Pre-declared ranking features for the phase-2 grid.

Every value is causal: it only reads bars at or before the decision date ``t``.
``rs63``/``relvol`` use stored bars, ``atr14`` is Wilder ATR(14) and retracement
reads only the Chan stroke bars named by the point.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from app.research.features import atr14

RANKS = ("rs", "retr", "risk", "relvol", "lowvol")
RS_LOOKBACK = 63

_LEG_COLUMNS = (
    "leg_prev_start",
    "leg_prev_end",
    "leg_last_start",
    "leg_last_end",
)

_FEATURE_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "close": pl.Float64,
    "atr14": pl.Float64,
    "rs63": pl.Float64,
    "relvol": pl.Float64,
    "atrp": pl.Float64,
}

_RETR_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "retr_score": pl.Float64,
}


def _as_days(values: pl.Series) -> np.ndarray:
    """``pl.Date`` values as integer day numbers for ``np.searchsorted``."""
    return values.to_numpy().astype("datetime64[D]").astype(np.int64)


def _rel_volume(volume: np.ndarray) -> np.ndarray:
    """Vectorised ``features.rel_volume``: volume over the previous 20 bars.

    The per-symbol sliding window avoids one Python-level median call per bar
    while producing the same values (including the NaN warm-up).
    """
    volume = np.asarray(volume, dtype=float)
    n = volume.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= 20:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(volume, 20)[: n - 20]
    medians = np.median(windows, axis=1)
    values = volume[20:]
    usable = (medians != 0) & ~np.isnan(medians)
    out[20:][usable] = values[usable] / medians[usable]
    return out


def _day_number(day: date) -> int:
    return int(np.datetime64(day, "D").astype(np.int64))


def rank_features(bars: pl.DataFrame, spy: pl.DataFrame) -> pl.DataFrame:
    """Causal ranking features for every stored bar.

    ``atr14`` is computed per symbol unless ``bars`` already carries it. SPY
    returns are computed on SPY's own bars and joined by date, so a missing
    SPY bar yields a null ``rs63``.
    """
    if bars.is_empty():
        return pl.DataFrame(schema=_FEATURE_SCHEMA)

    frame = bars.sort(["symbol", "date"])
    if "atr14" in frame.columns:
        frame = frame.with_columns(pl.col("atr14").cast(pl.Float64))
    else:
        parts = [
            group.with_columns(
                pl.Series(
                    "atr14",
                    atr14(
                        group["high"].to_numpy(),
                        group["low"].to_numpy(),
                        group["close"].to_numpy(),
                    ),
                )
            )
            for group in frame.partition_by("symbol", maintain_order=True)
        ]
        frame = pl.concat(parts)

    frame = pl.concat(
        [
            group.with_columns(
                pl.Series("relvol", _rel_volume(group["volume"].to_numpy()))
            )
            for group in frame.partition_by("symbol", maintain_order=True)
        ]
    )

    frame = frame.with_columns(
        (pl.col("close") / pl.col("close").shift(RS_LOOKBACK).over("symbol")).alias(
            "_ret"
        )
    )

    spy_returns = (
        spy.select("date", "close")
        .sort("date")
        .with_columns(
            (pl.col("close") / pl.col("close").shift(RS_LOOKBACK)).alias("_spy_ret")
        )
        .select("date", "_spy_ret")
    )
    frame = frame.join(spy_returns, on="date", how="left")

    return (
        frame.with_columns(
            (pl.col("_ret") - pl.col("_spy_ret")).alias("rs63"),
            (pl.col("atr14") / pl.col("close")).alias("atrp"),
        )
        .select(*_FEATURE_SCHEMA)
        .with_columns(pl.col("atr14", "rs63", "relvol", "atrp").fill_nan(None))
        .sort(["symbol", "date"])
    )


def _stroke_range(
    days: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    start: date,
    end: date,
) -> float | None:
    """``max(high) - min(low)`` over the inclusive bar slice ``[start, end]``."""
    left = int(np.searchsorted(days, _day_number(start), side="left"))
    right = int(np.searchsorted(days, _day_number(end), side="right"))
    if right <= left:
        return None
    stroke_highs = highs[left:right]
    stroke_lows = lows[left:right]
    if not np.isfinite(stroke_highs).all() or not np.isfinite(stroke_lows).all():
        return None
    return float(stroke_highs.max() - stroke_lows.min())


def chan_retracement(
    points: pl.DataFrame,
    bars: pl.DataFrame,
    types: tuple[str, ...],
    date_column: str,
) -> pl.DataFrame:
    """Retracement score ``-range(leg_last) / range(leg_prev)`` per point.

    The point date is ``date_column`` (``known_date`` or ``confirmed_date``).
    Null undated points are dropped; a leg date later than the point date is a
    lookahead bug and raises ``ValueError`` naming the symbol.
    """
    if points.is_empty():
        return pl.DataFrame(schema=_RETR_SCHEMA)

    selected = points.filter(pl.col("type").is_in(types))
    if selected.is_empty():
        return pl.DataFrame(schema=_RETR_SCHEMA)

    for leg in _LEG_COLUMNS:
        future = selected.filter(
            pl.col(date_column).is_not_null()
            & pl.col(leg).is_not_null()
            & (pl.col(leg) > pl.col(date_column))
        )
        if not future.is_empty():
            symbol = future["symbol"][0]
            raise ValueError(
                f"{symbol}: {leg} is after {date_column} (lookahead in point)"
            )

    selected = selected.filter(pl.col(date_column).is_not_null())
    if selected.is_empty():
        return pl.DataFrame(schema=_RETR_SCHEMA)

    index: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for group in bars.sort(["symbol", "date"]).partition_by(
        "symbol", maintain_order=True
    ):
        symbol = group["symbol"][0]
        index[symbol] = (
            _as_days(group["date"]),
            group["high"].to_numpy().astype(float),
            group["low"].to_numpy().astype(float),
        )

    records: list[dict] = []
    for row in selected.iter_rows(named=True):
        symbol = row["symbol"]
        score: float | None = None
        legs = [row[leg] for leg in _LEG_COLUMNS]
        if symbol in index and all(leg is not None for leg in legs):
            days, highs, lows = index[symbol]
            prev_range = _stroke_range(days, highs, lows, legs[0], legs[1])
            last_range = _stroke_range(days, highs, lows, legs[2], legs[3])
            if (
                prev_range is not None
                and prev_range != 0
                and last_range is not None
            ):
                score = -(last_range / prev_range)
        records.append(
            {"symbol": symbol, "date": row[date_column], "retr_score": score}
        )

    frame = pl.DataFrame(records, schema=_RETR_SCHEMA)
    return frame.group_by(["symbol", "date"], maintain_order=True).agg(
        pl.col("retr_score").max()
    )


def _joined(
    entries: pl.DataFrame,
    lookup: pl.DataFrame,
    value: pl.Expr,
    extra_columns: tuple[str, ...],
) -> pl.DataFrame:
    """Left-join lookup columns onto ``entries``, preserving every entry row."""
    original = entries.drop("raw_score") if "raw_score" in entries.columns else entries
    joined = original.with_row_index("__row").join(
        lookup.select("symbol", "date", *extra_columns),
        on=["symbol", "date"],
        how="left",
    )
    return (
        joined.with_columns(value.cast(pl.Float64).alias("raw_score"))
        .sort("__row")
        .drop("__row")
        .select(*original.columns, "raw_score")
    )


def raw_scores(
    entries: pl.DataFrame,
    rank: str,
    features: pl.DataFrame,
    retr: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Attach ``raw_score`` (Float64) for ``rank`` to every entry row.

    Higher is always better. Null scores (missing features, missing legs) are
    kept for the composer to order last.
    """
    if rank not in RANKS:
        raise ValueError(f"unknown rank {rank!r}; expected one of {RANKS}")

    if rank == "retr":
        if retr is None:
            raise ValueError("rank 'retr' requires a retr frame")
        original = (
            entries.drop("raw_score") if "raw_score" in entries.columns else entries
        )
        lookup = retr.select(
            "symbol", "date", pl.col("retr_score").cast(pl.Float64).alias("raw_score")
        )
        return _joined(original, lookup, pl.col("raw_score"), ("raw_score",))

    if rank == "rs":
        value = pl.col("rs63")
    elif rank == "risk":
        value = (
            pl.when(pl.col("atr14").is_null() | (pl.col("atr14") == 0.0))
            .then(None)
            .otherwise(
                -(pl.col("close") - pl.col("invalidation")) / pl.col("atr14")
            )
        )
    elif rank == "relvol":
        value = pl.col("relvol")
    else:  # lowvol
        value = -pl.col("atr14") / pl.col("close")
    return _joined(
        entries,
        features,
        value,
        ("close", "atr14", "rs63", "relvol", "atrp"),
    )