"""Forward point extension: day-by-day Chan must equal a fresh replay."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import polars as pl

from app.forward.signals import (
    extend_points,
    load_points_through,
    load_stored_chan,
    verify_sample,
)
from app.research.chan_replay import ENGINE_COLUMNS, replay_symbol
from app.research.versions import chan_engine_version

_START = date(2024, 1, 1)

def _days(n: int) -> list[date]:
    return [_START + timedelta(days=i) for i in range(n)]

def _bar_row(symbol: str, day: date, close: float = 10.0) -> dict:
    return {
        "symbol": symbol,
        "date": day,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": close,
        "volume": 1_000_000.0,
        "amount": 10_000_000.0,
        "raw_close": close,
    }

def _write_bars(root: Path, rows: list[dict]) -> None:
    path = Path(root) / "kline_daily_enriched" / "date=2024-01-01"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path / "part.parquet")

def _point(
    *,
    point_type: str = "1B",
    dt: str = "2024-05-09",
    state: str = "candidate",
) -> dict:
    return {
        "type": point_type,
        "side": "buy",
        "dt": dt,
        "price": 10.0,
        "invalidation_price": 9.0,
        "basis": "trend_divergence",
        "state": state,
        "still_valid": True,
        "bars_since": 0,
    }

def _snap(points=()) -> dict:
    return {
        "status": "available",
        "buy_sell_points": list(points),
        "recent_bis": [],
    }

def _scripted(mapping: dict[int, dict]):
    """Snapshot fake keyed by ``len(frame)``; unlisted lengths are empty/available."""

    def snapshot_fn(frame: pd.DataFrame) -> dict:
        return mapping.get(len(frame), _snap())

    return snapshot_fn

def _fresh_chan(root: Path, symbol: str, through: date, snapshot_fn):
    from app.forward.data import load_forward_bars

    bars = load_forward_bars(root, symbols=[symbol], end=through)
    frame = bars.to_pandas().loc[:, list(ENGINE_COLUMNS)]
    return replay_symbol(symbol, frame, snapshot_fn, chan_engine_version())

def test_extend_day_by_day_equals_fresh(tmp_path: Path) -> None:
    """Seed once, then extend one day at a time: points and gone rows match replay."""
    n = 140
    days = _days(n)
    _write_bars(tmp_path, [_bar_row("AAA", day) for day in days])

    # First seen on t=129, still a candidate through the seed (t=131), confirmed
    # on t=132, then gone on t=134.
    mapping: dict[int, dict] = {
        length: _snap(points=[_point(state="candidate")])
        for length in range(130, 133)
    }
    mapping[133] = _snap(points=[_point(state="confirmed")])
    mapping[134] = _snap(points=[_point(state="confirmed")])
    for length in range(135, n + 1):
        mapping[length] = _snap()
    fake = _scripted(mapping)

    seeded = extend_points(tmp_path, ["AAA"], days[131], snapshot_fn=fake)
    assert seeded == {"extended": [], "replayed": ["AAA"], "failed": []}

    for index in range(132, n):
        result = extend_points(tmp_path, ["AAA"], days[index], snapshot_fn=fake)
        assert result == {"extended": ["AAA"], "replayed": [], "failed": []}, index

    fresh_points, fresh_gone, _ = _fresh_chan(tmp_path, "AAA", days[-1], fake)
    stored_points, stored_gone = load_stored_chan(tmp_path, "AAA")

    assert stored_points.equals(fresh_points)
    assert stored_gone.equals(fresh_gone)

    # The scenario itself: known first, confirmed later, gone later still.
    assert stored_points["known_date"].to_list() == [days[129]]
    assert stored_points["confirmed_date"].to_list() == [days[132]]
    assert stored_gone["first_known_date"].to_list() == [days[129]]
    assert stored_gone["gone_date"].to_list() == [days[134]]

def test_history_change_triggers_full_replay(tmp_path: Path) -> None:
    n = 140
    days = _days(n)
    rows = [_bar_row("AAA", day) for day in days]
    _write_bars(tmp_path, rows)
    fake = _scripted(
        {length: _snap(points=[_point()]) for length in range(130, n + 1)}
    )

    first = extend_points(tmp_path, ["AAA"], days[135], snapshot_fn=fake)
    assert first["replayed"] == ["AAA"]

    # An old bar is revised below the day already extended through.
    rows[10] = _bar_row("AAA", days[10], close=12.5)
    _write_bars(tmp_path, rows)

    result = extend_points(tmp_path, ["AAA"], days[-1], snapshot_fn=fake)
    assert result == {"extended": [], "replayed": ["AAA"], "failed": []}

    fresh_points, fresh_gone, _ = _fresh_chan(tmp_path, "AAA", days[-1], fake)
    stored_points, stored_gone = load_stored_chan(tmp_path, "AAA")
    assert stored_points.equals(fresh_points)
    assert stored_gone.equals(fresh_gone)

def test_load_points_through_filters_known_date(tmp_path: Path) -> None:
    n = 140
    days = _days(n)
    _write_bars(tmp_path, [_bar_row("AAA", day) for day in days])
    fake = _scripted(
        {length: _snap(points=[_point()]) for length in range(130, n + 1)}
    )
    extend_points(tmp_path, ["AAA"], days[-1], snapshot_fn=fake)

    # A synthetic Wyckoff event dated later than the Chan point.
    event = {
        "symbol": "ZZZ",
        "known_date": days[135],
        "kind": "sos",
        "idx_date": days[134],
        "price": 1.0,
        "stop_ref": None,
        "range_kind": "accumulation",
        "range_lo": None,
        "range_hi": None,
        "atr_known": 1.0,
        "port_version": "v1",
    }
    events_dir = tmp_path / "forward" / "points" / "wyckoff_events"
    events_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([event]).write_parquet(events_dir / "ZZZ.parquet")

    early_chan, early_events, _ = load_points_through(tmp_path, days[130])
    assert early_chan["known_date"].to_list() == [days[129]]
    assert early_events.height == 0

    late_chan, late_events, _ = load_points_through(tmp_path, days[-1])
    assert late_chan.height == 1
    assert "ZZZ" in late_events["symbol"].to_list()

def test_verify_sample_reports_difference(tmp_path: Path) -> None:
    n = 140
    days = _days(n)
    _write_bars(tmp_path, [_bar_row("AAA", day) for day in days])
    fake = _scripted(
        {length: _snap(points=[_point()]) for length in range(130, n + 1)}
    )
    extend_points(tmp_path, ["AAA"], days[-1], snapshot_fn=fake)

    assert verify_sample(tmp_path, ["AAA"], days[-1], snapshot_fn=fake) == []

    points_path = tmp_path / "forward" / "points" / "chan" / "AAA.parquet"
    stored = pl.read_parquet(points_path)
    stored.with_columns((pl.col("price") + 1.0).alias("price")).write_parquet(
        points_path
    )

    assert verify_sample(tmp_path, ["AAA"], days[-1], snapshot_fn=fake) == ["AAA"]

def test_extension_matches_replay_across_unavailable_days(tmp_path: Path) -> None:
    """Partial and error days are skipped exactly as in the pinned loop."""
    n = 140
    days = _days(n)
    _write_bars(tmp_path, [_bar_row("AAA", day) for day in days])
    point = _point()
    mapping: dict[int, dict] = {}
    for length in range(130, n + 1):
        status = "available"
        if length == 133:
            status = "partial"
        if length == 135:
            status = "error"
        points = [point] if length < 137 else []
        mapping[length] = {
            "status": status,
            "buy_sell_points": points,
            "recent_bis": [],
        }
    fake = _scripted(mapping)

    extend_points(tmp_path, ["AAA"], days[130], snapshot_fn=fake)
    for index in range(131, n):
        result = extend_points(tmp_path, ["AAA"], days[index], snapshot_fn=fake)
        assert result["extended"] == ["AAA"]

    fresh_points, fresh_gone, fresh_stats = _fresh_chan(
        tmp_path, "AAA", days[-1], fake
    )
    stored_points, stored_gone = load_stored_chan(tmp_path, "AAA")
    assert stored_points.equals(fresh_points)
    assert stored_gone.equals(fresh_gone)
    assert stored_gone["gone_date"].to_list() == [days[136]]
    assert fresh_stats["error_days"] == 1
