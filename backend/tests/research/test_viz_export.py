"""Strategy replay exporter: slots, legs, payload contract and data.js writing."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.research import viz_export

# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

START = date(2024, 1, 1)
END = date(2024, 1, 31)
BAR_START = START - timedelta(days=200)


def _period() -> SimpleNamespace:
    return SimpleNamespace(name="dev", start=START, end=END)


def _equity() -> pl.DataFrame:
    dates = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    return pl.DataFrame(
        {
            "date": dates,
            "equity": [100_000.0 + 100.0 * i for i in range(len(dates))],
        },
        schema_overrides={"date": pl.Date},
    )


def _bars() -> pl.DataFrame:
    rows: list[dict] = []
    dates = [BAR_START + timedelta(days=i) for i in range((END - BAR_START).days + 1)]
    for i, d in enumerate(dates):
        base = 100.0 + (i % 5)
        rows.append(
            {
                "symbol": "AAA",
                "date": d,
                "open": base,
                "high": base + 2.0,
                "low": base - 3.0,
                "close": base + 1.0,
                "volume": 1_000.0 + i,
            }
        )
        base = 50.0 + (i % 4)
        rows.append(
            {
                "symbol": "BBB",
                "date": d,
                "open": base,
                "high": base + 2.0,
                "low": base - 1.0,
                "close": base + 0.5,
                "volume": 2_000.0 + i,
            }
        )
        rows.append(
            {
                "symbol": "SPY",
                "date": d,
                "open": 400.0 + 0.5 * i,
                "high": 401.0 + 0.5 * i,
                "low": 399.0 + 0.5 * i,
                "close": 400.5 + 0.5 * i,
                "volume": 10_000.0,
            }
        )
    return pl.DataFrame(rows, schema_overrides={"date": pl.Date})


def _table() -> pl.DataFrame:
    rows = [
        ("AAA", date(2024, 1, 5), 3.0, 0.3, 95.0, 4.0),
        ("BBB", date(2024, 1, 5), 1.0, 0.1, 49.0, 2.0),
        ("AAA", date(2024, 1, 8), 2.0, 0.2, 99.0, 4.0),
        ("BBB", date(2024, 1, 8), 5.0, 0.5, 48.0, 2.0),
        ("AAA", date(2024, 1, 10), 4.0, 0.4, 101.0, 4.0),
        ("BBB", date(2024, 1, 10), 0.0, 0.0, 49.5, 2.0),
    ]
    return pl.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "date": [row[1] for row in rows],
            "entry": [True] * len(rows),
            "exit": [False] * len(rows),
            "score": [row[2] for row in rows],
            "raw_score": [row[3] for row in rows],
            "invalidation": [row[4] for row in rows],
            "atr14": [row[5] for row in rows],
            "close": [100.0] * len(rows),
        },
        schema_overrides={
            "date": pl.Date,
            "entry": pl.Boolean,
            "exit": pl.Boolean,
        },
    )


def _points() -> pl.DataFrame:
    rows = [
        {
            "symbol": "AAA",
            "type": "2B",
            "known_date": date(2024, 1, 5),
            "leg_prev_start": BAR_START,
            "leg_prev_end": BAR_START + timedelta(days=10),
            "leg_last_start": BAR_START + timedelta(days=10),
            "leg_last_end": BAR_START + timedelta(days=20),
        },
        {
            "symbol": "AAA",
            "type": "2B",
            "known_date": date(2024, 1, 10),
            "leg_prev_start": date(2023, 12, 1),
            "leg_prev_end": date(2023, 12, 10),
            "leg_last_start": date(2023, 12, 10),
            "leg_last_end": date(2023, 12, 20),
        },
        {
            "symbol": "BBB",
            "type": "2B",
            "known_date": date(2024, 1, 8),
            "leg_prev_start": date(2023, 12, 15),
            "leg_prev_end": date(2023, 12, 20),
            "leg_last_start": date(2023, 12, 20),
            "leg_last_end": date(2023, 12, 28),
        },
    ]
    return pl.DataFrame(
        rows,
        schema_overrides={
            "known_date": pl.Date,
            "leg_prev_start": pl.Date,
            "leg_prev_end": pl.Date,
            "leg_last_start": pl.Date,
            "leg_last_end": pl.Date,
        },
    )


def _trades() -> list[dict]:
    # Deliberately not in entry order: the payload ids follow entry order.
    return [
        {
            "symbol": "BBB",
            "entry_signal_date": "2024-01-08",
            "entry_date": "2024-01-09",
            "exit_date": "2024-01-16",
            "entry_price": 50.0,
            "exit_price": 45.0,
            "duration": 7,
            "exit_reason": "signal",
        },
        {
            "symbol": "AAA",
            "entry_signal_date": "2024-01-05",
            "entry_date": "2024-01-08",
            "exit_date": "2024-01-12",
            "entry_price": 100.0,
            "exit_price": 110.0,
            "duration": 4,
            "exit_reason": "take_profit",
        },
        {
            "symbol": "AAA",
            "entry_signal_date": "2024-01-10",
            "entry_date": "2024-01-15",
            "exit_date": None,
            "entry_price": 105.0,
            "exit_price": None,
            "duration": None,
            "exit_reason": None,
        },
    ]


def _stats() -> dict:
    return {
        "cagr": 0.123456789,
        "sharpe_ann": 1.23456789,
        "max_drawdown": -0.25,
        "trades": 3,
        "win_rate": 2 / 3,
        "avg_r": 0.75,
    }


def _payload() -> dict:
    return viz_export.build_payload(
        variant_id="H1-2B~retr",
        config={"base": "H1-2B", "rank": "retr", "slots": 2},
        period=_period(),
        trades=_trades(),
        equity=_equity(),
        table=_table(),
        bars=_bars(),
        points=_points(),
        retr_spec=(("2B",), "known_date"),
        stats=_stats(),
        holdout=False,
        generated_at="2024-02-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Slot lanes
# ---------------------------------------------------------------------------


def test_assign_slots_reuses_lane_after_exit() -> None:
    trades = [
        {"symbol": "C", "entry_date": "2024-01-03", "exit_date": "2024-01-05"},
        {"symbol": "A", "entry_date": "2024-01-01", "exit_date": "2024-01-03"},
        {"symbol": "B", "entry_date": "2024-01-02", "exit_date": "2024-01-04"},
    ]

    assert viz_export.assign_slots(trades, 2) == [0, 0, 1]


def test_assign_slots_open_trade_keeps_lane() -> None:
    trades = [
        {"symbol": "A", "entry_date": "2024-01-01", "exit_date": None},
        {"symbol": "B", "entry_date": "2024-01-03", "exit_date": "2024-01-04"},
    ]

    with pytest.raises(ValueError):
        viz_export.assign_slots(trades, 1)


def test_assign_slots_overflow_raises() -> None:
    trades = [
        {"symbol": "A", "entry_date": "2024-01-01", "exit_date": "2024-01-10"},
        {"symbol": "B", "entry_date": "2024-01-02", "exit_date": "2024-01-10"},
        {"symbol": "C", "entry_date": "2024-01-03", "exit_date": "2024-01-10"},
    ]

    with pytest.raises(ValueError):
        viz_export.assign_slots(trades, 2)


# ---------------------------------------------------------------------------
# Legs
# ---------------------------------------------------------------------------


def test_leg_vertices_up_then_down() -> None:
    bars = pl.DataFrame(
        {
            "date": [date(2024, 1, 1), date(2024, 1, 5), date(2024, 1, 10)],
            "high": [10.0, 12.0, 9.0],
            "low": [5.0, 7.0, 4.0],
        },
        schema_overrides={"date": pl.Date},
    )
    point = {
        "leg_prev_start": date(2024, 1, 1),
        "leg_prev_end": date(2024, 1, 5),
        "leg_last_start": date(2024, 1, 5),
        "leg_last_end": date(2024, 1, 10),
    }

    assert viz_export.leg_vertices(point, bars) == [
        ["2024-01-01", 5.0],
        ["2024-01-05", 12.0],
        ["2024-01-10", 4.0],
    ]


def test_leg_vertices_missing_leg_date_is_none() -> None:
    bars = pl.DataFrame(
        {
            "date": [date(2024, 1, 1), date(2024, 1, 5), date(2024, 1, 10)],
            "high": [10.0, 12.0, 9.0],
            "low": [5.0, 7.0, 4.0],
        },
        schema_overrides={"date": pl.Date},
    )
    point = {
        "leg_prev_start": date(2024, 1, 1),
        "leg_prev_end": date(2024, 1, 5),
        "leg_last_start": date(2024, 1, 5),
        "leg_last_end": None,
    }

    assert viz_export.leg_vertices(point, bars) is None


# ---------------------------------------------------------------------------
# Payload contract
# ---------------------------------------------------------------------------


def test_payload_contract() -> None:
    payload = _payload()

    assert set(payload) == {"meta", "days", "equity", "spy", "slots_used", "trades", "signals", "bars"}
    assert set(payload["meta"]) == {
        "variant",
        "base",
        "rank",
        "slots",
        "period",
        "start",
        "end",
        "generated_at",
        "holdout",
        "stats",
        "spy_cagr",
    }
    assert set(payload["meta"]["stats"]) == {
        "cagr",
        "sharpe",
        "max_drawdown",
        "trades",
        "win_rate",
        "avg_r",
    }
    assert payload["meta"]["stats"]["sharpe"] == pytest.approx(1.234568)
    assert payload["meta"]["stats"]["trades"] == 3
    assert payload["meta"]["period"] == "dev"
    assert payload["meta"]["holdout"] is False

    days = payload["days"]
    assert len(days) == 31
    assert days[0] == "2024-01-01"
    assert days[-1] == "2024-01-31"
    assert len(payload["equity"]) == len(days)
    assert len(payload["spy"]) == len(days)
    assert len(payload["slots_used"]) == len(days)
    assert payload["equity"][0] == 1.0
    assert payload["spy"][0] == 1.0

    signals = payload["signals"]
    assert [item["date"] for item in signals] == ["2024-01-05", "2024-01-08", "2024-01-10"]
    assert [len(item["items"]) for item in signals] == [2, 2, 2]
    assert [item["symbol"] for item in signals[0]["items"]] == ["AAA", "BBB"]
    assert [item["score"] for item in signals[1]["items"]] == [5.0, 2.0]
    assert [item["symbol"] for item in signals[2]["items"]] == ["AAA", "BBB"]

    taken = {
        (item["symbol"], signal["date"])
        for signal in signals
        for item in signal["items"]
        if item["taken"]
    }
    assert taken == {
        ("AAA", "2024-01-05"),
        ("BBB", "2024-01-08"),
        ("AAA", "2024-01-10"),
    }

    trades = payload["trades"]
    assert [trade["id"] for trade in trades] == [0, 1, 2]
    assert [trade["symbol"] for trade in trades] == ["AAA", "BBB", "AAA"]
    assert [trade["entry_date"] for trade in trades] == ["2024-01-08", "2024-01-09", "2024-01-15"]
    assert [trade["slot"] for trade in trades] == [0, 1, 0]
    assert trades[0]["ret"] == pytest.approx(0.1)
    assert trades[0]["r"] == pytest.approx(2.0)
    assert trades[1]["ret"] == pytest.approx(-0.1)
    assert trades[1]["r"] == pytest.approx(-2.5)
    assert trades[2]["exit_date"] is None
    assert trades[2]["exit_price"] is None
    assert trades[2]["ret"] is None
    assert trades[2]["r"] is None
    assert trades[0]["hold_days"] == 4
    assert trades[2]["hold_days"] is None
    assert len(trades[0]["legs"]) == 3
    assert trades[0]["legs"][0][0] == BAR_START.isoformat()

    slots_by_day = dict(zip(days, payload["slots_used"], strict=True))
    assert slots_by_day["2024-01-08"] == 1
    assert slots_by_day["2024-01-09"] == 2
    assert slots_by_day["2024-01-12"] == 1
    assert slots_by_day["2024-01-20"] == 1

    assert set(payload["bars"]) == {"AAA", "BBB", "SPY"}
    window_start = min(START - timedelta(days=180), BAR_START)
    for series in payload["bars"].values():
        assert series["d"][0] == window_start.isoformat()
        assert series["d"][-1] <= END.isoformat()
        assert len(series["d"]) == len(series["o"]) == len(series["h"])
        assert len(series["l"]) == len(series["c"]) == len(series["v"])


def test_payload_no_lookahead_fields() -> None:
    payload = _payload()

    for trade in payload["trades"]:
        assert trade["entry_date"] > trade["signal_date"]

    start, end = payload["meta"]["start"], payload["meta"]["end"]
    for signal in payload["signals"]:
        assert start <= signal["date"] <= end


def test_unmatched_trade_raises() -> None:
    trades = [
        {
            "symbol": "ZZZ",
            "entry_signal_date": "2024-01-05",
            "entry_date": "2024-01-08",
            "exit_date": "2024-01-10",
            "entry_price": 10.0,
            "exit_price": 11.0,
            "duration": 2,
            "exit_reason": "signal",
        }
    ]
    empty_table = pl.DataFrame(
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "entry": pl.Boolean,
            "exit": pl.Boolean,
            "score": pl.Float64,
            "invalidation": pl.Float64,
            "atr14": pl.Float64,
            "close": pl.Float64,
        }
    )
    bars = pl.DataFrame(
        {
            "symbol": ["SPY", "ZZZ"],
            "date": [START, START],
            "open": [400.0, 10.0],
            "high": [401.0, 11.0],
            "low": [399.0, 9.0],
            "close": [400.0, 10.0],
            "volume": [1.0, 1.0],
        },
        schema_overrides={"date": pl.Date},
    )

    with pytest.raises(ValueError, match="ZZZ"):
        viz_export.build_payload(
            variant_id="H1-2B~retr",
            config={"base": "H1-2B", "rank": "retr", "slots": 1},
            period=_period(),
            trades=trades,
            equity=_equity(),
            table=empty_table,
            bars=bars,
            points=None,
            retr_spec=None,
            stats={},
            holdout=False,
            generated_at="2024-02-01T00:00:00+00:00",
        )


# ---------------------------------------------------------------------------
# data.js writer
# ---------------------------------------------------------------------------


def test_write_data_js_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "current.js"
    payload = {
        "a": float("nan"),
        "b": [1.0, float("inf")],
        "c": {"d": float("-inf"), "e": "keep"},
    }

    viz_export.write_data_js(payload, path)

    text = path.read_text(encoding="utf-8")
    assert text.startswith("window.ZT_VIZ = ")
    assert text.endswith(";\n")
    loaded = json.loads(text[len("window.ZT_VIZ = ") : -2])
    assert loaded == {"a": None, "b": [1.0, None], "c": {"d": None, "e": "keep"}}