"""Composer core: eligibility, gap skip, active invalidation and neutral score."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.research.compose import (
    FORBIDDEN_INPUT_COLUMNS,
    TABLE_COLUMNS,
    Signals,
    assert_no_future_columns,
    compose_table,
    entry_score,
)

DATES = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]


def _bars(
    symbol: str,
    closes: list[float] | None = None,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pl.DataFrame:
    closes = closes if closes is not None else [100.0] * len(DATES)
    opens = opens if opens is not None else closes
    highs = highs if highs is not None else [c + 1.0 for c in closes]
    lows = lows if lows is not None else [c - 1.0 for c in closes]
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(DATES),
            "date": DATES,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1_000_000.0] * len(DATES),
            "amount": [20_000_000.0] * len(DATES),
            "raw_close": closes,
        }
    )


def _entries(rows: list[tuple[str, int, float, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [DATES[r[1]] for r in rows],
            "invalidation": [r[2] for r in rows],
            "source": [r[3] for r in rows],
        },
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "invalidation": pl.Float64,
            "source": pl.String,
        },
    )


def _scored_entries(
    rows: list[tuple[str, int, float, float | None]],
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [DATES[r[1]] for r in rows],
            "invalidation": [r[2] for r in rows],
            "source": ["ranked"] * len(rows),
            "raw_score": [r[3] for r in rows],
        },
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "invalidation": pl.Float64,
            "source": pl.String,
            "raw_score": pl.Float64,
        },
    )


def _sells(rows: list[tuple[str, int]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": [r[0] for r in rows], "date": [DATES[r[1]] for r in rows]},
        schema={"symbol": pl.String, "date": pl.Date},
    )


def _elig(
    symbols: list[str], ineligible: set[tuple[str, int]] | None = None
) -> pl.DataFrame:
    ineligible = ineligible or set()
    rows = [
        (symbol, DATES[i], (symbol, i) not in ineligible)
        for symbol in symbols
        for i in range(len(DATES))
    ]
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "eligible": [r[2] for r in rows],
        }
    )


def test_forbidden_column_rejected() -> None:
    bars = _bars("AAA").with_columns(pl.lit(False).alias("still_valid"))
    with pytest.raises(ValueError, match="still_valid"):
        assert_no_future_columns(bars)
    assert "still_valid" in FORBIDDEN_INPUT_COLUMNS


def test_ineligible_entry_dropped_exit_kept() -> None:
    signals = Signals(
        entries=_entries([("AAA", 2, 90.0, "H1-2B")]),
        sells=_sells([("AAA", 5)]),
    )
    table, log = compose_table(
        signals, _bars("AAA"), _elig(["AAA"], {("AAA", 2)})
    )

    assert log["emitted"] == 1
    assert log["ineligible"] == 1
    assert log["gapped_through"] == 0
    assert log["kept"] == 0
    assert log["exits"] == 1

    assert table.height == 1
    row = table.row(0, named=True)
    assert (row["symbol"], row["date"]) == ("AAA", DATES[5])
    assert row["entry"] is False
    assert row["exit"] is True
    assert list(table.columns) == list(TABLE_COLUMNS)


def test_gapped_through_skipped() -> None:
    closes = [100.0] * len(DATES)
    opens = [100.0] * len(DATES)
    opens[3] = 89.99  # bar t+1 opens at invalidation - 0.01
    signals = Signals(
        entries=_entries([("AAA", 2, 90.0, "H1-2B")]), sells=_sells([])
    )
    table, log = compose_table(
        signals, _bars("AAA", closes=closes, opens=opens), _elig(["AAA"])
    )

    assert log["gapped_through"] == 1
    assert log["kept"] == 0
    assert table.is_empty()


def test_same_day_merge_uses_higher_invalidation() -> None:
    signals = Signals(
        entries=_entries(
            [("AAA", 2, 90.0, "H1-2B"), ("AAA", 2, 95.0, "H2-spring")]
        ),
        sells=_sells([]),
    )
    table, log = compose_table(signals, _bars("AAA"), _elig(["AAA"]))

    assert log["emitted"] == 1
    assert log["kept"] == 1
    rows = table.filter(pl.col("entry")).to_dicts()
    assert len(rows) == 1
    assert rows[0]["invalidation"] == 95.0


def test_newest_signal_invalidation_governs() -> None:
    # Close of 93 on day 6 breaches entry B (95) but not entry A (90).
    closes = [100.0] * len(DATES)
    closes[6] = 93.0
    signals = Signals(
        entries=_entries([("AAA", 2, 90.0, "H1-2B"), ("AAA", 5, 95.0, "H1-3B")]),
        sells=_sells([]),
    )
    table, _ = compose_table(
        signals,
        _bars("AAA", closes=closes, opens=[100.0] * len(DATES)),
        _elig(["AAA"]),
    )
    day6 = table.filter(pl.col("date") == DATES[6]).row(0, named=True)
    assert day6["exit"] is True
    assert day6["invalidation"] == 95.0

    # The same close on day 4 is above the active invalidation of 90: no exit.
    closes4 = [100.0] * len(DATES)
    closes4[4] = 93.0
    table4, _ = compose_table(
        Signals(entries=signals.entries, sells=_sells([])),
        _bars("AAA", closes=closes4, opens=[100.0] * len(DATES)),
        _elig(["AAA"]),
    )
    assert table4.filter(pl.col("exit")).is_empty()


def test_sell_row_sets_exit() -> None:
    signals = Signals(
        entries=_entries([("AAA", 2, 50.0, "H1-2B")]),
        sells=_sells([("AAA", 6)]),
    )
    table, log = compose_table(signals, _bars("AAA"), _elig(["AAA"]))

    assert log["exits"] == 1
    rows = table.filter(pl.col("exit")).to_dicts()
    assert len(rows) == 1
    assert rows[0]["date"] == DATES[6]
    assert rows[0]["entry"] is False


def test_score_deterministic_and_neutral() -> None:
    fixed = date(2024, 1, 2)
    scores = {s: entry_score(s, fixed) for s in ("AAA", "BBB", "CCC")}
    assert all(0.0 < v < 1.0 for v in scores.values())

    order = sorted(scores, key=scores.__getitem__)
    assert order == ["CCC", "AAA", "BBB"]
    assert order != ["AAA", "BBB", "CCC"]

    signals = Signals(
        entries=_entries(
            [("AAA", 1, 50.0, "x"), ("BBB", 1, 50.0, "x"), ("CCC", 1, 50.0, "x")]
        ),
        sells=_sells([]),
    )
    bars = pl.concat([_bars("AAA"), _bars("BBB"), _bars("CCC")])
    elig = _elig(["AAA", "BBB", "CCC"])
    table, _ = compose_table(signals, bars, elig)
    got = {
        row["symbol"]: row["score"] for row in table.filter(pl.col("entry")).to_dicts()
    }
    assert got == scores
    assert [row["symbol"] for row in table.to_dicts()] == ["AAA", "BBB", "CCC"]

def test_precomputed_atr_gives_identical_table() -> None:
    from app.research.compose import with_atr14

    closes = [100.0 + i for i in range(len(DATES))]
    bars = _bars("AAA", closes=closes, opens=closes)
    signals = Signals(entries=_entries([("AAA", 2, 90.0, "H1-2B")]), sells=_sells([]))
    plain, plain_log = compose_table(signals, bars, _elig(["AAA"]))
    fast, fast_log = compose_table(signals, with_atr14(bars), _elig(["AAA"]))
    assert plain.equals(fast)
    assert plain_log == fast_log


def test_raw_score_orders_entries_per_date() -> None:
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    signals = Signals(
        entries=_scored_entries(
            [
                ("AAA", 2, 50.0, 0.3),
                ("BBB", 2, 50.0, None),
                ("CCC", 2, 50.0, 0.9),
                ("DDD", 2, 50.0, 0.1),
            ]
        ),
        sells=_sells([]),
    )
    bars = pl.concat([_bars(symbol) for symbol in symbols])
    table, _ = compose_table(signals, bars, _elig(symbols))
    scores = {
        row["symbol"]: row["score"]
        for row in table.filter(pl.col("entry")).to_dicts()
    }
    assert scores == {"AAA": 3.0, "BBB": 1.0, "CCC": 4.0, "DDD": 2.0}


def test_raw_score_ties_use_hash() -> None:
    symbols = ["AAA", "BBB"]
    signals = Signals(
        entries=_scored_entries(
            [("AAA", 2, 50.0, 0.5), ("BBB", 2, 50.0, 0.5)]
        ),
        sells=_sells([]),
    )
    bars = pl.concat([_bars(symbol) for symbol in symbols])
    table, _ = compose_table(signals, bars, _elig(symbols))
    scores = {
        row["symbol"]: row["score"]
        for row in table.filter(pl.col("entry")).to_dicts()
    }

    hashes = {s: entry_score(s, DATES[2]) for s in symbols}
    best = max(symbols, key=hashes.__getitem__)
    worst = min(symbols, key=hashes.__getitem__)
    assert scores[best] == 2.0
    assert scores[worst] == 1.0
    assert len(set(scores.values())) == 2


def test_raw_score_ranks_are_per_date() -> None:
    symbols = ["AAA", "BBB", "CCC"]
    signals = Signals(
        entries=_scored_entries(
            [
                ("AAA", 1, 50.0, 0.2),
                ("BBB", 1, 50.0, 0.9),
                ("CCC", 1, 50.0, 0.5),
                ("BBB", 5, 50.0, 0.1),
                ("CCC", 5, 50.0, 0.8),
            ]
        ),
        sells=_sells([]),
    )
    bars = pl.concat([_bars(symbol) for symbol in symbols])
    table, _ = compose_table(signals, bars, _elig(symbols))
    entries = table.filter(pl.col("entry")).to_dicts()
    day1 = {r["symbol"]: r["score"] for r in entries if r["date"] == DATES[1]}
    day5 = {r["symbol"]: r["score"] for r in entries if r["date"] == DATES[5]}
    assert day1 == {"AAA": 1.0, "BBB": 3.0, "CCC": 2.0}
    assert day5 == {"BBB": 1.0, "CCC": 2.0}


def test_raw_score_merge_takes_max() -> None:
    symbols = ["AAA", "BBB"]
    signals = Signals(
        entries=_scored_entries(
            [
                ("AAA", 2, 50.0, 0.2),
                ("AAA", 2, 55.0, 0.7),
                ("BBB", 2, 50.0, 0.5),
            ]
        ),
        sells=_sells([]),
    )
    bars = pl.concat([_bars(symbol) for symbol in symbols])
    table, log = compose_table(signals, bars, _elig(symbols))

    assert log["emitted"] == 2
    aaa = table.filter(pl.col("symbol") == "AAA").to_dicts()
    assert len(aaa) == 1
    assert aaa[0]["invalidation"] == 55.0
    scores = {
        row["symbol"]: row["score"]
        for row in table.filter(pl.col("entry")).to_dicts()
    }
    assert scores == {"AAA": 2.0, "BBB": 1.0}


def test_without_raw_score_unchanged() -> None:
    signals = Signals(
        entries=_entries(
            [("AAA", 1, 50.0, "x"), ("BBB", 1, 50.0, "x"), ("CCC", 4, 50.0, "x")]
        ),
        sells=_sells([("AAA", 6)]),
    )
    symbols = ["AAA", "BBB", "CCC"]
    bars = pl.concat([_bars(symbol) for symbol in symbols])
    table, _ = compose_table(signals, bars, _elig(symbols))
    entry_rows = table.filter(pl.col("entry")).to_dicts()
    assert entry_rows
    for row in entry_rows:
        assert row["score"] == entry_score(row["symbol"], row["date"])
    assert table.filter(pl.col("exit")).height == 1
    exit_rows = table.filter(pl.col("exit")).to_dicts()
    assert all(row["score"] == 0.0 for row in exit_rows)


def test_scores_survive_float32() -> None:
    symbols = [f"S{i:03d}" for i in range(300)]
    signals = Signals(
        entries=_scored_entries(
            [(symbol, 2, 50.0, i / 300.0) for i, symbol in enumerate(symbols)]
        ),
        sells=_sells([]),
    )
    bars = pl.concat([_bars(symbol) for symbol in symbols])
    table, _ = compose_table(signals, bars, _elig(symbols))
    scores32 = table.select(pl.col("score").cast(pl.Float32))["score"]
    assert table.filter(pl.col("entry")).height == 300
    assert scores32.n_unique() == 300
