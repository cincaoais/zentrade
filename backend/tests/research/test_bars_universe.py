"""Bars loader, instrument filter, causal features and point-in-time universe."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.research import RESEARCH_LAST_DATE
from app.research.bars import EXCLUDED_SYMBOLS, load_bars, tradable_symbols
from app.research.features import atr14, rel_volume
from app.research.universe import UniverseParams, eligibility, universe_path

BAR_COLUMNS = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "raw_close",
]


def _dates(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _bars(
    symbol: str,
    n: int,
    *,
    raw_close: float = 10.0,
    amount: float = 20_000_000.0,
    volume: float = 1_000_000.0,
    start: date = date(2024, 1, 1),
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "date": _dates(n, start),
            "open": [raw_close] * n,
            "high": [raw_close + 1.0] * n,
            "low": [raw_close - 1.0] * n,
            "close": [raw_close] * n,
            "volume": [volume] * n,
            "amount": [amount] * n,
            "raw_close": [raw_close] * n,
        }
    )


def _write_partition(data_dir, day: date, rows: list[dict]) -> None:
    path = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path / "part.parquet")


def _bar_row(symbol: str, day: date) -> dict:
    return {
        "symbol": symbol,
        "date": day,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 1_000_000.0,
        "amount": 10_500_000.0,
        "raw_close": 10.5,
        "raw_high": 11.0,
        "raw_low": 9.0,
        "turnover_rate": 1.0,
        "quote_ts": 0,
    }


def _write_instruments(data_dir, rows: list[dict]) -> None:
    path = data_dir / "instruments"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        rows,
        schema_overrides={
            "symbol": pl.String,
            "asset_type": pl.String,
            "instrument_type": pl.String,
            "is_etf": pl.Boolean,
            "is_preferred": pl.Boolean,
            "is_warrant": pl.Boolean,
            "is_unit": pl.Boolean,
        },
    ).write_parquet(path / "instruments.parquet")


def _instrument(symbol: str, **overrides) -> dict:
    row = {
        "symbol": symbol,
        "asset_type": "stock",
        "instrument_type": "common_stock",
        "is_etf": False,
        "is_preferred": False,
        "is_warrant": False,
        "is_unit": False,
    }
    row.update(overrides)
    return row


def test_load_bars_clips_partial_day(tmp_path) -> None:
    _write_partition(tmp_path, date(2026, 11, 12), [_bar_row("AAA", date(2026, 11, 12))])
    _write_partition(tmp_path, date(2026, 11, 13), [_bar_row("AAA", date(2026, 11, 13))])
    _write_partition(tmp_path, date(2026, 11, 16), [_bar_row("AAA", date(2026, 11, 16))])

    bars = load_bars(tmp_path, end=date(2026, 11, 20))

    assert bars["date"].max() == date(2026, 11, 13)
    assert RESEARCH_LAST_DATE == date(2026, 11, 13)
    assert bars.columns == BAR_COLUMNS
    assert bars["date"].to_list() == [date(2026, 11, 12), date(2026, 11, 13)]


def test_load_bars_filters_symbols_and_start(tmp_path) -> None:
    day = date(2026, 11, 13)
    _write_partition(tmp_path, day, [_bar_row("AAA", day), _bar_row("BBB", day)])

    bars = load_bars(tmp_path, symbols=["BBB"], start=day)

    assert bars["symbol"].to_list() == ["BBB"]
    assert bars["date"].to_list() == [day]


def test_excluded_symbols_never_tradable(tmp_path) -> None:
    _write_instruments(
        tmp_path,
        [_instrument("GOOD"), *[_instrument(sym) for sym in sorted(EXCLUDED_SYMBOLS)]],
    )

    tradable = tradable_symbols(tmp_path)

    assert "GOOD" in tradable
    assert tradable.isdisjoint(EXCLUDED_SYMBOLS)

    elig = eligibility(_bars("DPM", 250), tradable, UniverseParams())
    assert not elig["eligible"].any()


def test_tradable_symbols_filters_non_stocks(tmp_path) -> None:
    _write_instruments(
        tmp_path,
        [
            _instrument("GOOD"),
            _instrument(
                "NULLFLAGS",
                is_etf=None,
                is_preferred=None,
                is_warrant=None,
                is_unit=None,
            ),
            _instrument("ETF", is_etf=True),
            _instrument("PFD", is_preferred=True),
            _instrument("WRT", is_warrant=True),
            _instrument("UNIT", is_unit=True),
            _instrument("RIGHT", instrument_type="right"),
            _instrument("ETFASSET", asset_type="etf"),
        ],
    )

    assert tradable_symbols(tmp_path) == {"GOOD", "NULLFLAGS"}


def test_universe_uses_unadjusted_amount_not_volume() -> None:
    n = 40
    split_index = 20
    bars = _bars("X", n, raw_close=100.0, amount=20_000_000.0, volume=1_000_000.0)
    # A fake split: adjusted volume jumps 10x, unadjusted amount does not move.
    bars = bars.with_columns(
        pl.when(pl.col("date") >= _dates(n)[split_index])
        .then(pl.col("volume") * 10)
        .otherwise(pl.col("volume"))
        .alias("volume")
    )

    elig = eligibility(bars, {"X"}, UniverseParams())
    at_split = elig.filter(pl.col("date") == _dates(n)[split_index])

    assert at_split["dv_med20"].to_list() == [20_000_000.0]

    before = elig.filter(pl.col("date") < _dates(n)[split_index])
    truncated = eligibility(
        bars.filter(pl.col("date") < _dates(n)[split_index]), {"X"}, UniverseParams()
    )
    assert before["dv_med20"].to_list() == truncated["dv_med20"].to_list()
    assert before["eligible"].to_list() == truncated["eligible"].to_list()


def test_universe_rules() -> None:
    # 249 stored bars are not enough, the 250th is.
    elig = eligibility(_bars("AAA", 250), {"AAA"}, UniverseParams()).sort("date")
    assert elig["eligible"].to_list()[248] is False
    assert elig["eligible"].to_list()[249] is True
    assert elig["n_bars"].to_list()[249] == 250
    assert elig["dv_med20"].to_list()[249] == 20_000_000.0

    # Price below $5 is never eligible.
    cheap = eligibility(_bars("PENNY", 250, raw_close=4.99), {"PENNY"}, UniverseParams())
    assert not cheap["eligible"].any()

    # Dollar volume below $10M is never eligible.
    thin = eligibility(_bars("THIN", 250, amount=9_900_000.0), {"THIN"}, UniverseParams())
    assert not thin["eligible"].any()

    # top_n keeps the highest dv_med20 names.
    pooled = pl.concat(
        [
            _bars("AAA", 250, amount=30_000_000.0),
            _bars("BBB", 250, amount=20_000_000.0),
            _bars("CCC", 250, amount=15_000_000.0),
        ]
    )
    top2 = eligibility(pooled, {"AAA", "BBB", "CCC"}, UniverseParams(top_n=2))
    last_day = top2.filter(pl.col("n_bars") == 250)
    assert set(last_day.filter(pl.col("eligible"))["symbol"].to_list()) == {"AAA", "BBB"}
    assert last_day.filter(pl.col("symbol") == "CCC")["rank"].to_list() == [3]

    # Ties are broken by symbol ascending.
    tied = pl.concat([_bars("AAA", 250), _bars("BBB", 250)])
    one = eligibility(tied, {"AAA", "BBB"}, UniverseParams(top_n=1))
    last = one.filter(pl.col("n_bars") == 250)
    assert set(last.filter(pl.col("eligible"))["symbol"].to_list()) == {"AAA"}
    assert last.filter(pl.col("symbol") == "BBB")["rank"].to_list() == [2]

    # A symbol outside the tradable set is never eligible.
    untradable = eligibility(_bars("NOPE", 250), {"AAA"}, UniverseParams())
    assert not untradable["eligible"].any()


def test_non_tradable_does_not_take_top_n_slot() -> None:
    """A huge-volume ETF (SPY) must not push a tradable stock out of top_n."""
    bars = pl.concat(
        [
            _bars("SPY", 260, amount=1e11),
            _bars("AAA", 260, amount=3e7),
            _bars("BBB", 260, amount=2e7),
        ]
    )
    params = UniverseParams(min_bars=250, top_n=2)
    out = eligibility(bars, {"AAA", "BBB"}, params)
    last = out.filter(pl.col("date") == out["date"].max())
    eligible = set(last.filter(pl.col("eligible"))["symbol"].to_list())
    assert eligible == {"AAA", "BBB"}
    assert last.filter(pl.col("symbol") == "SPY")["rank"].to_list() == [None]


def test_universe_params_hash() -> None:
    first = UniverseParams()
    assert first.hash() == UniverseParams().hash()
    assert re.match(r"^[0-9a-f]{16}$", first.hash())
    assert first.hash() != UniverseParams(top_n=100).hash()
    assert (
        universe_path("data", first)
        == Path("data/research/universe") / f"{first.hash()}.parquet"
    )


def test_atr14_and_rel_volume_causal() -> None:
    n = 60
    rng = np.random.default_rng(7)
    close = 100.0 + np.cumsum(rng.normal(size=n))
    high = close + 1.0
    low = close - 1.0
    volume = rng.integers(100, 1_000, size=n).astype(float)

    base_atr = atr14(high, low, close)
    base_rel = rel_volume(volume)

    assert np.isnan(base_atr[:13]).all()
    assert np.isfinite(base_atr[13:]).all()
    assert np.isnan(base_rel[:20]).all()
    assert np.isfinite(base_rel[20:]).all()

    # A later bar cannot change earlier values.
    t = 30
    close2 = close.copy()
    close2[t + 1] = close2[t + 1] + 50.0
    high2 = high.copy()
    high2[t + 1] = high2[t + 1] + 50.0
    volume2 = volume.copy()
    volume2[t + 1] = volume2[t + 1] * 100.0

    atr2 = atr14(high2, low, close2)
    rel2 = rel_volume(volume2)
    np.testing.assert_allclose(atr2[: t + 1], base_atr[: t + 1], equal_nan=True)
    np.testing.assert_allclose(rel2[: t + 1], base_rel[: t + 1], equal_nan=True)

    # rel_volume at t uses the previous 20 bars only, not volume[t] itself.
    flat = np.full(21, 100.0)
    flat[20] = 1_000.0
    rel = rel_volume(flat)
    assert rel[20] == 10.0
    assert np.isnan(rel[19])

    # The first ATR value is the mean of the first 14 true ranges.
    constant = np.full(30, 100.0)
    atr = atr14(constant + 1.0, constant - 1.0, constant)
    assert atr[13] == 2.0
    assert atr[14] == 2.0