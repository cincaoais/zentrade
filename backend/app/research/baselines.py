"""Baselines: random-entry tables and buy-and-hold curves.

The random-entry baseline keeps the variant's daily entry count and invalidation
distance but replaces the symbols with uniform draws from that day's eligible
universe. The buy-and-hold baselines invest equal capital per symbol and pay the
same commissions and slippage as the strategy runs.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from app.research.compose import Signals
from app.research.features import atr14

_CURVE_SCHEMA = {"date": pl.Date, "equity": pl.Float64}

_EMPTY_SELLS: dict[str, pl.DataType] = {"symbol": pl.String, "date": pl.Date}


def invalidation_k(table: pl.DataFrame) -> float | None:
    """Median ``(close - invalidation) / atr14`` over entry rows, or ``None``."""
    rows = table.filter(
        pl.col("entry")
        & pl.col("close").is_not_null()
        & pl.col("invalidation").is_not_null()
        & pl.col("atr14").is_not_null()
    )
    if rows.is_empty():
        return None
    ratio = (rows["close"] - rows["invalidation"]) / rows["atr14"]
    return float(ratio.median())


def _atr_frame(bars: pl.DataFrame) -> pl.DataFrame:
    """Bars with the causal ``atr14`` column attached per symbol.

    A precomputed ``atr14`` column is reused: the random baseline runs hundreds
    of seeds over the full store, and recomputing ATR per seed costs hours.
    """
    if "atr14" in bars.columns:
        return bars
    parts = [
        group.with_columns(pl.Series("atr14", atr14(group["high"], group["low"], group["close"])))
        for _, group in bars.sort(["symbol", "date"]).group_by(["symbol"], maintain_order=True)
    ]
    if not parts:
        return bars.with_columns(pl.lit(None, dtype=pl.Float64).alias("atr14"))
    return pl.concat(parts)


def random_signals(
    variant_table: pl.DataFrame,
    elig: pl.DataFrame,
    bars: pl.DataFrame,
    seed: int,
) -> Signals | None:
    """Random-entry stand-in for a variant, or ``None`` when it has no entries.

    Each decision date draws as many distinct eligible symbols as the variant
    has entry rows that day; the invalidation is ``close - k * atr14`` with the
    variant's median ``k``. The baseline never sells.
    """
    k = invalidation_k(variant_table)
    if k is None:
        return None

    counts = variant_table.filter(pl.col("entry")).group_by("date").len().sort("date")
    if counts.is_empty():
        return None

    eligible_by_date: dict[date, list[str]] = {}
    eligible = elig.filter(pl.col("eligible")).select("symbol", "date")
    for d, group in eligible.group_by("date", maintain_order=True):
        eligible_by_date[d[0]] = group["symbol"].unique().sort().to_list()

    rng = np.random.default_rng(seed)
    drawn_dates: list[date] = []
    drawn_symbols: list[str] = []
    for d, n in counts.iter_rows():
        candidates = eligible_by_date.get(d, [])
        if not candidates:
            continue
        size = min(n, len(candidates))
        picks = rng.choice(len(candidates), size=size, replace=False)
        for index in picks:
            drawn_symbols.append(candidates[int(index)])
            drawn_dates.append(d)

    drawn = pl.DataFrame(
        {
            "symbol": pl.Series("symbol", drawn_symbols, dtype=pl.String),
            "date": pl.Series("date", drawn_dates, dtype=pl.Date),
        },
        schema={"symbol": pl.String, "date": pl.Date},
    )
    lookup = _atr_frame(bars).select("symbol", "date", "close", "atr14")
    entries = (
        drawn.join(lookup, on=["symbol", "date"], how="left")
        .with_columns(
            (pl.col("close") - k * pl.col("atr14")).alias("invalidation"),
            pl.lit("random").alias("source"),
        )
        .select("symbol", "date", "invalidation", "source")
        .sort(["date", "symbol"])
    )
    return Signals(
        entries=entries,
        sells=pl.DataFrame(schema=_EMPTY_SELLS),
    )


def _empty_curve() -> pl.DataFrame:
    return pl.DataFrame(schema=_CURVE_SCHEMA)


def buy_and_hold_curve(
    bars: pl.DataFrame,
    symbols: list[str],
    start: date,
    end: date,
    capital: float = 1_000_000.0,
    commission_per_share: float = 0.005,
    commission_min: float = 1.0,
    slippage_bps: float = 5.0,
) -> pl.DataFrame:
    """Equal-weight, whole-share buy-and-hold equity curve.

    Each symbol buys at its first open plus slippage, is marked at the close
    (forward-filled) and is liquidated at the last close minus slippage. Cash
    left over from whole-share rounding is kept, and both legs pay commission.
    """
    if not symbols:
        return _empty_curve()

    frame = (
        bars.filter(
            pl.col("symbol").is_in(list(symbols))
            & (pl.col("date") >= pl.lit(start))
            & (pl.col("date") <= pl.lit(end))
        )
        .select("symbol", "date", "open", "close")
        .sort(["symbol", "date"])
    )
    if frame.is_empty():
        return _empty_curve()

    grid = frame["date"].unique().sort().to_list()
    per_symbol = capital / len(symbols)
    slip = slippage_bps / 10_000.0

    contributions: list[list[float]] = []
    for symbol in symbols:
        sub = frame.filter(pl.col("symbol") == symbol)
        by_date = {row["date"]: row for row in sub.iter_rows(named=True)}
        first_date = sub["date"][0] if not sub.is_empty() else None

        cash = per_symbol
        shares = 0
        last_close: float | None = None
        values: list[float] = []

        for d in grid:
            bar = by_date.get(d)
            if bar is not None:
                last_close = bar["close"]

            if shares == 0 and d == first_date and bar is not None:
                buy_price = bar["open"] * (1 + slip)
                shares = int(per_symbol // buy_price)
                if shares > 0:
                    fee = max(shares * commission_per_share, commission_min)
                    cash -= shares * buy_price + fee

            if shares > 0 and last_close is not None:
                values.append(cash + shares * last_close)
            else:
                values.append(cash)

        if shares > 0 and last_close is not None:
            sell_price = last_close * (1 - slip)
            fee = max(shares * commission_per_share, commission_min)
            values[-1] = cash + shares * sell_price - fee

        contributions.append(values)

    equity = [float(sum(column)) for column in zip(*contributions, strict=True)]
    return pl.DataFrame(
        {"date": grid, "equity": equity},
        schema=_CURVE_SCHEMA,
    )


def b0_bh(
    bars: pl.DataFrame,
    elig: pl.DataFrame,
    start: date,
    end: date,
    capital: float = 1_000_000.0,
) -> pl.DataFrame:
    """Buy and hold the symbols eligible on the first trading date >= ``start``."""
    window = bars.filter((pl.col("date") >= pl.lit(start)) & (pl.col("date") <= pl.lit(end)))
    dates = window["date"].unique().sort()
    if dates.is_empty():
        return _empty_curve()
    first = dates[0]
    symbols = (
        elig.filter((pl.col("date") == first) & pl.col("eligible"))["symbol"]
        .unique()
        .sort()
        .to_list()
    )
    return buy_and_hold_curve(bars, symbols, start, end, capital=capital)


def b0_spy(
    bars_spy: pl.DataFrame,
    start: date,
    end: date,
    capital: float = 1_000_000.0,
) -> pl.DataFrame:
    """Buy and hold ``SPY`` (reference only, never a traded variant)."""
    return buy_and_hold_curve(bars_spy, ["SPY"], start, end, capital=capital)


__all__ = [
    "b0_bh",
    "b0_spy",
    "buy_and_hold_curve",
    "invalidation_k",
    "random_signals",
]
