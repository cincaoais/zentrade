"""Baselines: random-entry tables and buy-and-hold curves."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.research.baselines import (
    b0_bh,
    b0_spy,
    buy_and_hold_curve,
    invalidation_k,
    random_signals,
)
from app.research.features import atr14

DATES = [date(2024, 1, 1) + timedelta(days=i) for i in range(30)]


def _bars(symbols: list[str], closes: dict[str, list[float]] | None = None) -> pl.DataFrame:
    rows: list[dict] = []
    for symbol in symbols:
        series = (
            closes[symbol]
            if closes is not None and symbol in closes
            else [100.0 + 0.5 * i for i in range(len(DATES))]
        )
        for d, close in zip(DATES, series, strict=True):
            rows.append(
                {
                    "symbol": symbol,
                    "date": d,
                    "open": close - 0.2,
                    "high": close + 0.5,
                    "low": close - 0.5,
                    "close": close,
                    "volume": 1_000_000.0,
                    "amount": 20_000_000.0,
                    "raw_close": close,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def _elig(
    symbols: list[str], dates: list[date], excluded: set[tuple[str, date]] | None = None
) -> pl.DataFrame:
    excluded = excluded or set()
    rows = [(symbol, d, (symbol, d) not in excluded) for d in dates for symbol in symbols]
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "eligible": [r[2] for r in rows],
        },
        schema={"symbol": pl.String, "date": pl.Date, "eligible": pl.Boolean},
    )


def _variant_table(rows: list[tuple[str, date, float, float, float]]) -> pl.DataFrame:
    """Entry rows as ``(symbol, date, close, atr14, invalidation)``."""
    records = [
        {
            "symbol": symbol,
            "date": d,
            "entry": True,
            "exit": False,
            "score": 0.5,
            "invalidation": invalidation,
            "atr14": atr14,
            "close": close,
        }
        for symbol, d, close, atr14, invalidation in rows
    ]
    return pl.DataFrame(
        records,
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "entry": pl.Boolean,
            "exit": pl.Boolean,
            "score": pl.Float64,
            "invalidation": pl.Float64,
            "atr14": pl.Float64,
            "close": pl.Float64,
        },
    )


def test_invalidation_k_median() -> None:
    table = _variant_table(
        [
            ("AAA", DATES[20], 100.0, 2.0, 97.0),  # ratio 1.5
            ("BBB", DATES[20], 50.0, 2.0, 45.0),  # ratio 2.5
            ("CCC", DATES[21], 80.0, 4.0, 60.0),  # ratio 5.0
        ]
    )
    exit_only = table.with_columns(pl.lit(False).alias("entry"), pl.lit(True).alias("exit"))

    assert invalidation_k(table) == pytest.approx(2.5)
    assert invalidation_k(exit_only) is None
    assert invalidation_k(table.head(0)) is None


def test_random_matches_daily_counts_and_universe() -> None:
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    bars = _bars(symbols)
    elig = _elig(symbols, DATES)
    variant = _variant_table(
        [
            ("AAA", DATES[20], 100.0, 2.0, 97.0),
            ("BBB", DATES[20], 100.0, 2.0, 97.0),
            ("CCC", DATES[21], 100.0, 2.0, 97.0),
            ("AAA", DATES[22], 100.0, 2.0, 97.0),
            ("BBB", DATES[22], 100.0, 2.0, 97.0),
            ("CCC", DATES[22], 100.0, 2.0, 97.0),
        ]
    )

    signals = random_signals(variant, elig, bars, seed=7)

    assert signals is not None
    counts = variant.filter(pl.col("entry")).group_by("date").len().sort("date")
    random_counts = signals.entries.group_by("date").len().sort("date")
    assert random_counts["date"].to_list() == counts["date"].to_list()
    assert random_counts["len"].to_list() == counts["len"].to_list()

    eligible_pairs = set(
        zip(
            elig.filter(pl.col("eligible"))["symbol"].to_list(),
            elig.filter(pl.col("eligible"))["date"].to_list(),
            strict=True,
        )
    )
    for symbol, d in zip(
        signals.entries["symbol"].to_list(),
        signals.entries["date"].to_list(),
        strict=True,
    ):
        assert (symbol, d) in eligible_pairs
    for d in signals.entries["date"].unique().to_list():
        drawn = signals.entries.filter(pl.col("date") == d)["symbol"].to_list()
        assert len(drawn) == len(set(drawn))

    k = invalidation_k(variant)
    assert k is not None
    lookup = {}
    for symbol in symbols:
        sub = _bars([symbol])
        atrs = atr14(sub["high"], sub["low"], sub["close"])
        for row, atr in zip(sub.iter_rows(named=True), atrs, strict=True):
            lookup[(row["symbol"], row["date"])] = (row["close"], atr)
    for row in signals.entries.iter_rows(named=True):
        close, atr = lookup[(row["symbol"], row["date"])]
        assert row["invalidation"] == pytest.approx(close - k * atr)

    assert signals.sells.is_empty()


def test_random_seed_reproducible() -> None:
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    bars = _bars(symbols)
    elig = _elig(symbols, DATES)
    variant = _variant_table(
        [
            ("AAA", DATES[20], 100.0, 2.0, 97.0),
            ("BBB", DATES[20], 100.0, 2.0, 97.0),
            ("CCC", DATES[20], 100.0, 2.0, 97.0),
            ("DDD", DATES[21], 100.0, 2.0, 97.0),
        ]
    )

    first = random_signals(variant, elig, bars, seed=11)
    second = random_signals(variant, elig, bars, seed=11)
    other = random_signals(variant, elig, bars, seed=12)

    assert first is not None and second is not None and other is not None
    assert first.entries.equals(second.entries)
    assert not first.entries.equals(other.entries)


def test_random_baseline_no_entries() -> None:
    symbols = ["AAA", "BBB"]
    empty = _variant_table([])
    assert random_signals(empty, _elig(symbols, DATES), _bars(symbols), seed=3) is None


def test_buy_and_hold_costs() -> None:
    days = [date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5)]
    bars = pl.DataFrame(
        {
            "symbol": ["AAA"] * 3,
            "date": days,
            "open": [10.0, 10.0, 10.0],
            "high": [11.0, 11.0, 11.0],
            "low": [10.0, 10.0, 10.0],
            "close": [11.0, 11.0, 11.0],
            "volume": [1e6] * 3,
            "amount": [2e7] * 3,
            "raw_close": [11.0] * 3,
        }
    ).with_columns(pl.col("date").cast(pl.Date))

    curve = buy_and_hold_curve(bars, ["AAA"], days[0], days[-1])

    assert curve.columns == ["date", "equity"]
    assert curve["date"].to_list() == days

    buy_price = 10.0 * (1 + 5.0 / 10_000)
    shares = int(1_000_000.0 // buy_price)  # 99950 whole shares
    buy_fee = max(shares * 0.005, 1.0)
    cash = 1_000_000.0 - shares * buy_price - buy_fee

    # Marked at the close while holding.
    assert curve["equity"][0] == pytest.approx(cash + shares * 11.0)
    assert curve["equity"][1] == pytest.approx(cash + shares * 11.0)

    sell_price = 11.0 * (1 - 5.0 / 10_000)
    sell_fee = max(shares * 0.005, 1.0)
    final = cash + shares * sell_price - sell_fee
    assert curve["equity"][2] == pytest.approx(final)
    assert final == pytest.approx(1_097_901.025)


def test_b0_bh_and_b0_spy_use_first_trading_date() -> None:
    symbols = ["AAA", "BBB"]
    bars = _bars(symbols)
    elig = _elig(symbols, DATES, excluded={("BBB", DATES[3])})

    curve = b0_bh(bars, elig, DATES[3], DATES[5])
    assert curve["date"].to_list() == DATES[3:6]
    # Only AAA is eligible on the first trading date, so the curve matches it.
    aaa = buy_and_hold_curve(bars, ["AAA"], DATES[3], DATES[5])
    assert curve["equity"].to_list() == pytest.approx(aaa["equity"].to_list())

    spy_bars = _bars(["SPY"])
    spy = b0_spy(spy_bars, DATES[3], DATES[5])
    reference = buy_and_hold_curve(spy_bars, ["SPY"], DATES[3], DATES[5])
    assert spy["equity"].to_list() == pytest.approx(reference["equity"].to_list())


def test_b0_bh_symbol_order_deterministic() -> None:
    from app.research.baselines import b0_bh

    rows = []
    for symbol in ("CCC", "AAA", "BBB"):
        for day in range(3):
            rows.append({"symbol": symbol, "date": date(2024, 1, 2 + day), "open": 10.0 + day,
                         "high": 11.0 + day, "low": 9.0 + day, "close": 10.5 + day})
    bars = pl.DataFrame(rows)
    elig = bars.select("symbol", "date").with_columns(pl.lit(True).alias("eligible"))
    first = b0_bh(bars, elig, date(2024, 1, 2), date(2024, 1, 4))
    for _ in range(5):
        assert b0_bh(bars, elig, date(2024, 1, 2), date(2024, 1, 4)).equals(first)
