"""Chan point-in-time replay core: first-seen, confirmation, gone, legs."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import polars as pl

from app.research.chan_replay import GONE_COLUMNS, POINT_COLUMNS, replay_symbol


def _dates(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _bars(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": _dates(n),
            "open": [10.0] * n,
            "high": [11.0] * n,
            "low": [9.0] * n,
            "close": [10.0] * n,
            "volume": [1_000_000] * n,
            "amount": [10_000_000.0] * n,
        }
    )


def _point(
    *,
    type: str = "1B",
    dt: str = "2024-05-09",
    side: str = "buy",
    price: float = 10.0,
    invalidation_price: float = 9.0,
    basis: str = "trend_divergence",
    state: str = "candidate",
) -> dict:
    return {
        "type": type,
        "side": side,
        "dt": dt,
        "price": price,
        "invalidation_price": invalidation_price,
        "basis": basis,
        "state": state,
        # Fields that must never be persisted.
        "still_valid": True,
        "bars_since": 0,
    }


def _bi(start: str, end: str) -> dict:
    return {"start_dt": start, "end_dt": end, "direction": "up", "state": "confirmed"}


def _snap(status: str = "available", points=(), bis=()) -> dict:
    return {
        "status": status,
        "buy_sell_points": list(points),
        "recent_bis": list(bis),
    }


def _scripted(mapping: dict[int, dict]):
    """Snapshot fake keyed by ``len(frame)``; unlisted lengths are empty/available."""

    def snapshot_fn(frame: pd.DataFrame) -> dict:
        return mapping.get(len(frame), _snap())

    return snapshot_fn


def test_first_seen_row_dated_at_first_appearance():
    dates = _dates(140)
    mapping = {n: _snap(points=[_point(dt="2024-05-09")]) for n in range(130, 141)}
    points, gone, _ = replay_symbol(
        "AAA", _bars(140), _scripted(mapping), "engine-v1"
    )

    assert points.height == 1
    assert points["known_date"].to_list() == [dates[129]]
    assert gone.height == 0


def test_confirmed_date_recorded_later():
    dates = _dates(140)
    mapping = {
        130: _snap(points=[_point(state="candidate")]),
        131: _snap(points=[_point(state="candidate")]),
        132: _snap(points=[_point(state="candidate")]),
        133: _snap(points=[_point(state="confirmed")]),
        134: _snap(points=[_point(state="confirmed")]),
    }
    points, _, _ = replay_symbol("AAA", _bars(140), _scripted(mapping), "engine-v1")

    assert points.height == 1
    assert points["state_at_known"].to_list() == ["candidate"]
    assert points["known_date"].to_list() == [dates[129]]
    assert points["confirmed_date"].to_list() == [dates[132]]


def test_confirmed_on_first_seen_day_uses_known_date():
    dates = _dates(140)
    mapping = {130: _snap(points=[_point(state="confirmed")])}
    points, _, _ = replay_symbol("AAA", _bars(140), _scripted(mapping), "engine-v1")

    assert points["confirmed_date"].to_list() == [dates[129]]


def test_gone_row_with_window_start():
    dates = _dates(140)
    point = _point(dt="2024-05-09")
    bis = [_bi("2024-04-20", "2024-05-01"), _bi("2024-05-01", "2024-05-09")]
    mapping = {
        130: _snap(points=[point], bis=bis),
        131: _snap(points=[point], bis=bis),
        132: _snap(bis=bis),
    }
    points, gone, _ = replay_symbol("AAA", _bars(140), _scripted(mapping), "engine-v1")

    assert points.height == 1
    assert gone.height == 1
    row = gone.row(0, named=True)
    assert row["gone_date"] == dates[131]
    assert row["window_start"] == date(2024, 4, 20)
    assert row["first_known_date"] == dates[129]
    assert row["type"] == "1B"
    assert row["point_dt"] == date(2024, 5, 9)


def test_reappear_keeps_first_seen():
    dates = _dates(140)
    point = _point(dt="2024-05-09")
    mapping = {
        130: _snap(points=[point]),
        131: _snap(points=[point]),
        132: _snap(),
        133: _snap(points=[point]),
        **{n: _snap(points=[point]) for n in range(134, 141)},
    }
    points, gone, _ = replay_symbol("AAA", _bars(140), _scripted(mapping), "engine-v1")

    assert points.height == 1
    assert points["known_date"].to_list() == [dates[129]]
    assert gone.height == 1
    assert gone["gone_date"].to_list() == [dates[131]]


def test_second_disappearance_adds_new_gone_row():
    dates = _dates(140)
    point = _point(dt="2024-05-09")
    mapping = {
        130: _snap(points=[point]),
        131: _snap(),
        132: _snap(points=[point]),
        133: _snap(),
    }
    points, gone, _ = replay_symbol("AAA", _bars(140), _scripted(mapping), "engine-v1")

    assert points.height == 1
    assert gone.height == 2
    assert gone["gone_date"].to_list() == [dates[130], dates[132]]


def test_legs_from_matching_stroke():
    point = _point(dt="2024-05-09")
    bis = [
        _bi("2024-04-20", "2024-05-01"),
        _bi("2024-05-01", "2024-05-09"),
        _bi("2024-05-09", "2024-05-20"),
    ]
    mapping = {130: _snap(points=[point], bis=bis)}
    points, _, _ = replay_symbol("AAA", _bars(140), _scripted(mapping), "engine-v1")

    row = points.row(0, named=True)
    assert row["leg_last_start"] == date(2024, 5, 1)
    assert row["leg_last_end"] == date(2024, 5, 9)
    assert row["leg_prev_start"] == date(2024, 4, 20)
    assert row["leg_prev_end"] == date(2024, 5, 1)


def test_legs_null_when_stroke_not_found():
    point = _point(dt="2024-06-01")
    bis = [_bi("2024-04-20", "2024-05-01")]
    mapping = {130: _snap(points=[point], bis=bis)}
    points, _, _ = replay_symbol("AAA", _bars(140), _scripted(mapping), "engine-v1")

    row = points.row(0, named=True)
    assert row["leg_last_start"] is None
    assert row["leg_last_end"] is None
    assert row["leg_prev_start"] is None
    assert row["leg_prev_end"] is None


def test_no_future_fields_stored():
    mapping = {130: _snap(points=[_point(dt="2024-05-09", state="confirmed")])}
    points, gone, _ = replay_symbol("AAA", _bars(140), _scripted(mapping), "engine-v1")

    assert tuple(points.columns) == POINT_COLUMNS
    assert tuple(gone.columns) == GONE_COLUMNS
    assert "still_valid" not in points.columns
    assert "bars_since" not in points.columns
    assert points.row(0, named=True)["engine_version"] == "engine-v1"


def test_unavailable_days_skipped():
    dates = _dates(140)
    point = _point(dt="2024-05-09")
    status_by_len = {130: "partial", 132: "error", 134: "partial"}
    mapping = {
        n: _snap(status=status_by_len.get(n, "available"), points=[point])
        for n in range(130, 141)
    }
    points, gone, stats = replay_symbol(
        "AAA", _bars(140), _scripted(mapping), "engine-v1"
    )

    # Length 130 is partial and 132 is error, so the first usable day is length 131.
    assert points.height == 1
    assert points["known_date"].to_list() == [dates[130]]
    assert gone.height == 0

    expected_days = len(range(119, 140))
    expected_available = sum(
        1 for n in range(120, 141) if status_by_len.get(n, "available") == "available"
    )
    expected_error = sum(
        1 for n in range(120, 141) if status_by_len.get(n, "available") == "error"
    )
    assert stats == {
        "days": expected_days,
        "available_days": expected_available,
        "error_days": expected_error,
    }


def test_start_index_controls_first_day():
    dates = _dates(140)
    point = _point(dt="2024-05-09")
    mapping = {n: _snap(points=[point]) for n in range(125, 141)}
    points, _, stats = replay_symbol(
        "AAA", _bars(140), _scripted(mapping), "engine-v1", start_index=130
    )

    assert stats["days"] == 10
    assert points["known_date"].to_list() == [dates[130]]


def test_empty_result_has_typed_columns():
    points, gone, stats = replay_symbol("AAA", _bars(140), _scripted({}), "engine-v1")

    assert points.height == 0
    assert gone.height == 0
    assert tuple(points.columns) == POINT_COLUMNS
    assert tuple(gone.columns) == GONE_COLUMNS
    assert points.schema["known_date"] == pl.Date
    assert points.schema["confirmed_date"] == pl.Date
    assert gone.schema["gone_date"] == pl.Date
    assert stats == {"days": 21, "available_days": 21, "error_days": 0}