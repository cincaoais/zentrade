"""The 18 v1 research hypotheses as signal builders.

Each variant turns point-in-time Chan points, Wyckoff events/ranges and stored
bars into a :class:`~app.research.compose.Signals`: entry rows carry the
decision date ``t`` and the invalidation price that the composer turns into a
shared entry/exit table. No variant reads anything else from the store.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from app.research.compose import Signals
from app.research.features import rel_volume
from app.research.wyckoff.accumulation import ACC_KINDS

CHAN_BUYS = ("1B", "2B", "3B")
CHAN_SELLS = ("1S", "2S", "3S")
WYK_BUYS = ("entry_steady", "sos", "lps")
WYK_SELLS = ("sow", "utad_confirmed", "sos_failed")

# "in the last 20 bars ... up to and including t" (20 bars back counts).
RECENT_EVENT_BARS = 20

_ENTRY_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "invalidation": pl.Float64,
    "source": pl.String,
}
_SELL_SCHEMA = {"symbol": pl.String, "date": pl.Date}


@dataclass(frozen=True)
class Inputs:
    """Everything the variants consume, loaded once for every variant."""

    chan_points: pl.DataFrame
    wyk_events: pl.DataFrame
    wyk_ranges: pl.DataFrame
    bars: pl.DataFrame
    spy: pl.DataFrame


# ---------------------------------------------------------------------------
# Small frame helpers
# ---------------------------------------------------------------------------


def _empty_entries() -> pl.DataFrame:
    return pl.DataFrame(schema=_ENTRY_SCHEMA)


def _empty_sells() -> pl.DataFrame:
    return pl.DataFrame(schema=_SELL_SCHEMA)


def _concat_entries(frames: list[pl.DataFrame]) -> pl.DataFrame:
    frames = [frame for frame in frames if frame.height]
    if not frames:
        return _empty_entries()
    return pl.concat([frame.select(list(_ENTRY_SCHEMA)) for frame in frames], how="vertical")


def _concat_sells(frames: list[pl.DataFrame]) -> pl.DataFrame:
    frames = [frame for frame in frames if frame.height]
    if not frames:
        return _empty_sells()
    return (
        pl.concat([frame.select("symbol", "date") for frame in frames], how="vertical")
        .unique()
        .sort(["date", "symbol"])
    )


def _wyk_invalidation_expr() -> pl.Expr:
    """Invalidation for one Wyckoff event, by kind."""
    return (
        pl.when(pl.col("kind") == "sos")
        .then(pl.col("range_hi") - 0.25 * pl.col("atr_known"))
        .otherwise(pl.col("stop_ref"))
    )


def _chan_entries(
    points: pl.DataFrame,
    types: tuple[str, ...],
    source: str,
    date_column: str = "known_date",
) -> pl.DataFrame:
    """Chan buy rows dated at ``known_date`` or ``confirmed_date``."""
    frame = points.filter(pl.col("type").is_in(types))
    if date_column == "confirmed_date":
        frame = frame.filter(pl.col("confirmed_date").is_not_null())
    return frame.select(
        "symbol",
        pl.col(date_column).alias("date"),
        pl.col("invalidation_price").alias("invalidation"),
        pl.lit(source).alias("source"),
    ).drop_nulls("date")


def _chan_sells(points: pl.DataFrame) -> pl.DataFrame:
    return (
        points.filter(pl.col("type").is_in(CHAN_SELLS))
        .select("symbol", pl.col("known_date").alias("date"))
        .unique()
    )


def _wyk_entries(events: pl.DataFrame, kinds: tuple[str, ...], source: str) -> pl.DataFrame:
    return (
        events.filter(pl.col("kind").is_in(kinds))
        .select(
            "symbol",
            "kind",
            "stop_ref",
            "range_hi",
            "atr_known",
            pl.col("known_date").alias("date"),
        )
        .with_columns(_wyk_invalidation_expr().alias("invalidation"))
        .select("symbol", "date", "invalidation", pl.lit(source).alias("source"))
        .drop_nulls("date")
    )


def _wyk_sells(events: pl.DataFrame) -> pl.DataFrame:
    return (
        events.filter(pl.col("kind").is_in(WYK_SELLS))
        .select("symbol", pl.col("known_date").alias("date"))
        .unique()
    )


def _bar_positions(bars: pl.DataFrame) -> pl.DataFrame:
    """``symbol, date, bar_pos`` on the symbol's stored trading bars."""
    grid = bars.select("symbol", "date").unique().sort(["symbol", "date"])
    if grid.is_empty():
        return grid.with_columns(pl.lit(None, dtype=pl.Int64).alias("bar_pos"))
    return grid.with_columns(pl.int_range(pl.len()).over("symbol").cast(pl.Int64).alias("bar_pos"))


def _rel_volume_by_symbol(bars: pl.DataFrame) -> dict[str, tuple[list, np.ndarray]]:
    result: dict[str, tuple[list, np.ndarray]] = {}
    if bars.is_empty():
        return result
    ordered = bars.select("symbol", "date", "volume").sort(["symbol", "date"])
    for keys, group in ordered.group_by(["symbol"], maintain_order=True):
        symbol = keys[0] if isinstance(keys, tuple) else keys
        result[str(symbol)] = (
            group["date"].to_list(),
            rel_volume(group["volume"].to_numpy()),
        )
    return result


# ---------------------------------------------------------------------------
# H1 and H1c: pure Chan
# ---------------------------------------------------------------------------


def _h1_variant(chan_type: str, source: str) -> Callable[[Inputs], Signals]:
    def variant(inputs: Inputs) -> Signals:
        return Signals(
            entries=_chan_entries(inputs.chan_points, (chan_type,), source),
            sells=_chan_sells(inputs.chan_points),
        )

    return variant


def _h1c_variant(chan_type: str, source: str) -> Callable[[Inputs], Signals]:
    def variant(inputs: Inputs) -> Signals:
        return Signals(
            entries=_chan_entries(
                inputs.chan_points, (chan_type,), source, date_column="confirmed_date"
            ),
            sells=_chan_sells(inputs.chan_points),
        )

    return variant


# ---------------------------------------------------------------------------
# H2: pure Wyckoff
# ---------------------------------------------------------------------------


def _h2_variant(kinds: tuple[str, ...], source: str) -> Callable[[Inputs], Signals]:
    def variant(inputs: Inputs) -> Signals:
        return Signals(
            entries=_wyk_entries(inputs.wyk_events, kinds, source),
            sells=_wyk_sells(inputs.wyk_events),
        )

    return variant


# ---------------------------------------------------------------------------
# H3: Chan buy + active accumulation range + recent Wyckoff buy
# ---------------------------------------------------------------------------


def _h3_variant(chan_type: str, source: str) -> Callable[[Inputs], Signals]:
    def variant(inputs: Inputs) -> Signals:
        entries = _chan_entries(inputs.chan_points, (chan_type,), source)
        sells = _concat_sells([_chan_sells(inputs.chan_points), _wyk_sells(inputs.wyk_events)])
        if entries.is_empty():
            return Signals(entries=entries, sells=sells)

        bar_pos = _bar_positions(inputs.bars)
        entries = entries.with_row_index("_entry_id").join(
            bar_pos, on=["symbol", "date"], how="left"
        )

        ranges = (
            inputs.wyk_ranges.filter(pl.col("range_kind").is_in(ACC_KINDS))
            .select(
                "symbol",
                pl.col("known_date").alias("_range_known"),
                "end_date",
            )
            .unique()
        )
        active = (
            entries.select("_entry_id", "symbol", "date")
            .join(ranges, on="symbol", how="inner")
            .filter(
                (pl.col("_range_known") <= pl.col("date"))
                & (pl.col("end_date").is_null() | (pl.col("date") < pl.col("end_date")))
            )
            .select("_entry_id")
            .unique()
        )
        entries = entries.join(active, on="_entry_id", how="semi")
        if entries.is_empty():
            return Signals(entries=_empty_entries(), sells=sells)

        events = (
            inputs.wyk_events.filter(pl.col("kind").is_in(WYK_BUYS))
            .select(
                "symbol",
                "kind",
                "stop_ref",
                "range_hi",
                "atr_known",
                pl.col("known_date").alias("_event_date"),
            )
            .with_columns(_wyk_invalidation_expr().alias("event_invalidation"))
            .join(
                bar_pos.rename({"date": "_event_date", "bar_pos": "event_pos"}),
                on=["symbol", "_event_date"],
                how="inner",
            )
        )
        candidates = (
            entries.select("_entry_id", "symbol", "date", "bar_pos", "invalidation")
            .join(events, on="symbol", how="inner")
            .filter(
                (pl.col("event_pos") <= pl.col("bar_pos"))
                & ((pl.col("bar_pos") - pl.col("event_pos")) <= RECENT_EVENT_BARS)
            )
        )
        recent = (
            candidates.sort(
                ["event_pos", "event_invalidation"],
                descending=[True, True],
                nulls_last=True,
            )
            .group_by("_entry_id", maintain_order=True)
            .agg(pl.col("event_invalidation").first())
        )
        entries = entries.join(recent, on="_entry_id", how="inner").with_columns(
            pl.max_horizontal("invalidation", "event_invalidation").alias("invalidation")
        )
        return Signals(entries=entries.select(list(_ENTRY_SCHEMA)), sells=sells)

    return variant


# ---------------------------------------------------------------------------
# H4: Chan 3B with a volume pattern over the legs
# ---------------------------------------------------------------------------


def _h4_variant(inputs: Inputs) -> Signals:
    sells = _chan_sells(inputs.chan_points)
    legs = ("leg_prev_start", "leg_prev_end", "leg_last_start", "leg_last_end")
    points = (
        inputs.chan_points.filter(pl.col("type") == "3B")
        .select(
            "symbol",
            pl.col("known_date").alias("date"),
            pl.col("invalidation_price").alias("invalidation"),
            *legs,
        )
        .drop_nulls(list(legs))
    )
    if points.is_empty():
        return Signals(entries=_empty_entries(), sells=sells)

    volumes = _rel_volume_by_symbol(inputs.bars)
    rows: list[dict] = []
    for row in points.iter_rows(named=True):
        series = volumes.get(row["symbol"])
        if series is None:
            continue
        dates, values = series
        position = {day: index for index, day in enumerate(dates)}
        try:
            prev_start = position[row["leg_prev_start"]]
            prev_end = position[row["leg_prev_end"]]
            last_start = position[row["leg_last_start"]]
            last_end = position[row["leg_last_end"]]
        except KeyError:
            continue

        prev_window = values[prev_start : prev_end + 1]
        last_window = values[last_start + 1 : last_end + 1]
        if not prev_window.size or not last_window.size:
            continue
        prev_mean = float(np.mean(prev_window))
        last_mean = float(np.mean(last_window))
        if np.isnan(prev_mean) or np.isnan(last_mean):
            continue
        if prev_mean >= 1.5 and last_mean <= 0.8:
            rows.append(
                {
                    "symbol": row["symbol"],
                    "date": row["date"],
                    "invalidation": row["invalidation"],
                    "source": "H4-3B",
                }
            )
    return Signals(entries=pl.DataFrame(rows, schema=_ENTRY_SCHEMA), sells=sells)


# ---------------------------------------------------------------------------
# H5: a Chan buy and a Wyckoff buy close together in bars
# ---------------------------------------------------------------------------


def _h5_variant(window: int, source: str) -> Callable[[Inputs], Signals]:
    def variant(inputs: Inputs) -> Signals:
        sells = _concat_sells([_chan_sells(inputs.chan_points), _wyk_sells(inputs.wyk_events)])
        chan = inputs.chan_points.filter(pl.col("type").is_in(CHAN_BUYS)).select(
            "symbol",
            pl.col("known_date").alias("chan_date"),
            pl.col("invalidation_price").alias("chan_invalidation"),
        )
        wyk = (
            inputs.wyk_events.filter(pl.col("kind").is_in(WYK_BUYS))
            .select(
                "symbol",
                "kind",
                "stop_ref",
                "range_hi",
                "atr_known",
                pl.col("known_date").alias("wyk_date"),
            )
            .with_columns(_wyk_invalidation_expr().alias("wyk_invalidation"))
        )
        if chan.is_empty() or wyk.is_empty():
            return Signals(entries=_empty_entries(), sells=sells)

        bar_pos = _bar_positions(inputs.bars)
        chan = chan.join(
            bar_pos.rename({"date": "chan_date", "bar_pos": "chan_pos"}),
            on=["symbol", "chan_date"],
            how="inner",
        )
        wyk = wyk.join(
            bar_pos.rename({"date": "wyk_date", "bar_pos": "wyk_pos"}),
            on=["symbol", "wyk_date"],
            how="inner",
        )
        pairs = chan.join(wyk, on="symbol", how="inner").filter(
            (pl.col("chan_pos") - pl.col("wyk_pos")).abs() <= window
        )
        entries = (
            pairs.select(
                "symbol",
                pl.when(pl.col("chan_date") >= pl.col("wyk_date"))
                .then(pl.col("chan_date"))
                .otherwise(pl.col("wyk_date"))
                .alias("date"),
                pl.max_horizontal("chan_invalidation", "wyk_invalidation").alias("invalidation"),
            )
            .group_by(["symbol", "date"])
            .agg(pl.col("invalidation").max())
            .with_columns(pl.lit(source).alias("source"))
            .select(list(_ENTRY_SCHEMA))
        )
        return Signals(entries=entries, sells=sells)

    return variant


# ---------------------------------------------------------------------------
# H6: the named variant only in a SPY uptrend
# ---------------------------------------------------------------------------


def _spy_regime(spy: pl.DataFrame) -> pl.DataFrame:
    """Dates where SPY closes above its 200-day simple moving average."""
    if spy.is_empty():
        return pl.DataFrame(schema={"date": pl.Date})
    frame = (
        spy.select("date", "close")
        .sort("date")
        .with_columns(
            pl.col("close").rolling_mean(window_size=200, min_samples=200).alias("_spy_ma")
        )
    )
    return frame.filter(
        pl.col("_spy_ma").is_not_null() & (pl.col("close") > pl.col("_spy_ma"))
    ).select("date")


def _h6_variant(base_id: str) -> Callable[[Inputs], Signals]:
    def variant(inputs: Inputs) -> Signals:
        base = VARIANTS[base_id](inputs)
        regime = _spy_regime(inputs.spy)
        return Signals(
            entries=base.entries.join(regime, on="date", how="semi"),
            sells=base.sells,
        )

    return variant


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

VARIANTS: dict[str, Callable[[Inputs], Signals]] = {
    "H1-2B": _h1_variant("2B", "H1-2B"),
    "H1-3B": _h1_variant("3B", "H1-3B"),
    "H1-1B": _h1_variant("1B", "H1-1B"),
    "H2-spring": _h2_variant(("entry_steady",), "H2-spring"),
    "H2-sos": _h2_variant(("sos",), "H2-sos"),
    "H2-lps": _h2_variant(("lps",), "H2-lps"),
    "H3-2B": _h3_variant("2B", "H3-2B"),
    "H3-3B": _h3_variant("3B", "H3-3B"),
    "H4-3B": _h4_variant,
    "H5-w5": _h5_variant(5, "H5-w5"),
    "H5-w10": _h5_variant(10, "H5-w10"),
    "H1c-2B": _h1c_variant("2B", "H1c-2B"),
    "H1c-3B": _h1c_variant("3B", "H1c-3B"),
}

# H6 wraps a fully built base variant, so it is registered after the base map.
VARIANTS.update(
    {
        "H6-H3-2B": _h6_variant("H3-2B"),
        "H6-H3-3B": _h6_variant("H3-3B"),
        "H6-H4-3B": _h6_variant("H4-3B"),
        "H6-H5-w5": _h6_variant("H5-w5"),
        "H6-H5-w10": _h6_variant("H5-w10"),
    }
)


def variant_path(data_dir: Path, variant_id: str, table: pl.DataFrame) -> Path:
    """``<data_dir>/research/variants/<id>/<content hash>.parquet``."""
    buffer = io.BytesIO()
    table.write_parquet(buffer, compression="zstd", statistics=False)
    digest = hashlib.sha256(buffer.getvalue()).hexdigest()[:16]
    return Path(data_dir) / "research" / "variants" / variant_id / f"{digest}.parquet"
