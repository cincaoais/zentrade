"""Forward tables compose with the frozen research variant code."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.forward.tables import (
    build_inputs,
    candidates_on,
    forward_eligibility,
    forward_table,
    kept_entry_symbols,
)
from app.research.compose import compose_table, with_atr14
from app.research.hypotheses import VARIANTS, Inputs
from app.research.ranking import rank_features

DAYS = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]


def _bars(
    symbol: str,
    days: list[date] | None = None,
    closes: list[float] | None = None,
    opens: list[float] | None = None,
    volume: float = 1_000_000.0,
) -> pl.DataFrame:
    days = days if days is not None else DAYS
    closes = closes if closes is not None else [100.0] * len(days)
    opens = opens if opens is not None else closes
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(days),
            "date": days,
            "open": opens,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [volume] * len(days),
            "amount": [c * volume for c in closes],
            "raw_close": closes,
        }
    )


def _points(rows: list[tuple[str, str, date, float]]) -> pl.DataFrame:
    """Fake Chan points: ``(symbol, type, known_date, invalidation_price)``."""
    return pl.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "type": [row[1] for row in rows],
            "known_date": [row[2] for row in rows],
            "invalidation_price": [row[3] for row in rows],
        },
        schema={
            "symbol": pl.String,
            "type": pl.String,
            "known_date": pl.Date,
            "invalidation_price": pl.Float64,
        },
    )


def _elig(symbols: list[str], days: list[date] | None = None) -> pl.DataFrame:
    days = days if days is not None else DAYS
    return pl.DataFrame(
        {
            "symbol": [symbol for symbol in symbols for _ in days],
            "date": [day for _ in symbols for day in days],
            "eligible": [True] * (len(symbols) * len(days)),
        }
    )


def _reference_inputs(
    bars: pl.DataFrame, points: pl.DataFrame, spy: pl.DataFrame
) -> Inputs:
    empty = pl.DataFrame()
    atr_bars = with_atr14(bars)
    return Inputs(
        chan_points=points,
        wyk_events=empty,
        wyk_ranges=empty,
        bars=atr_bars,
        spy=spy,
        features=rank_features(atr_bars, spy),
    )


def test_forward_table_equals_research_table_on_past_data() -> None:
    raw = pl.concat([_bars("AAA"), _bars("BBB", closes=[50.0] * len(DAYS))])
    spy = _bars("SPY")
    points = _points(
        [
            ("AAA", "2B", DAYS[2], 90.0),
            ("AAA", "1S", DAYS[6], 0.0),
            ("BBB", "2B", DAYS[3], 40.0),
        ]
    )
    elig = _elig(["AAA", "BBB"])

    reference_inputs = _reference_inputs(raw, points, spy)
    expected, expected_log = compose_table(
        VARIANTS["H1-2B"](reference_inputs), reference_inputs.bars, elig
    )

    empty = pl.DataFrame()
    inputs = build_inputs(raw, spy, points, empty, empty)
    assert inputs.bars.equals(reference_inputs.bars)
    assert inputs.features is not None
    assert inputs.features.equals(reference_inputs.features)

    table, log = forward_table("H1-2B", inputs, elig, inputs.bars)

    assert table.equals(expected)
    assert log == expected_log
    assert expected.filter(pl.col("entry")).height == 2


def test_last_day_entry_dropped_without_next_bar() -> None:
    raw = _bars("AAA")
    spy = _bars("SPY")
    points = _points([("AAA", "2B", DAYS[-1], 90.0)])

    inputs = build_inputs(raw, spy, points, pl.DataFrame(), pl.DataFrame())
    table, log = forward_table("H1-2B", inputs, _elig(["AAA"]), inputs.bars)

    # ``compose_table`` needs the next stored bar to check the gap, so a
    # signal on the final day cannot become an entry yet.
    assert log["gapped_through"] == 1
    assert log["kept"] == 0
    assert table.filter(pl.col("entry")).is_empty()


def test_last_day_entry_kept_with_open_only_bar() -> None:
    raw = _bars("AAA")
    spy = _bars("SPY")
    points = _points([("AAA", "2B", DAYS[-1], 90.0)])
    t1 = DAYS[-1] + timedelta(days=1)

    def _with_open(open_price: float) -> pl.DataFrame:
        row = _bars(
            "AAA",
            days=[t1],
            closes=[open_price],
            opens=[open_price],
        )
        return pl.concat([raw, row])

    open_above = build_inputs(
        _with_open(100.0), spy, points, pl.DataFrame(), pl.DataFrame()
    )
    kept, kept_log = forward_table(
        "H1-2B", open_above, _elig(["AAA"]), open_above.bars
    )
    assert kept_log["kept"] == 1
    entry = kept.filter(pl.col("entry")).row(0, named=True)
    assert entry["symbol"] == "AAA"
    assert entry["date"] == DAYS[-1]

    open_at_invalidation = build_inputs(
        _with_open(90.0), spy, points, pl.DataFrame(), pl.DataFrame()
    )
    gapped, gapped_log = forward_table(
        "H1-2B", open_at_invalidation, _elig(["AAA"]), open_at_invalidation.bars
    )
    assert gapped_log["kept"] == 0
    assert gapped.filter(pl.col("entry")).is_empty()


def test_candidates_on_before_gap_filter() -> None:
    t = DAYS[4]
    other = DAYS[3]
    signals_entries = pl.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC", "AAA", "DDD"],
            "date": [t, t, t, t, other],
            "invalidation": [90.0, 40.0, 70.0, 91.0, 80.0],
            "source": ["H1-2B"] * 5,
        },
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "invalidation": pl.Float64,
            "source": pl.String,
        },
    )
    elig = pl.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC", "DDD"],
            "date": [t] * 4,
            "eligible": [True, False, True, True],
        },
        schema={"symbol": pl.String, "date": pl.Date, "eligible": pl.Boolean},
    )

    # CCC may be gapped through on the next open; the gap filter lives in the
    # composer, so the pre-filter candidate list still names it once.
    assert candidates_on(signals_entries, elig, t) == ["AAA", "CCC"]


def test_kept_entry_symbols_from_start() -> None:
    table = pl.DataFrame(
        {
            "symbol": ["AAA", "BBB", "AAA", "CCC"],
            "date": [DAYS[1], DAYS[1], DAYS[5], DAYS[6]],
            "entry": [True, True, True, False],
        }
    )

    assert kept_entry_symbols(table, DAYS[5]) == {"AAA"}
    assert kept_entry_symbols(table, DAYS[0]) == {"AAA", "BBB"}


def test_forward_eligibility_uses_forward_snapshot(tmp_path) -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(260)]
    raw = pl.concat([_bars("AAA", days=days), _bars("BBB", days=days)])
    snapshot_dir = tmp_path / "forward" / "universe"
    snapshot_dir.mkdir(parents=True)
    pl.DataFrame({"symbol": ["AAA"]}, schema={"symbol": pl.String}).write_parquet(
        snapshot_dir / f"{days[-1].isoformat()}.parquet"
    )

    frame = forward_eligibility(tmp_path, raw, days[-1])
    last = frame.filter(pl.col("date") == days[-1])

    assert last.filter(pl.col("symbol") == "AAA")["eligible"].to_list() == [True]
    assert last.filter(pl.col("symbol") == "BBB")["eligible"].to_list() == [False]
