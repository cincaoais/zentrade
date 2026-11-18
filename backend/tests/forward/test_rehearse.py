"""Historical rehearsal: replay the morning procedure over a fixture year.

The fixture lives entirely under ``tmp_path``. Points are fake Wyckoff events,
the store is written directly as enriched partitions, and ``next_pad_day`` is
replaced by "the next calendar day" so no market calendar or network is used.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.forward import rehearse as rehearse_module
from app.forward.rehearse import rehearse
from app.forward.tables import build_inputs, forward_table
from app.research.bars import load_bars, tradable_symbols
from app.research.chan_replay import load_chan_points
from app.research.universe import UniverseParams, eligibility
from app.research.wyckoff.replay import (
    load_wyckoff_events,
    load_wyckoff_ranges,
    wyckoff_dir,
)

DAYS = [date(2024, 1, 1) + timedelta(days=index) for index in range(280)]
START_I = 250
END_I = 279
START = DAYS[START_I]
END = DAYS[END_I]
SYMBOLS = ("AAA", "BBB", "CCC")
VARIANT = "H2-spring"

_BASE = {"AAA": 100.0, "BBB": 50.0, "CCC": 80.0}
_CRASH_I = 254


def _price(symbol: str, index: int) -> float:
    return _BASE[symbol] * (1.0 + 0.002 * index)


def _bar(symbol: str, index: int) -> dict:
    price = _price(symbol, index)
    open_ = high = low = close = price
    high = price * 1.01
    low = price * 0.99
    if symbol == "AAA" and index == _CRASH_I:
        entry_price = _price("AAA", _CRASH_I - 1)
        open_ = entry_price * 0.92
        high = entry_price * 0.95
        low = entry_price * 0.84
        close = entry_price * 0.90
    return {
        "symbol": symbol,
        "date": DAYS[index],
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000_000.0,
        "amount": close * 1_000_000.0,
        "raw_close": close,
        "raw_high": high,
        "raw_low": low,
    }


def _spy_bar(index: int) -> dict:
    price = 400.0 * (1.0 + 0.001 * index)
    return {
        "symbol": "SPY",
        "date": DAYS[index],
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


def _write_store(data_dir: Path) -> None:
    rows = [_bar(symbol, index) for symbol in SYMBOLS for index in range(len(DAYS))]
    rows += [_spy_bar(index) for index in range(len(DAYS))]
    bars = pl.DataFrame(rows)
    for keys, part in bars.group_by(["date"], maintain_order=True):
        day = keys[0] if isinstance(keys, tuple) else keys
        target = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}"
        target.mkdir(parents=True, exist_ok=True)
        part.sort("symbol").write_parquet(target / "part.parquet")

    instruments = data_dir / "instruments"
    instruments.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        [
            {
                "symbol": symbol,
                "name": symbol,
                "asset_type": "stock",
                "instrument_type": "common",
                "is_etf": False,
                "is_preferred": False,
                "is_warrant": False,
                "is_unit": False,
            }
            for symbol in SYMBOLS
        ]
    ).write_parquet(instruments / "instruments.parquet")


def _event(
    symbol: str,
    index: int,
    kind: str,
    *,
    stop_ref: float | None = None,
) -> dict:
    price = _price(symbol, index)
    return {
        "symbol": symbol,
        "known_date": DAYS[index],
        "kind": kind,
        "idx_date": DAYS[index],
        "price": price,
        "stop_ref": stop_ref,
        "range_kind": "accumulation",
        "range_lo": price * 0.8,
        "range_hi": price * 1.1,
        "atr_known": price * 0.02,
        "port_version": "fixture",
    }


_EVENT_SCHEMA = {
    "symbol": pl.String,
    "known_date": pl.Date,
    "kind": pl.String,
    "idx_date": pl.Date,
    "price": pl.Float64,
    "stop_ref": pl.Float64,
    "range_kind": pl.String,
    "range_lo": pl.Float64,
    "range_hi": pl.Float64,
    "atr_known": pl.Float64,
    "port_version": pl.String,
}


def _write_events(data_dir: Path) -> None:
    events = [
        _event("AAA", 252, "entry_steady", stop_ref=0.88 * _price("AAA", 252)),
        _event("BBB", 256, "entry_steady", stop_ref=0.88 * _price("BBB", 256)),
        _event("CCC", 258, "entry_steady", stop_ref=0.88 * _price("CCC", 258)),
        _event("BBB", 262, "sow"),
        _event("AAA", 265, "entry_steady", stop_ref=0.88 * _price("AAA", 265)),
        _event("BBB", 268, "entry_steady", stop_ref=0.88 * _price("BBB", 268)),
        _event("AAA", 273, "sow"),
    ]
    directory = wyckoff_dir(data_dir) / "events"
    directory.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(events, schema=_EVENT_SCHEMA).write_parquet(directory / "ALL.parquet")


def _write_fixture(data_dir: Path) -> None:
    _write_store(data_dir)
    _write_events(data_dir)


def _write_frozen_table(data_dir: Path) -> None:
    """The table ``scripts/research/compose.py`` would have frozen for this store."""
    bars = load_bars(data_dir)
    spy = load_bars(data_dir, symbols=["SPY"])
    inputs = build_inputs(
        bars,
        spy,
        load_chan_points(data_dir),
        load_wyckoff_events(data_dir),
        load_wyckoff_ranges(data_dir),
    )
    elig = eligibility(bars, tradable_symbols(data_dir), UniverseParams())
    table, _log = forward_table(VARIANT, inputs, elig, inputs.bars)
    directory = data_dir / "research" / "variants" / VARIANT
    directory.mkdir(parents=True)
    table.write_parquet(directory / "frozen.parquet", compression="zstd", statistics=False)


@pytest.fixture
def fixture_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    _write_fixture(data_dir)
    _write_frozen_table(data_dir)
    monkeypatch.setattr(
        rehearse_module, "next_pad_day", lambda t1: t1 + timedelta(days=1)
    )
    return data_dir


def test_rehearsal_reproduces_reference_on_fixture(
    tmp_path: Path, fixture_tree: Path
) -> None:
    result = rehearse(
        fixture_tree,
        VARIANT,
        START,
        END,
        1_000_000.0,
        tmp_path / "work",
    )

    assert result["differences"] == []
    assert result["days"] == END_I - START_I
    assert result["intraday_stops"] == 1
    assert result["reference_trades"] > 0
    assert result["rehearsal_trades"] > 0


def test_rehearsal_reports_injected_difference(
    tmp_path: Path, fixture_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_actions_on = rehearse_module.actions_on
    dropped = {"done": False}

    def dropping_actions_on(result: object, t1: date) -> list:
        actions = real_actions_on(result, t1)
        if not dropped["done"]:
            for index, action in enumerate(actions):
                if action.kind == "buy":
                    dropped["done"] = True
                    return actions[:index] + actions[index + 1 :]
        return actions

    monkeypatch.setattr(rehearse_module, "actions_on", dropping_actions_on)

    result = rehearse(
        fixture_tree,
        VARIANT,
        START,
        END,
        1_000_000.0,
        tmp_path / "work",
    )

    assert dropped["done"] is True
    assert len(result["differences"]) == 1

def test_open_exit_overridden_by_intraday_stop_is_classified() -> None:
    # Final-year COIN case: the morning sells at the open for max_hold, while the
    # full-bar engine checks risk exits first and records a stop at the stop
    # price on the same day. That is the frozen engine's ordering, not a rule
    # mismatch, so it is reported apart from differences.
    from collections import Counter

    from app.forward.rehearse import compare_actions

    collected = Counter({("COIN", "2026-07-31", "sell", 715, "max_hold"): 1})
    expected: Counter = Counter()
    intraday = [
        {"symbol": "COIN", "date": date(2026, 7, 31), "low_ok": True, "low": 139.11, "stop_price": 139.4}
    ]

    differences, overridden = compare_actions(collected, expected, intraday)

    assert differences == []
    assert overridden == ["COIN 2026-07-31 open max_hold vs intraday stop_loss"]


def test_unexplained_extra_sell_is_still_a_difference() -> None:
    from collections import Counter

    from app.forward.rehearse import compare_actions

    collected = Counter({("COIN", "2026-07-31", "sell", 715, "max_hold"): 1})
    differences, overridden = compare_actions(collected, Counter(), [])

    assert differences == ["unexpected sell COIN 2026-07-31 shares=715 reason=max_hold"]
    assert overridden == []
