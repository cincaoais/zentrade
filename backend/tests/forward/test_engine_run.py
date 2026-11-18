"""Morning engine run: an open-only bar for t+1 decides the day's orders.

The fixture stores are built entirely under ``tmp_path``. Nothing here touches
the real data directory, OpenD or the network.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.backtest.engine import MatcherConfig
from app.forward.engine_run import (
    Action,
    actions_on,
    build_engine_store,
    engine_config,
    open_only_rows,
    relevant_symbols,
    run_engine,
)
from app.forward.tables import build_inputs, forward_table
from app.research.compose import entry_score
from app.research.evaluate import Period, run_table

START = date(2025, 1, 6)
DAYS = [START + timedelta(days=i) for i in range(60)]
SYMBOLS = [f"S{index:02d}" for index in range(12)]
T = DAYS[58]
T1 = DAYS[59]
PAD = T1 + timedelta(days=1)

_EMPTY = pl.DataFrame()

_TABLE_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "entry": pl.Boolean,
    "exit": pl.Boolean,
    "score": pl.Float64,
}


def _price(symbol: str, day_index: int) -> float:
    base = 20.0 + 5.0 * SYMBOLS.index(symbol)
    return base * (1.0 + 0.002 * day_index)


def _bars(symbol: str, days: list[date]) -> pl.DataFrame:
    rows = []
    for day in days:
        index = DAYS.index(day)
        price = _price(symbol, index)
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1_000_000.0 + 1_000.0 * index,
                "amount": price * 1_000_000.0,
                "raw_close": price,
                "raw_high": price * 1.01,
                "raw_low": price * 0.99,
            }
        )
    return pl.DataFrame(rows)


def _all_bars(count: int = len(DAYS)) -> pl.DataFrame:
    return pl.concat([_bars(symbol, DAYS[:count]) for symbol in SYMBOLS])


def _spy_bars(days: list[date]) -> pl.DataFrame:
    rows = []
    for day in days:
        index = DAYS.index(day)
        price = 100.0 * (1.0 + 0.003 * index)
        rows.append(
            {
                "symbol": "SPY",
                "date": day,
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 5_000_000.0,
                "amount": price * 5_000_000.0,
                "raw_close": price,
                "raw_high": price * 1.01,
                "raw_low": price * 0.99,
            }
        )
    return pl.DataFrame(rows)


def _write_store(root: Path, bars: pl.DataFrame) -> Path:
    for key, part in bars.group_by(["date"], maintain_order=True):
        day = key[0] if isinstance(key, tuple) else key
        target = root / "kline_daily_enriched" / f"date={day.isoformat()}"
        target.mkdir(parents=True, exist_ok=True)
        part.sort("symbol").write_parquet(target / "part.parquet")

    instruments = root / "instruments"
    instruments.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": SYMBOLS,
            "name": [f"Fixture {symbol}" for symbol in SYMBOLS],
            "asset_type": ["stock"] * len(SYMBOLS),
            "instrument_type": ["stock"] * len(SYMBOLS),
            "is_etf": [False] * len(SYMBOLS),
            "is_preferred": [False] * len(SYMBOLS),
            "is_warrant": [False] * len(SYMBOLS),
            "is_unit": [False] * len(SYMBOLS),
            "total_shares": [1_000_000_000.0] * len(SYMBOLS),
            "float_shares": [1_000_000_000.0] * len(SYMBOLS),
        }
    ).write_parquet(instruments / "instruments.parquet")
    return root


def _table(rows: list[tuple[str, date, bool, bool, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "date": [row[1] for row in rows],
            "entry": [row[2] for row in rows],
            "exit": [row[3] for row in rows],
            "score": [row[4] for row in rows],
        },
        schema=_TABLE_SCHEMA,
    )


def _day_t_bars(count: int = 59) -> pl.DataFrame:
    return _all_bars(count).filter(pl.col("date") == T)


def _matcher() -> MatcherConfig:
    return MatcherConfig(
        entry_fill="open_t+1",
        exit_fill="open_t+1",
        slippage_bps=5.0,
        max_positions=10,
    )


def _tree_hash(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


def test_engine_config_matches_run_table_except_initial_capital(tmp_path) -> None:
    captured: dict[str, object] = {}

    class _Service:
        def run(self, config):
            captured["config"] = config
            return config

    table_path = tmp_path / "table.parquet"
    run_table(_Service(), table_path, Period("forward", DAYS[10], DAYS[20]))

    expected = asdict(captured["config"])
    ours = asdict(engine_config(table_path, DAYS[10], DAYS[20], 250_000.0))

    assert ours.pop("initial_capital") == 250_000.0
    expected.pop("initial_capital")
    assert ours == expected


def test_end_sales_never_actions(tmp_path) -> None:
    src = _write_store(tmp_path / "src", _all_bars())
    table = _table([("S00", DAYS[40], True, False, 1.0)])
    dst = tmp_path / "dst"
    build_engine_store(src, dst, SYMBOLS, DAYS[0], DAYS[-1], None)

    result = run_engine(dst, table, DAYS[0], DAYS[-1], 250_000.0)

    end_trades = [trade for trade in result.trades if trade["exit_reason"] == "end"]
    assert end_trades
    assert {trade["exit_date"] for trade in end_trades} == {DAYS[-1].isoformat()}
    assert actions_on(result, DAYS[-1]) == []


def test_entry_on_t_needs_open_only_bar(tmp_path) -> None:
    src = _write_store(tmp_path / "src", _all_bars(59))
    table = _table([("S00", T, True, False, 1.0)])

    without = tmp_path / "without"
    build_engine_store(src, without, SYMBOLS, DAYS[0], T1, None)
    bare = run_engine(without, table, DAYS[0], T1, 250_000.0)
    assert actions_on(bare, T1) == []

    extra = open_only_rows(_day_t_bars(), {"S00": 100.0}, T1, PAD)
    with_open = tmp_path / "with_open"
    build_engine_store(src, with_open, SYMBOLS, DAYS[0], PAD, extra)
    result = run_engine(with_open, table, DAYS[0], PAD, 250_000.0)

    actions = actions_on(result, T1)
    assert len(actions) == 1
    assert actions[0] == Action(
        kind="buy",
        symbol="S00",
        shares=round(result.trades[0]["shares"]),
        price=100.0,
        reason="entry",
        signal_date=T.isoformat(),
    )


def test_t1_entry_recorded_via_pad(tmp_path) -> None:
    src = _write_store(tmp_path / "src", _all_bars(59))
    table = _table([("S00", T, True, False, 1.0)])
    extra = open_only_rows(_day_t_bars(), {"S00": 100.0}, T1, PAD)
    dst = tmp_path / "dst"
    build_engine_store(src, dst, SYMBOLS, DAYS[0], PAD, extra)

    result = run_engine(dst, table, DAYS[0], PAD, 250_000.0)

    assert result.error is None
    trade = next(
        trade for trade in result.trades if trade["entry_date"] == T1.isoformat()
    )
    assert trade["exit_date"] == PAD.isoformat()
    assert trade["exit_reason"] == "end"
    actions = actions_on(result, T1)
    assert len(actions) == 1
    assert actions[0] == Action(
        kind="buy",
        symbol="S00",
        shares=round(trade["shares"]),
        price=100.0,
        reason="entry",
        signal_date=T.isoformat(),
    )


def test_pad_bar_values_do_not_change_t1_actions(tmp_path) -> None:
    src = _write_store(tmp_path / "src", _all_bars(59))
    table = _table(
        [
            ("S01", DAYS[55], True, False, 1.0),
            ("S01", T, False, True, 0.0),
            ("S00", T, True, False, 2.0),
        ]
    )
    opens = {"S00": 100.0, "S01": _price("S01", 59)}
    extra = open_only_rows(_day_t_bars(), opens, T1, PAD)

    def scaled(factor: float) -> pl.DataFrame:
        is_pad = pl.col("date") == PAD
        return extra.with_columns(
            *[
                pl.when(is_pad)
                .then(pl.col(column) * factor)
                .otherwise(pl.col(column))
                .alias(column)
                for column in ("open", "high", "low", "close")
            ],
            pl.when(is_pad)
            .then(pl.col("volume") * 10.0)
            .otherwise(pl.col("volume"))
            .alias("volume"),
        )

    def signature(result: object) -> list[tuple[str, str, int, float, str]]:
        return [
            (action.kind, action.symbol, action.shares, action.price, action.reason)
            for action in actions_on(result, T1)
        ]

    half_store = tmp_path / "half"
    build_engine_store(src, half_store, SYMBOLS, DAYS[0], PAD, scaled(0.5))
    half = run_engine(half_store, table, DAYS[0], PAD, 250_000.0)

    double_store = tmp_path / "double"
    build_engine_store(src, double_store, SYMBOLS, DAYS[0], PAD, scaled(2.0))
    double = run_engine(double_store, table, DAYS[0], PAD, 250_000.0)

    assert signature(half) == signature(double)
    assert [action.kind for action in actions_on(half, T1)] == ["sell", "buy"]


def test_shares_follow_equity(tmp_path) -> None:
    src = _write_store(tmp_path / "src", _all_bars(59))
    # S01 exits at t1's open after a large gain, so the prior P&L is large
    # enough to move the whole-share count of the next 10% position.
    extra = open_only_rows(
        _day_t_bars(),
        {"S00": _price("S00", 59), "S01": _price("S01", 59) * 1.2},
        T1,
        PAD,
    )
    dst = tmp_path / "dst"
    build_engine_store(src, dst, SYMBOLS, DAYS[0], PAD, extra)
    capital = 250_000.0

    with_prior = _table(
        [
            ("S01", DAYS[39], True, False, 1.0),
            ("S01", T, False, True, 0.0),
            ("S00", T, True, False, 2.0),
        ]
    )
    result = run_engine(dst, with_prior, DAYS[0], PAD, capital)
    prior = next(
        trade for trade in result.trades if trade["symbol"] == "S01"
    )
    assert prior["exit_date"] == T1.isoformat()
    assert prior["exit_reason"] == "signal"

    equity_before = capital - prior["entry_value"] + prior["exit_value"]
    expected = _matcher().affordable_shares(equity_before / 10, _price("S00", 59))
    actions = [action for action in actions_on(result, T1) if action.symbol == "S00"]
    assert len(actions) == 1
    assert actions[0].shares == expected

    without_prior = _table([("S00", T, True, False, 2.0)])
    plain = run_engine(dst, without_prior, DAYS[0], PAD, capital)
    plain_expected = _matcher().affordable_shares(capital / 10, _price("S00", 59))
    plain_action = actions_on(plain, T1)[0]
    assert plain_action.shares == plain_expected
    assert plain_action.shares != actions[0].shares


def test_gapped_top_candidate_promotes_next(tmp_path) -> None:
    candidates = ["S00", "S01"]
    gapped = max(candidates, key=lambda symbol: entry_score(symbol, T))
    survivor = next(symbol for symbol in candidates if symbol != gapped)

    opens = {symbol: _price(symbol, 59) for symbol in candidates}
    stop_ref = {
        gapped: opens[gapped] + 5.0,
        survivor: opens[survivor] - 5.0,
    }

    bars = pl.concat(
        [
            _day_t_bars(),
            open_only_rows(_day_t_bars(), opens, T1, PAD).filter(
                pl.col("date") == T1
            ),
        ]
    )
    wyk_events = pl.DataFrame(
        {
            "symbol": candidates,
            "kind": ["entry_steady", "entry_steady"],
            "stop_ref": [stop_ref[gapped], stop_ref[survivor]],
            "range_hi": [0.0, 0.0],
            "atr_known": [1.0, 1.0],
            "known_date": [T, T],
        },
        schema={
            "symbol": pl.String,
            "kind": pl.String,
            "stop_ref": pl.Float64,
            "range_hi": pl.Float64,
            "atr_known": pl.Float64,
            "known_date": pl.Date,
        },
    )
    inputs = build_inputs(
        bars, _spy_bars(DAYS[:59]), _EMPTY, wyk_events, _EMPTY
    )
    elig = pl.DataFrame(
        {
            "symbol": candidates,
            "date": [T, T],
            "eligible": [True, True],
        },
        schema={"symbol": pl.String, "date": pl.Date, "eligible": pl.Boolean},
    )
    table, log = forward_table("H2-spring", inputs, elig, inputs.bars)
    assert log["kept"] == 1
    entries = table.filter(pl.col("entry"))
    assert entries["symbol"].to_list() == [survivor]

    src = _write_store(tmp_path / "src", _all_bars(59))
    dst = tmp_path / "dst"
    extra = open_only_rows(_day_t_bars(), opens, T1, PAD)
    build_engine_store(src, dst, SYMBOLS, DAYS[0], PAD, extra)
    result = run_engine(dst, table, DAYS[0], PAD, 250_000.0)

    actions = actions_on(result, T1)
    assert [action.symbol for action in actions] == [survivor]
    assert gapped not in [action.symbol for action in actions]


def test_no_open_row_means_no_trade_for_symbol(tmp_path) -> None:
    src = _write_store(tmp_path / "src", _all_bars(59))
    table = _table(
        [
            ("S00", T, True, False, 1.0),
            ("S01", T, True, False, 1.0),
        ]
    )
    extra = open_only_rows(_day_t_bars(), {"S00": 100.0}, T1, PAD)
    dst = tmp_path / "dst"
    build_engine_store(src, dst, SYMBOLS, DAYS[0], PAD, extra)

    result = run_engine(dst, table, DAYS[0], PAD, 250_000.0)

    symbols = [action.symbol for action in actions_on(result, T1)]
    assert symbols == ["S00"]
    assert "S01" not in symbols


def test_restricted_symbols_same_trades(tmp_path) -> None:
    src = _write_store(tmp_path / "src", _all_bars())
    table = _table(
        [
            ("S00", DAYS[20], True, False, 1.0),
            ("S00", DAYS[35], False, True, 0.0),
            ("S05", DAYS[40], True, False, 1.0),
            ("S09", T, True, False, 1.0),
        ]
    )
    full_store = tmp_path / "full"
    build_engine_store(src, full_store, SYMBOLS, DAYS[0], DAYS[-1], None)
    full = run_engine(full_store, table, DAYS[0], DAYS[-1], 250_000.0)

    kept = relevant_symbols(table, DAYS[0], set(), ["S09"])
    assert kept == ["S00", "S05", "S09"]
    restricted_store = tmp_path / "restricted"
    build_engine_store(src, restricted_store, kept, DAYS[0], DAYS[-1], None)
    restricted = run_engine(restricted_store, table, DAYS[0], DAYS[-1], 250_000.0)

    assert full.error is None
    assert restricted.error is None
    assert restricted.trades == full.trades
    assert [(point["date"], point["value"]) for point in restricted.equity_curve] == [
        (point["date"], point["value"]) for point in full.equity_curve
    ]


def test_source_store_untouched(tmp_path) -> None:
    src = _write_store(tmp_path / "src", _all_bars())
    before = _tree_hash(src)
    table = _table([("S00", T, True, False, 1.0)])
    dst = tmp_path / "dst"

    build_engine_store(src, dst, SYMBOLS, DAYS[0], DAYS[-1], None)
    result = run_engine(dst, table, DAYS[0], DAYS[-1], 250_000.0)

    assert result.error is None
    assert _tree_hash(src) == before
    assert not (dst / ".backtest_matrix_cache").exists()

def test_relevant_symbols_take_night_candidates_explicitly() -> None:
    # At night the composed table has no day-t entries (compose drops them for
    # lack of a next bar), so today's candidates must come from candidates_on.
    table = pl.DataFrame(
        {
            "symbol": ["OLD"],
            "date": [DAYS[5]],
            "entry": [True],
            "exit": [False],
            "score": [1.0],
        }
    )
    assert relevant_symbols(table, DAYS[0], {"HELD"}, ["NEW1", "NEW2"]) == [
        "HELD", "NEW1", "NEW2", "OLD",
    ]
