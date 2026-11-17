"""Integrity checks: truncation, phantom rate and byte determinism."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from app.research.chan_replay import run_chan_replay
from app.research.integrity import (
    chan_truncation_check,
    determinism_check,
    phantom_rate,
    wyckoff_truncation_check,
)
from app.research.wyckoff.replay import run_wyckoff_replay, wyckoff_dir

from .wyckoff.shapes import accumulation

N_BARS = 220
START = date(2024, 1, 1)
POINT_DT = START + timedelta(days=129)

# Mutable global used by ``leaky_snapshot``: the point price is no longer a
# function of the prefix, so a truncated replay cannot reproduce the stored row.
LEAK = {"price": 10.0}


def _dates(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def _write_chan_store(data_dir: Path, symbols: list[str], n: int = N_BARS) -> None:
    for i, day in enumerate(_dates(n)):
        path = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}"
        path.mkdir(parents=True, exist_ok=True)
        rows = []
        for symbol in symbols:
            close = 10.0 + i * 0.1 + (i % 7) * 0.05
            rows.append(
                {
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
            )
        pl.DataFrame(rows).write_parquet(path / "part.parquet")


def _point(price: float, length: int) -> dict:
    return {
        "type": "1B",
        "side": "buy",
        "dt": POINT_DT,
        "price": price,
        "invalidation_price": 9.0,
        "basis": "trend_divergence",
        "state": "confirmed" if length >= 133 else "candidate",
    }


def honest_snapshot(frame: pd.DataFrame) -> dict:
    """Causal fake: the snapshot depends only on the frame it is given."""
    length = len(frame)
    points = [_point(10.0, length)] if length >= 130 else []
    return {
        "status": "available",
        "buy_sell_points": points,
        "recent_bis": [
            {
                "start_dt": START + timedelta(days=119),
                "end_dt": POINT_DT,
            }
        ],
    }


def leaky_snapshot(frame: pd.DataFrame) -> dict:
    """Non-causal fake: the price leaks the future through the global."""
    length = len(frame)
    points = [_point(LEAK["price"], length)] if length >= 130 else []
    return {
        "status": "available",
        "buy_sell_points": points,
        "recent_bis": [
            {
                "start_dt": START + timedelta(days=119),
                "end_dt": POINT_DT,
            }
        ],
    }


def test_chan_truncation_detects_injected_leak(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    _write_chan_store(data_dir, ["AAA"])
    run_chan_replay(data_dir, ["AAA"], workers=1, snapshot_fn=honest_snapshot)

    honest = chan_truncation_check(
        data_dir, ["AAA"], cuts_per_symbol=3, seed=0, snapshot_fn=honest_snapshot
    )
    assert honest["checked"] > 0
    assert honest["mismatches"] == []

    LEAK["price"] = 42.0
    try:
        leaky = chan_truncation_check(
            data_dir, ["AAA"], cuts_per_symbol=3, seed=0, snapshot_fn=leaky_snapshot
        )
    finally:
        LEAK["price"] = 10.0

    assert leaky["checked"] == honest["checked"]
    assert leaky["mismatches"]
    assert leaky["mismatches"][0]["symbol"] == "AAA"


def test_wyckoff_truncation_honest_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    _write_wyckoff_store(data_dir, ["AAA"])
    run_wyckoff_replay(data_dir, ["AAA"], workers=1)
    assert pl.read_parquet(wyckoff_dir(data_dir) / "events" / "AAA.parquet").height > 0

    result = wyckoff_truncation_check(data_dir, ["AAA"], cuts_per_symbol=2, seed=0)
    assert result["checked"] > 0
    assert result["mismatches"] == []


def test_phantom_counts_only_inside_window() -> None:
    points = pl.DataFrame(
        {
            "type": ["1B", "1B", "1B"],
            "point_dt": [
                date(2024, 1, 3),
                date(2024, 1, 4),
                date(2024, 1, 5),
            ],
        }
    )
    gone = pl.DataFrame(
        {
            "type": ["1B", "1B"],
            "point_dt": [date(2024, 1, 5), date(2024, 1, 10)],
            "window_start": [date(2024, 1, 9), date(2024, 1, 9)],
        }
    )

    rates = phantom_rate(points, gone)

    assert rates.columns == ["type", "first_seen", "phantoms", "rate"]
    row = rates.row(0, named=True)
    assert row["type"] == "1B"
    assert row["first_seen"] == 3
    assert row["phantoms"] == 1
    assert row["rate"] == 1 / 3


def _wyckoff_series() -> tuple[np.ndarray, ...]:
    """The accumulation-cycle shape the ported detectors are built around."""
    candles, _marks = accumulation()
    return candles.o, candles.h, candles.l, candles.c, candles.v


def _write_wyckoff_store(data_dir: Path, symbols: list[str]) -> None:
    open_, high, low, close, volume = _wyckoff_series()
    day = date(2024, 1, 2)
    path = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}"
    path.mkdir(parents=True, exist_ok=True)
    rows = []
    for symbol in symbols:
        for i in range(len(close)):
            rows.append(
                {
                    "symbol": symbol,
                    "date": day + timedelta(days=i),
                    "open": float(open_[i]),
                    "high": float(high[i]),
                    "low": float(low[i]),
                    "close": float(close[i]),
                    "volume": float(volume[i]),
                    "amount": float(close[i] * volume[i]),
                    "raw_close": float(close[i]),
                }
            )
    pl.DataFrame(rows).write_parquet(path / "part.parquet")


def test_determinism_detects_change(tmp_path: Path) -> None:
    data_dir = tmp_path / "store"
    _write_wyckoff_store(data_dir, ["AAA"])
    run_wyckoff_replay(data_dir, ["AAA"], workers=1)

    first = determinism_check(data_dir, ["AAA"])
    assert first["different"] == []
    assert first["identical"] == 2

    ranges_path = wyckoff_dir(data_dir) / "ranges" / "AAA.parquet"
    ranges_path.write_bytes(ranges_path.read_bytes() + b"changed")

    second = determinism_check(data_dir, ["AAA"])
    assert second["identical"] == 1
    assert len(second["different"]) == 1
    assert "ranges/AAA.parquet" in second["different"][0]
