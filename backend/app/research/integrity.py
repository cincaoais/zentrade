"""Integrity checks over stored replay parts.

Three independent guards:

* **Truncation** -- replaying a prefix of the bars must reproduce every stored
  row whose ``known_date`` falls inside that prefix. A row that changes proves
  the engine (or a fake in a test) reads bars after the decision date.
* **Phantoms** -- a point that vanishes while its ``point_dt`` is still inside
  the day's visible stroke window (``point_dt >= window_start``) was redrawn
  away: a genuine repaint. Points that vanish only because the window moved
  past them are not counted.
* **Determinism** -- replaying into a fresh directory must reproduce the stored
  parquet bytes exactly.

All checks are read-only with respect to ``data_dir``: determinism replays into
a temporary directory that only holds a copy of the needed bars.
"""

from __future__ import annotations

import math
import random
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl

from app.research.bars import load_bars
from app.research.chan_replay import (
    ENGINE_COLUMNS,
    POINT_COLUMNS,
    chan_dir,
    replay_symbol,
    run_chan_replay,
)
from app.research.compose import assert_no_future_columns
from app.research.versions import chan_engine_version, wyckoff_port_version
from app.research.wyckoff.replay import (
    BAR_INPUT_COLUMNS,
    EVENT_COLUMNS,
    RANGE_COLUMNS,
    run_wyckoff_replay,
    wyckoff_dir,
    wyckoff_symbol,
)

# A cut shorter than this cannot contain a useful replay history.
MIN_CUT = 200

_CHAN_FIELDS = tuple(
    column for column in POINT_COLUMNS if column not in ("symbol", "engine_version")
)


def _as_date(value: Any) -> date | None:
    """Normalise a bar date or a ``*_dt`` value to a plain ``date``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        return None
    return timestamp.date()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return value


def _same_value(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if (
        isinstance(left, float)
        and isinstance(right, float)
        and math.isnan(left)
        and math.isnan(right)
    ):
        return True
    return left == right


def _default_snapshot(frame: pd.DataFrame) -> dict:
    """The real daily engine; imported lazily so this module stays light."""
    from app.research.vendor.dsa_chan.engine import build_level_snapshot

    return build_level_snapshot(frame, "1d")


def _load_frame(data_dir: Path, symbol: str, columns: tuple[str, ...]) -> pd.DataFrame | None:
    bars = load_bars(Path(data_dir), symbols=[symbol])
    if bars.height == 0:
        return None
    frame = bars.to_pandas()
    return frame.loc[:, list(columns)]


def _read_part(path: Path) -> pl.DataFrame | None:
    if not path.exists():
        return None
    return pl.read_parquet(path)


def _cuts(length: int, cuts_per_symbol: int, rng: random.Random) -> list[int]:
    """Random cut indices in ``[MIN_CUT, length - 1]``, sorted for stable output."""
    available = length - MIN_CUT
    if available <= 0:
        return []
    count = min(cuts_per_symbol, available)
    return sorted(rng.sample(range(MIN_CUT, length), count))


def _normalize_confirmed(frame: pl.DataFrame, cut_date: date) -> pl.DataFrame:
    """A confirmation after the cut is unknowable: expect it as null."""
    return frame.with_columns(
        pl.when(pl.col("confirmed_date") > pl.lit(cut_date, dtype=pl.Date))
        .then(pl.lit(None, dtype=pl.Date))
        .otherwise(pl.col("confirmed_date"))
        .alias("confirmed_date")
    )


def _normalize_end_date(frame: pl.DataFrame, cut_date: date) -> pl.DataFrame:
    """A range ending after the cut is still open as far as the cut can know."""
    return frame.with_columns(
        pl.when(pl.col("end_date") > pl.lit(cut_date, dtype=pl.Date))
        .then(pl.lit(None, dtype=pl.Date))
        .otherwise(pl.col("end_date"))
        .alias("end_date")
    )


def _base_mismatch(symbol: str, check: str, cut: int, cut_date: date) -> dict:
    return {
        "symbol": symbol,
        "check": check,
        "cut": cut,
        "cut_date": cut_date.isoformat(),
    }


def _compare_chan(
    symbol: str,
    cut: int,
    cut_date: date,
    expected: pl.DataFrame,
    actual: pl.DataFrame,
) -> list[dict]:
    expected_map = {(row["type"], row["point_dt"]): row for row in expected.iter_rows(named=True)}
    actual_map = {(row["type"], row["point_dt"]): row for row in actual.iter_rows(named=True)}

    mismatches: list[dict] = []
    keys = sorted(
        set(expected_map) | set(actual_map),
        key=lambda key: (str(key[0]), str(key[1])),
    )
    for key in keys:
        base = _base_mismatch(symbol, "chan_points", cut, cut_date)
        base["type"] = key[0]
        base["point_dt"] = None if key[1] is None else key[1].isoformat()
        if key not in actual_map:
            mismatches.append({**base, "reason": "missing_in_replay"})
        elif key not in expected_map:
            mismatches.append({**base, "reason": "extra_in_replay"})
        else:
            expected_row = expected_map[key]
            actual_row = actual_map[key]
            diffs = {
                field: [_jsonable(expected_row[field]), _jsonable(actual_row[field])]
                for field in _CHAN_FIELDS
                if not _same_value(expected_row[field], actual_row[field])
            }
            if diffs:
                mismatches.append({**base, "reason": "field_diff", "fields": diffs})
    return mismatches


def _row_tuples(frame: pl.DataFrame, columns: tuple[str, ...]) -> list[tuple]:
    fields = [column for column in columns if column != "symbol"]
    rows = [tuple(row) for row in frame.select(fields).iter_rows()]
    return sorted(rows, key=lambda row: tuple(repr(value) for value in row))


def _compare_rows(
    check: str,
    symbol: str,
    cut: int,
    cut_date: date,
    expected: pl.DataFrame,
    actual: pl.DataFrame,
    columns: tuple[str, ...],
) -> list[dict]:
    expected_rows = _row_tuples(expected, columns)
    actual_rows = _row_tuples(actual, columns)
    if expected_rows == actual_rows:
        return []

    expected_counts = Counter(expected_rows)
    actual_counts = Counter(actual_rows)
    missing = [
        [_jsonable(value) for value in row]
        for row in list((expected_counts - actual_counts).elements())[:5]
    ]
    extra = [
        [_jsonable(value) for value in row]
        for row in list((actual_counts - expected_counts).elements())[:5]
    ]
    return [
        {
            **_base_mismatch(symbol, check, cut, cut_date),
            "reason": "rows_differ",
            "stored_rows": len(expected_rows),
            "replay_rows": len(actual_rows),
            "missing": missing,
            "extra": extra,
        }
    ]


def chan_truncation_check(
    data_dir: Path,
    symbols: list[str],
    cuts_per_symbol: int = 5,
    seed: int = 0,
    snapshot_fn: Callable[[pd.DataFrame], dict] | None = None,
) -> dict:
    """Compare truncated Chan replays with the stored points parts.

    For each symbol, ``cuts_per_symbol`` random prefixes are replayed. The
    stored points with ``known_date <= date[cut - 1]`` must equal the truncated
    replay; a stored ``confirmed_date`` after the cut is compared as null.
    ``snapshot_fn=None`` uses the real engine.
    """
    data_dir = Path(data_dir)
    engine_version = chan_engine_version()
    rng = random.Random(seed)
    snapshot = snapshot_fn if snapshot_fn is not None else _default_snapshot
    checked = 0
    mismatches: list[dict] = []

    for symbol in symbols:
        frame = _load_frame(data_dir, symbol, ENGINE_COLUMNS)
        if frame is None:
            continue

        stored = _read_part(chan_dir(data_dir) / "points" / f"{symbol}.parquet")
        if stored is None:
            mismatches.append(
                {
                    "symbol": symbol,
                    "check": "chan_points",
                    "reason": "missing_stored_part",
                }
            )
            continue
        try:
            assert_no_future_columns(stored)
        except ValueError as exc:
            mismatches.append(
                {
                    "symbol": symbol,
                    "check": "chan_points",
                    "reason": "future_column",
                    "detail": str(exc),
                }
            )
            continue

        dates = [_as_date(value) for value in frame["date"]]
        for cut in _cuts(len(frame), cuts_per_symbol, rng):
            cut_date = dates[cut - 1]
            checked += 1
            expected = _normalize_confirmed(
                stored.filter(pl.col("known_date") <= cut_date), cut_date
            )
            truncated, _gone, _stats = replay_symbol(
                symbol, frame.iloc[:cut], snapshot, engine_version
            )
            mismatches.extend(_compare_chan(symbol, cut, cut_date, expected, truncated))

    return {"checked": checked, "mismatches": mismatches}


def wyckoff_truncation_check(
    data_dir: Path,
    symbols: list[str],
    cuts_per_symbol: int = 5,
    seed: int = 0,
) -> dict:
    """The Chan check for Wyckoff events, plus the active ranges at each cut.

    Stored events with ``known_date <= date[cut - 1]`` must equal the truncated
    replay. Ranges are compared with an ``end_date`` after the cut normalised to
    null, so a range that is still open at the cut matches its truncated form.
    """
    data_dir = Path(data_dir)
    port_version = wyckoff_port_version()
    rng = random.Random(seed)
    checked = 0
    mismatches: list[dict] = []

    for symbol in symbols:
        frame = _load_frame(data_dir, symbol, BAR_INPUT_COLUMNS)
        if frame is None:
            continue

        base = wyckoff_dir(data_dir)
        stored_events = _read_part(base / "events" / f"{symbol}.parquet")
        stored_ranges = _read_part(base / "ranges" / f"{symbol}.parquet")
        if stored_events is None or stored_ranges is None:
            mismatches.append(
                {
                    "symbol": symbol,
                    "check": "wyckoff",
                    "reason": "missing_stored_part",
                }
            )
            continue
        try:
            assert_no_future_columns(stored_events)
            assert_no_future_columns(stored_ranges)
        except ValueError as exc:
            mismatches.append(
                {
                    "symbol": symbol,
                    "check": "wyckoff",
                    "reason": "future_column",
                    "detail": str(exc),
                }
            )
            continue

        dates = [_as_date(value) for value in frame["date"]]
        for cut in _cuts(len(frame), cuts_per_symbol, rng):
            cut_date = dates[cut - 1]
            checked += 1
            events, ranges = wyckoff_symbol(symbol, frame.iloc[:cut], port_version)

            expected_events = stored_events.filter(pl.col("known_date") <= cut_date)
            mismatches.extend(
                _compare_rows(
                    "wyckoff_events",
                    symbol,
                    cut,
                    cut_date,
                    expected_events,
                    events,
                    EVENT_COLUMNS,
                )
            )

            expected_ranges = _normalize_end_date(
                stored_ranges.filter(pl.col("known_date") <= cut_date), cut_date
            )
            actual_ranges = _normalize_end_date(ranges, cut_date)
            mismatches.extend(
                _compare_rows(
                    "wyckoff_ranges",
                    symbol,
                    cut,
                    cut_date,
                    expected_ranges,
                    actual_ranges,
                    RANGE_COLUMNS,
                )
            )

    return {"checked": checked, "mismatches": mismatches}


def phantom_rate(points: pl.DataFrame, gone: pl.DataFrame) -> pl.DataFrame:
    """Per point type: first-seen rows, phantoms and the phantom rate.

    A phantom is a gone row whose ``point_dt`` is on or after that day's
    ``window_start``: the point disappeared while still inside the visible
    stroke window, i.e. the engine redrew it away (a repaint). Gone rows older
    than the window only scrolled out of view and are not phantoms.
    """
    assert_no_future_columns(points)

    first_seen: Counter = Counter()
    if "type" in points.columns:
        first_seen.update(points["type"].to_list())

    phantoms: Counter = Counter()
    if gone.height:
        flagged = gone.filter(
            pl.col("point_dt").is_not_null()
            & pl.col("window_start").is_not_null()
            & (pl.col("point_dt") >= pl.col("window_start"))
        )
        phantoms.update(flagged["type"].to_list())

    types = sorted(set(first_seen) | set(phantoms))
    seen_counts = [first_seen.get(point_type, 0) for point_type in types]
    phantom_counts = [phantoms.get(point_type, 0) for point_type in types]
    rates = [
        (phantom_counts[i] / seen_counts[i]) if seen_counts[i] else 0.0 for i in range(len(types))
    ]

    return pl.DataFrame(
        {
            "type": types,
            "first_seen": seen_counts,
            "phantoms": phantom_counts,
            "rate": rates,
        },
        schema={
            "type": pl.String,
            "first_seen": pl.Int64,
            "phantoms": pl.Int64,
            "rate": pl.Float64,
        },
    )


def _stage_bars(data_dir: Path, target_dir: Path, symbols: list[str]) -> None:
    """Write the needed bars into ``target_dir``'s enriched layout."""
    bars = load_bars(Path(data_dir), symbols=symbols)
    if bars.height == 0:
        return
    path = target_dir / "kline_daily_enriched" / "date=1970-01-01"
    path.mkdir(parents=True, exist_ok=True)
    bars.write_parquet(path / "part.parquet")


def determinism_check(data_dir: Path, symbols: list[str]) -> dict:
    """Replay into a temporary directory and compare bytes with the stored parts.

    Only parts that already exist for ``symbols`` are compared, so a data dir
    with just Chan parts (or just Wyckoff parts) is checked for what it holds.
    """
    data_dir = Path(data_dir)
    chan_base = chan_dir(data_dir)
    wyckoff_base = wyckoff_dir(data_dir)

    chan_symbols = [
        symbol for symbol in symbols if (chan_base / "points" / f"{symbol}.parquet").exists()
    ]
    wyckoff_symbols = [
        symbol for symbol in symbols if (wyckoff_base / "events" / f"{symbol}.parquet").exists()
    ]

    stored: list[tuple[Path, Path]] = []
    for symbol in chan_symbols:
        for kind in ("points", "gone"):
            path = chan_base / kind / f"{symbol}.parquet"
            if path.exists():
                stored.append((path, path.relative_to(data_dir)))
    for symbol in wyckoff_symbols:
        for kind in ("events", "ranges"):
            path = wyckoff_base / kind / f"{symbol}.parquet"
            if path.exists():
                stored.append((path, path.relative_to(data_dir)))

    if not stored:
        return {"identical": 0, "different": []}

    replay_symbols = sorted(set(chan_symbols) | set(wyckoff_symbols))
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp_dir = Path(tmp_name)
        _stage_bars(data_dir, tmp_dir, replay_symbols)
        if chan_symbols:
            run_chan_replay(tmp_dir, chan_symbols, workers=1)
        if wyckoff_symbols:
            run_wyckoff_replay(tmp_dir, wyckoff_symbols, workers=1)

        identical = 0
        different: list[str] = []
        for path, relative in stored:
            fresh = tmp_dir / relative
            if fresh.exists() and path.read_bytes() == fresh.read_bytes():
                identical += 1
            else:
                different.append(relative.as_posix())

    return {"identical": identical, "different": sorted(different)}
