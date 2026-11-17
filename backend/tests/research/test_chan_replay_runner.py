"""Chan replay runner: parallel parts, resume, crash isolation, determinism."""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import polars as pl

from app.research.chan_replay import (
    chan_dir,
    load_chan_gone,
    load_chan_points,
    run_chan_replay,
)

N_BARS = 200


def _dates(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _bar_row(symbol: str, day: date, i: int) -> dict:
    # ``BAD`` carries a sentinel price so the module-level fake can raise for it.
    close = 999.0 if symbol == "BAD" else 10.0 + i * 0.1 + (i % 7) * 0.05
    return {
        "symbol": symbol,
        "date": day,
        "open": close - 0.1,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 1_000_000.0,
        "amount": close * 1_000_000.0,
        "raw_close": close,
    }


def _write_store(data_dir: Path, symbols: list[str], n: int = N_BARS) -> None:
    """Write one enriched partition per day, exactly like the real store."""
    for i, day in enumerate(_dates(n)):
        path = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}"
        path.mkdir(parents=True, exist_ok=True)
        rows = [_bar_row(symbol, day, i) for symbol in symbols]
        pl.DataFrame(rows).write_parquet(path / "part.parquet")


def fake_snapshot_fn(frame: pd.DataFrame) -> dict:
    """Module-level (picklable) deterministic fake snapshot.

    ``BAD`` symbols raise so the runner's per-symbol error handling is exercised.
    """
    if float(frame["close"].iloc[0]) == 999.0:
        raise RuntimeError("engine blew up on purpose")

    length = len(frame)
    last_date = pd.Timestamp(frame["date"].iloc[-1]).date()
    dt = last_date.isoformat()
    back = (last_date - timedelta(days=3)).isoformat()

    points: list[dict] = []
    if length >= 125:
        points.append(
            {
                "type": "1B",
                "side": "buy",
                "dt": back,
                "price": 10.0,
                "invalidation_price": 9.0,
                "basis": "trend_divergence",
                "state": "candidate" if length % 2 else "confirmed",
                "still_valid": True,
                "bars_since": 1,
            }
        )
    if length >= 130:
        points.append(
            {
                "type": "1S",
                "side": "sell",
                "dt": dt,
                "price": 11.0,
                "invalidation_price": 12.0,
                "basis": "trend_divergence",
                "state": "confirmed",
                "still_valid": True,
                "bars_since": 0,
            }
        )

    return {
        "status": "available",
        "buy_sell_points": points,
        "recent_bis": [
            {"start_dt": back, "end_dt": dt, "direction": "up", "state": "confirmed"}
        ],
    }


def test_runner_writes_parts_and_resumes(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    _write_store(data_dir, ["AAA", "BBB", "CCC"])

    first = run_chan_replay(data_dir, ["AAA", "BBB", "CCC"], workers=2, snapshot_fn=fake_snapshot_fn)
    assert first["done"] == 3
    assert first["skipped_existing"] == 0
    assert first["failed"] == []

    points_dir = chan_dir(data_dir) / "points"
    for symbol in ("AAA", "BBB", "CCC"):
        assert (points_dir / f"{symbol}.parquet").exists()

    second = run_chan_replay(data_dir, ["AAA", "BBB", "CCC"], workers=2, snapshot_fn=fake_snapshot_fn)
    assert second["done"] == 0
    assert second["skipped_existing"] == 3
    assert second["failed"] == []


def test_runner_parts_load_concatenated(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    _write_store(data_dir, ["AAA", "BBB", "CCC"])
    run_chan_replay(data_dir, ["AAA", "BBB", "CCC"], workers=2, snapshot_fn=fake_snapshot_fn)

    points = load_chan_points(data_dir)
    assert set(points["symbol"].unique().to_list()) == {"AAA", "BBB", "CCC"}
    assert points.height > 0

    gone = load_chan_gone(data_dir)
    assert gone.columns == [
        "symbol",
        "type",
        "point_dt",
        "first_known_date",
        "gone_date",
        "window_start",
    ]


def test_runner_only_passes_engine_columns(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    _write_store(data_dir, ["AAA"])

    def capturing_snapshot_fn(frame: pd.DataFrame) -> dict:
        assert list(frame.columns) == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ]
        return {"status": "partial"}

    run_chan_replay(data_dir, ["AAA"], workers=1, snapshot_fn=capturing_snapshot_fn)


def test_engine_error_symbol_skipped_and_counted(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    _write_store(data_dir, ["GOOD", "BAD"])

    result = run_chan_replay(data_dir, ["GOOD", "BAD"], workers=2, snapshot_fn=fake_snapshot_fn)

    assert result["failed"] == ["BAD"]
    assert result["done"] == 1

    points_dir = chan_dir(data_dir) / "points"
    assert (points_dir / "GOOD.parquet").exists()
    assert not (points_dir / "BAD.parquet").exists()
    assert list(points_dir.glob("*.tmp")) == []


def test_replay_deterministic_bytes(tmp_path: Path) -> None:
    symbols = ["AAA", "BBB", "CCC"]
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_store(left, symbols)
    _write_store(right, symbols)

    run_chan_replay(left, symbols, workers=2, snapshot_fn=fake_snapshot_fn)
    run_chan_replay(right, symbols, workers=2, snapshot_fn=fake_snapshot_fn)

    for symbol in symbols:
        a = (chan_dir(left) / "points" / f"{symbol}.parquet").read_bytes()
        b = (chan_dir(right) / "points" / f"{symbol}.parquet").read_bytes()
        assert a == b


def test_real_engine_smoke(tmp_path: Path) -> None:
    """~160 bars keeps the real engine smoke test near the measured ~6 s."""
    data_dir = tmp_path / "store"
    n = 160
    closes = [100 + 10 * math.sin(i / 7) + i * 0.05 for i in range(n)]
    for i, day in enumerate(_dates(n)):
        path = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}"
        path.mkdir(parents=True, exist_ok=True)
        close = closes[i]
        pl.DataFrame(
            [
                {
                    "symbol": "ZZZ",
                    "date": day,
                    "open": closes[i - 1] if i else close,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": 1_000_000.0,
                    "amount": close * 1_000_000.0,
                    "raw_close": close,
                }
            ]
        ).write_parquet(path / "part.parquet")

    result = run_chan_replay(data_dir, ["ZZZ"], workers=1)

    assert result["done"] == 1
    assert result["failed"] == []
    assert (chan_dir(data_dir) / "points" / "ZZZ.parquet").exists()
