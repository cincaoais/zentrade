"""Ranking features: causal RS, retracement and the raw-score directions."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.research.features import rel_volume
from app.research.ranking import (
    RANKS,
    RS_LOOKBACK,
    chan_retracement,
    rank_features,
    raw_scores,
)

DATES = [date(2024, 1, 1) + timedelta(days=i) for i in range(120)]


def _bars(
    symbol: str,
    closes: list[float],
    dates: list[date] | None = None,
) -> pl.DataFrame:
    dates = dates if dates is not None else DATES[: len(closes)]
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "date": dates,
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000.0 + i for i in range(len(closes))],
            "amount": [20_000_000.0] * len(closes),
        }
    )


def _row(frame: pl.DataFrame, symbol: str, day: date) -> dict:
    return frame.filter(
        (pl.col("symbol") == symbol) & (pl.col("date") == day)
    ).row(0, named=True)


def test_rs63_matches_definition() -> None:
    assert RS_LOOKBACK == 63
    aaa = [100.0 + i for i in range(100)]
    bbb = [200.0 + 0.5 * i for i in range(100)]
    spy = [400.0 + 0.25 * i for i in range(100)]
    bars = pl.concat([_bars("AAA", aaa), _bars("BBB", bbb)])
    features = rank_features(bars, _bars("SPY", spy))

    row = _row(features, "AAA", DATES[70])
    assert row["rs63"] == pytest.approx(aaa[70] / aaa[7] - spy[70] / spy[7])

    row = _row(features, "BBB", DATES[80])
    assert row["rs63"] == pytest.approx(bbb[80] / bbb[17] - spy[80] / spy[17])

    # Only 62 prior stored bars: the 63-bar lookback is unavailable.
    row = _row(features, "AAA", DATES[62])
    assert row["rs63"] is None


def test_features_are_causal() -> None:
    bars = pl.concat(
        [
            _bars("AAA", [100.0 + i for i in range(120)]),
            _bars("BBB", [50.0 + 0.3 * i for i in range(120)]),
        ]
    )
    spy = _bars("SPY", [400.0 + 0.2 * i for i in range(120)])
    full = rank_features(bars, spy)
    columns = ("close", "atr14", "rs63", "relvol", "atrp")

    for index in (40, 63, 80, 100, 119):
        day = DATES[index]
        truncated = rank_features(
            bars.filter(pl.col("date") <= day),
            spy.filter(pl.col("date") <= day),
        )
        for symbol in ("AAA", "BBB"):
            full_row = _row(full, symbol, day)
            truncated_row = _row(truncated, symbol, day)
            for column in columns:
                expected = full_row[column]
                actual = truncated_row[column]
                if expected is None or (
                    isinstance(expected, float) and np.isnan(expected)
                ):
                    assert actual is None or (
                        isinstance(actual, float) and np.isnan(actual)
                    )
                else:
                    assert actual == pytest.approx(expected)


def test_relvol_and_lowvol() -> None:
    closes = [100.0 + i for i in range(50)]
    bars = _bars("AAA", closes)
    features = rank_features(bars, _bars("SPY", closes))

    expected = rel_volume(bars["volume"].to_numpy())
    got = features["relvol"].to_numpy()
    np.testing.assert_allclose(got, expected, equal_nan=True)
    assert np.isnan(got[:20]).all()

    for index in (25, 40, 49):
        row = _row(features, "AAA", DATES[index])
        assert row["atrp"] == pytest.approx(row["atr14"] / row["close"])

    # A precomputed atr14 column is reused instead of recomputed.
    precomputed = bars.with_columns(pl.lit(2.0).alias("atr14"))
    reused = rank_features(precomputed, _bars("SPY", closes))
    assert reused["atr14"].to_list() == [2.0] * 50
    row = _row(reused, "AAA", DATES[25])
    assert row["atrp"] == pytest.approx(2.0 / closes[25])


def _stroke_bars() -> pl.DataFrame:
    dates = DATES[:10]
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * 10,
            "date": dates,
            "high": [100.0, 110.0, 109.0, 110.0, 150.0, 204.0, 203.0, 204.0, 300.0, 301.0],
            "low": [99.0, 100.0, 100.0, 100.0, 140.0, 200.0, 200.0, 200.0, 299.0, 291.0],
            "close": [100.0] * 10,
            "volume": [1000.0] * 10,
        }
    )


def _points(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def _point(
    symbol: str,
    known: date,
    confirmed: date,
    prev_start: date | None,
    prev_end: date | None,
    last_start: date | None,
    last_end: date | None,
    point_type: str = "2B",
) -> dict:
    return {
        "symbol": symbol,
        "type": point_type,
        "known_date": known,
        "confirmed_date": confirmed,
        "leg_prev_start": prev_start,
        "leg_prev_end": prev_end,
        "leg_last_start": last_start,
        "leg_last_end": last_end,
    }


def test_retracement_ratio() -> None:
    bars = _stroke_bars()
    dates = DATES[:10]
    # prev stroke [d1, d3]: 110 - 100 = 10; last stroke [d5, d7]: 204 - 200 = 4.
    points = _points(
        [
            _point(
                "AAA",
                dates[8],
                dates[9],
                dates[1],
                dates[3],
                dates[5],
                dates[7],
            )
        ]
    )

    by_known = chan_retracement(points, bars, ("2B",), "known_date")
    assert by_known.height == 1
    assert by_known["date"][0] == dates[8]
    assert by_known["retr_score"][0] == pytest.approx(-0.4)

    by_confirmed = chan_retracement(points, bars, ("2B",), "confirmed_date")
    assert by_confirmed["date"][0] == dates[9]
    assert by_confirmed["retr_score"][0] == pytest.approx(-0.4)

    # Two points on the same (symbol, date) keep the larger score.
    # Second stroke [d8, d8]: 300 - 299 = 1 -> -0.1 beats -0.4.
    both = _points(
        [
            _point(
                "AAA",
                dates[8],
                dates[8],
                dates[1],
                dates[3],
                dates[5],
                dates[7],
            ),
            _point(
                "AAA",
                dates[8],
                dates[8],
                dates[1],
                dates[3],
                dates[8],
                dates[8],
            ),
        ]
    )
    merged = chan_retracement(both, bars, ("2B",), "known_date")
    assert merged.height == 1
    assert merged["retr_score"][0] == pytest.approx(-0.1)


def test_retracement_rejects_future_leg() -> None:
    bars = _stroke_bars()
    dates = DATES[:10]
    points = _points(
        [
            _point(
                "BAD",
                dates[8],
                dates[8],
                dates[1],
                dates[3],
                dates[5],
                dates[9],
            )
        ]
    )
    with pytest.raises(ValueError, match="BAD"):
        chan_retracement(points, bars, ("2B",), "known_date")


def test_retracement_nulls() -> None:
    dates = DATES[:5]
    bars = pl.DataFrame(
        {
            "symbol": ["N1", "N1", "N2", "N2", "N2"],
            "date": [dates[0], dates[1], dates[0], dates[1], dates[2]],
            "high": [100.0, 101.0, 100.0, 100.0, 102.0],
            "low": [99.0, 99.0, 100.0, 100.0, 98.0],
            "close": [100.0] * 5,
            "volume": [1000.0] * 5,
        }
    )
    points = _points(
        [
            # Missing leg date: row kept, score null.
            _point(
                "N1",
                dates[2],
                dates[2],
                None,
                dates[1],
                dates[0],
                dates[1],
            ),
            # Previous stroke has zero range: row kept, score null.
            _point(
                "N2",
                dates[2],
                dates[2],
                dates[0],
                dates[1],
                dates[1],
                dates[2],
            ),
        ]
    )
    result = chan_retracement(points, bars, ("2B",), "known_date")
    assert result.height == 2
    assert result["retr_score"].to_list() == [None, None]


def test_raw_scores_directions() -> None:
    assert RANKS == ("rs", "retr", "risk", "relvol", "lowvol")
    day = date(2024, 1, 1)
    entries = pl.DataFrame(
        {
            "symbol": ["X", "Y", "Z"],
            "date": [day, day, day],
            "invalidation": [98.0, 95.0, 90.0],
            "source": ["s", "s", "s"],
        }
    )
    features = pl.DataFrame(
        {
            "symbol": ["X", "Y"],
            "date": [day, day],
            "close": [100.0, 100.0],
            "atr14": [2.0, 4.0],
            "rs63": [0.1, 0.2],
            "relvol": [1.5, 2.5],
            "atrp": [0.02, 0.04],
        }
    )

    def scores(rank: str, retr: pl.DataFrame | None = None) -> dict[str, float | None]:
        out = raw_scores(entries, rank, features, retr)
        assert out.height == 3
        assert out.columns[:4] == entries.columns
        assert out.schema["raw_score"] == pl.Float64
        return {row["symbol"]: row["raw_score"] for row in out.iter_rows(named=True)}

    rs = scores("rs")
    assert rs["Y"] > rs["X"]
    assert rs["Z"] is None

    risk = scores("risk")
    assert risk["X"] == pytest.approx(-1.0)
    assert risk["Y"] == pytest.approx(-1.25)
    assert risk["X"] > risk["Y"]

    lowvol = scores("lowvol")
    assert lowvol["X"] == pytest.approx(-0.02)
    assert lowvol["Y"] == pytest.approx(-0.04)
    assert lowvol["X"] > lowvol["Y"]

    relvol = scores("relvol")
    assert relvol["Y"] > relvol["X"]

    retr = pl.DataFrame(
        {
            "symbol": ["X", "Y"],
            "date": [day, day],
            "retr_score": [-0.4, -0.1],
        }
    )
    retr_scores = scores("retr", retr)
    assert retr_scores["X"] == pytest.approx(-0.4)
    assert retr_scores["Y"] == pytest.approx(-0.1)
    assert retr_scores["Z"] is None

    with pytest.raises(ValueError):
        raw_scores(entries, "nope", features)
    with pytest.raises(ValueError):
        raw_scores(entries, "retr", features, None)

    out = raw_scores(entries, "rs", features)
    assert out.select(entries.columns).equals(entries)
