"""Point-in-time point extension for the forward runner (N2).

The research replay modules are pinned. This module extends their output for
one symbol-day at a time without copying the engine:

* **Chan** is incremental. :func:`app.research.chan_replay.replay_symbol` is a
  day-by-day loop, so a symbol is seeded with the pinned function and its
  ``seen`` / ``previous_ids`` / emitted rows are persisted; later days call the
  pinned ``_real_snapshot`` and mirror the pinned loop body exactly. A symbol
  with no state, or whose bars up to the last processed day changed, is seeded
  again from scratch through ``t``.
* **Wyckoff** is a stateless batch replay, so it is re-run in full with the
  pinned ``wyckoff_symbol`` on every call.

The hard invariant is that extending day by day produces frames that are
``pl.DataFrame.equals`` to one fresh replay through the final day, for points
*and* gone rows (see ``tests/forward/test_signals.py``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import polars as pl

from app.forward.data import changed_symbols, history_hashes, load_forward_bars
from app.research.chan_replay import (
    _GONE_SCHEMA,
    _POINT_SCHEMA,
    ENGINE_COLUMNS,
    GONE_COLUMNS,
    POINT_COLUMNS,
    _as_date,
    _daily_level,
    _earliest_start,
    _legs,
    _real_snapshot,
    replay_symbol,
)
from app.research.chan_replay import (
    _frame as _chan_frame,
)
from app.research.versions import chan_engine_version, wyckoff_port_version
from app.research.wyckoff.replay import (
    _EVENT_SCHEMA,
    _RANGE_SCHEMA,
    BAR_INPUT_COLUMNS,
    EVENT_COLUMNS,
    RANGE_COLUMNS,
    wyckoff_symbol,
)
from app.research.wyckoff.replay import (
    _frame as _wyckoff_frame,
)

logger = logging.getLogger(__name__)

# ``replay_symbol``'s default; the first day it may emit anything.
_START_INDEX = 119
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()

def forward_points_dir(data_dir: Path) -> Path:
    """``<data_dir>/forward/points`` (separate from the research store)."""
    return Path(data_dir) / "forward" / "points"

# ---------------------------------------------------------------------------
# State serialisation
# ---------------------------------------------------------------------------

def _encode(value):
    """JSON-safe copy, keeping dates distinguishable from strings."""
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value

def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"__date__"}:
            return date.fromisoformat(value["__date__"])
        if set(value) == {"__datetime__"}:
            return datetime.fromisoformat(value["__datetime__"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value

def _state_path(data_dir: Path, symbol: str) -> Path:
    return forward_points_dir(data_dir) / "state" / f"{symbol}.json"

def _write_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir, state["symbol"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": state["symbol"],
        "engine_version": state["engine_version"],
        "through": state["through"].isoformat(),
        "bars_seen": state["bars_seen"],
        "history_hash": state["history_hash"],
        "stats": state["stats"],
        "point_rows": [_encode(row) for row in state["point_rows"]],
        "gone_rows": [_encode(row) for row in state["gone_rows"]],
        "seen": [_encode(list(identity)) for identity in state["seen"]],
        "previous_ids": [_encode(list(identity)) for identity in state["previous_ids"]],
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()

def _read_state(data_dir: Path, symbol: str) -> dict | None:
    path = _state_path(data_dir, symbol)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    point_rows = [_decode(row) for row in raw["point_rows"]]
    gone_rows = [_decode(row) for row in raw["gone_rows"]]
    by_identity = {(row["type"], row["point_dt"]): row for row in point_rows}
    seen: dict[tuple, dict] = {}
    for encoded in raw.get("seen", []):
        key = tuple(_decode(encoded))
        row = by_identity.get(key)
        if row is not None:
            seen[key] = row
    # Defensive: every emitted row is also a seen identity.
    for key, row in by_identity.items():
        seen.setdefault(key, row)
    return {
        "symbol": raw["symbol"],
        "engine_version": raw["engine_version"],
        "through": date.fromisoformat(raw["through"]),
        "bars_seen": int(raw["bars_seen"]),
        "history_hash": raw["history_hash"],
        "stats": _decode(raw["stats"]),
        "point_rows": point_rows,
        "gone_rows": gone_rows,
        "seen": seen,
        "previous_ids": {tuple(_decode(item)) for item in raw.get("previous_ids", [])},
    }

def _state_frames(state: dict) -> tuple[pl.DataFrame, pl.DataFrame]:
    return (
        _chan_frame(state["point_rows"], POINT_COLUMNS, _POINT_SCHEMA),
        _chan_frame(state["gone_rows"], GONE_COLUMNS, _GONE_SCHEMA),
    )

# ---------------------------------------------------------------------------
# Chan: seed with the pinned replay, then mirror its loop body per day
# ---------------------------------------------------------------------------

def _last_available_ids(
    frame: pd.DataFrame, snapshot_fn
) -> set[tuple]:
    """Identities in the most recent usable snapshot, as ``replay_symbol`` leaves them."""
    for t in range(len(frame) - 1, _START_INDEX - 1, -1):
        snapshot = _daily_level(snapshot_fn(frame.iloc[: t + 1]))
        if snapshot.get("status") != "available":
            continue
        identities: set[tuple] = set()
        for point in snapshot.get("buy_sell_points") or []:
            identities.add((point.get("type"), _as_date(point.get("dt"))))
        return identities
    return set()

def _hash_through(symbol: str, bars: pl.DataFrame, through: date) -> str:
    """Per-symbol hash of the bars dated on or before ``through``."""
    if bars.is_empty():
        return _EMPTY_HASH
    hashes = history_hashes(bars, before=through + timedelta(days=1))
    return hashes.get(symbol, _EMPTY_HASH)

def _bars_through(bars: pl.DataFrame, through: date) -> int:
    return bars.filter(pl.col("date") <= pl.lit(through)).height

def _seed_state(
    symbol: str,
    frame: pd.DataFrame,
    snapshot_fn,
    engine_version: str,
    through: date,
    history_hash: str,
) -> dict:
    points, gone, stats = replay_symbol(
        symbol, frame, snapshot_fn, engine_version
    )
    point_rows = points.to_dicts()
    gone_rows = gone.to_dicts()
    seen = {(row["type"], row["point_dt"]): row for row in point_rows}
    return {
        "symbol": symbol,
        "engine_version": engine_version,
        "through": through,
        "bars_seen": len(frame),
        "history_hash": history_hash,
        "stats": stats,
        "point_rows": point_rows,
        "gone_rows": gone_rows,
        "seen": seen,
        "previous_ids": _last_available_ids(frame, snapshot_fn),
    }

def _advance_state(
    state: dict, frame: pd.DataFrame, start: int, snapshot_fn
) -> None:
    """The body of ``replay_symbol``'s loop, for bars ``start..len(frame)-1``."""
    point_rows = state["point_rows"]
    gone_rows = state["gone_rows"]
    seen = state["seen"]
    previous_ids = state["previous_ids"]
    stats = state["stats"]
    symbol = state["symbol"]
    engine_version = state["engine_version"]
    dates = frame["date"]

    for t in range(start, len(frame)):
        stats["days"] += 1
        snapshot = _daily_level(snapshot_fn(frame.iloc[: t + 1]))
        status = snapshot.get("status")
        if status == "error":
            stats["error_days"] += 1
        if status != "available":
            # A non-usable day produces no rows and does not become the
            # reference day for disappearance detection.
            continue
        stats["available_days"] += 1

        current_date = _as_date(dates.iloc[t])
        recent_bis = snapshot.get("recent_bis") or []
        current_points = snapshot.get("buy_sell_points") or []
        current_ids: set[tuple] = set()

        for point in current_points:
            point_dt = _as_date(point.get("dt"))
            identity = (point.get("type"), point_dt)
            current_ids.add(identity)

            row = seen.get(identity)
            if row is None:
                prev_start, prev_end, last_start, last_end = _legs(
                    point_dt, recent_bis
                )
                point_state = point.get("state")
                row = {
                    "symbol": symbol,
                    "known_date": current_date,
                    "type": point.get("type"),
                    "side": point.get("side"),
                    "point_dt": point_dt,
                    "price": point.get("price"),
                    "invalidation_price": point.get("invalidation_price"),
                    "basis": point.get("basis"),
                    "state_at_known": point_state,
                    "confirmed_date": (
                        current_date if point_state == "confirmed" else None
                    ),
                    "leg_prev_start": prev_start,
                    "leg_prev_end": prev_end,
                    "leg_last_start": last_start,
                    "leg_last_end": last_end,
                    "engine_version": engine_version,
                }
                seen[identity] = row
                point_rows.append(row)
            elif row["confirmed_date"] is None and point.get("state") == "confirmed":
                row["confirmed_date"] = current_date

        window_start = _earliest_start(recent_bis)
        vanished = sorted(
            previous_ids - current_ids,
            key=lambda key: (str(key[0]), str(key[1])),
        )
        for identity in vanished:
            row = seen[identity]
            gone_rows.append(
                {
                    "symbol": symbol,
                    "type": identity[0],
                    "point_dt": row["point_dt"],
                    "first_known_date": row["known_date"],
                    "gone_date": current_date,
                    "window_start": window_start,
                }
            )

        previous_ids = current_ids

    state["previous_ids"] = previous_ids

def _atomic_write_parquet(frame: pl.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        frame.write_parquet(tmp, compression="zstd", statistics=False)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()

def _extend_chan(
    data_dir: Path,
    symbol: str,
    bars: pl.DataFrame,
    snapshot_fn,
    engine_version: str,
) -> str:
    """Bring one symbol's Chan state through ``bars``; return the mode used."""
    frame = bars.to_pandas().loc[:, list(ENGINE_COLUMNS)]
    through = _as_date(frame["date"].iloc[-1])
    new_hash = _hash_through(symbol, bars, through)

    state = _read_state(data_dir, symbol)
    history_changed = (
        []
        if state is None
        else changed_symbols(
            {symbol: state["history_hash"]},
            {symbol: _hash_through(symbol, bars, state["through"])},
        )
    )
    if (
        state is not None
        and state["engine_version"] == engine_version
        and state["through"] <= through
        and state["bars_seen"] == _bars_through(bars, state["through"])
        and not history_changed
    ):
        start = max(state["bars_seen"], _START_INDEX)
        if start < len(frame):
            _advance_state(state, frame, start, snapshot_fn)
        state["through"] = through
        state["bars_seen"] = len(frame)
        state["history_hash"] = new_hash
        mode = "extended"
    else:
        state = _seed_state(
            symbol, frame, snapshot_fn, engine_version, through, new_hash
        )
        mode = "replayed"

    points, _gone = _state_frames(state)
    _atomic_write_parquet(
        points, forward_points_dir(data_dir) / "chan" / f"{symbol}.parquet"
    )
    _write_state(data_dir, state)
    return mode

# ---------------------------------------------------------------------------
# Wyckoff: nightly full replay with the pinned function
# ---------------------------------------------------------------------------

def _replay_wyckoff(
    data_dir: Path, symbol: str, bars: pl.DataFrame, port_version: str
) -> None:
    frame = bars.to_pandas().loc[:, list(BAR_INPUT_COLUMNS)]
    events, ranges = wyckoff_symbol(symbol, frame, port_version)
    base = forward_points_dir(data_dir)
    _atomic_write_parquet(
        ranges, base / "wyckoff_ranges" / f"{symbol}.parquet"
    )
    _atomic_write_parquet(
        events, base / "wyckoff_events" / f"{symbol}.parquet"
    )

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extend_points(
    data_dir: Path,
    symbols: list[str],
    t: date,
    *,
    snapshot_fn=None,
    workers: int = 1,
) -> dict:
    """Bring every symbol's forwarded points to what a fresh replay through t yields.

    ``workers`` is accepted for the caller's interface; symbols are processed
    sequentially so an injected (possibly unpicklable) ``snapshot_fn`` works and
    each symbol's state advances atomically.
    """
    data_dir = Path(data_dir)
    snapshot_fn = snapshot_fn if snapshot_fn is not None else _real_snapshot
    engine_version = chan_engine_version()
    port_version = wyckoff_port_version()
    bars = load_forward_bars(data_dir, symbols=list(symbols), end=t)

    result: dict[str, list[str]] = {
        "extended": [],
        "replayed": [],
        "failed": [],
    }
    for symbol in sorted(set(symbols)):
        symbol_bars = bars.filter(pl.col("symbol") == symbol).sort("date")
        if symbol_bars.is_empty():
            logger.warning("point extension has no bars for %s through %s", symbol, t)
            result["failed"].append(symbol)
            continue
        try:
            mode = _extend_chan(
                data_dir, symbol, symbol_bars, snapshot_fn, engine_version
            )
            _replay_wyckoff(data_dir, symbol, symbol_bars, port_version)
        except Exception:
            logger.exception("point extension failed for %s", symbol)
            result["failed"].append(symbol)
            continue
        result[mode].append(symbol)
    return result

def _concat_parts(
    directory: Path,
    columns: tuple[str, ...],
    schema: dict,
    empty_frame,
) -> pl.DataFrame:
    files = sorted(directory.glob("*.parquet")) if directory.is_dir() else []
    frames = [pl.read_parquet(path) for path in files]
    if not frames:
        return empty_frame([], columns, schema)

    combined = pl.concat(frames, how="diagonal_relaxed")
    for column in columns:
        if column not in combined.columns:
            combined = combined.with_columns(
                pl.lit(None, dtype=schema[column]).alias(column)
            )
    return combined.select(list(columns))

def load_points_through(
    data_dir: Path, t: date
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Stored Chan points, Wyckoff events and Wyckoff ranges known by ``t``."""
    base = forward_points_dir(data_dir)
    chan = _concat_parts(
        base / "chan", POINT_COLUMNS, _POINT_SCHEMA, _chan_frame
    ).filter(pl.col("known_date") <= pl.lit(t))
    events = _concat_parts(
        base / "wyckoff_events", EVENT_COLUMNS, _EVENT_SCHEMA, _wyckoff_frame
    ).filter(pl.col("known_date") <= pl.lit(t))
    ranges = _concat_parts(
        base / "wyckoff_ranges", RANGE_COLUMNS, _RANGE_SCHEMA, _wyckoff_frame
    ).filter(pl.col("known_date") <= pl.lit(t))
    return (
        chan.sort(["symbol", "known_date", "type", "point_dt"]),
        events.sort(["symbol", "known_date", "kind"]),
        ranges.sort(["symbol", "known_date", "range_kind"]),
    )

def _read_part(
    path: Path, columns: tuple[str, ...], schema: dict, chan: bool
) -> pl.DataFrame:
    if not path.exists():
        if chan:
            return _chan_frame([], columns, schema)
        return _wyckoff_frame([], columns, schema)
    return pl.read_parquet(path).select(list(columns))

def load_stored_chan(
    data_dir: Path, symbol: str
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """The stored Chan points and gone rows for one symbol."""
    base = forward_points_dir(data_dir)
    points = _read_part(
        base / "chan" / f"{symbol}.parquet", POINT_COLUMNS, _POINT_SCHEMA, True
    )
    state = _read_state(data_dir, symbol)
    if state is None:
        gone = _chan_frame([], GONE_COLUMNS, _GONE_SCHEMA)
    else:
        gone = _chan_frame(state["gone_rows"], GONE_COLUMNS, _GONE_SCHEMA)
    return points, gone

def verify_sample(
    data_dir: Path,
    symbols: list[str],
    t: date,
    *,
    snapshot_fn=None,
) -> list[str]:
    """Fresh replay vs stored rows; return the symbols that differ."""
    data_dir = Path(data_dir)
    snapshot_fn = snapshot_fn if snapshot_fn is not None else _real_snapshot
    engine_version = chan_engine_version()
    port_version = wyckoff_port_version()
    bars = load_forward_bars(data_dir, symbols=list(symbols), end=t)
    base = forward_points_dir(data_dir)

    different: list[str] = []
    for symbol in sorted(set(symbols)):
        symbol_bars = bars.filter(pl.col("symbol") == symbol).sort("date")
        if symbol_bars.is_empty():
            different.append(symbol)
            continue
        pandas_frame = symbol_bars.to_pandas()
        chan_frame = pandas_frame.loc[:, list(ENGINE_COLUMNS)]
        wyckoff_frame = pandas_frame.loc[:, list(BAR_INPUT_COLUMNS)]

        fresh_points, fresh_gone, _ = replay_symbol(
            symbol, chan_frame, snapshot_fn, engine_version
        )
        fresh_events, fresh_ranges = wyckoff_symbol(
            symbol, wyckoff_frame, port_version
        )
        stored_points, stored_gone = load_stored_chan(data_dir, symbol)
        stored_events = _read_part(
            base / "wyckoff_events" / f"{symbol}.parquet",
            EVENT_COLUMNS,
            _EVENT_SCHEMA,
            False,
        )
        stored_ranges = _read_part(
            base / "wyckoff_ranges" / f"{symbol}.parquet",
            RANGE_COLUMNS,
            _RANGE_SCHEMA,
            False,
        )

        if not (
            fresh_points.equals(stored_points)
            and fresh_gone.equals(stored_gone)
            and fresh_events.equals(stored_events)
            and fresh_ranges.equals(stored_ranges)
        ):
            different.append(symbol)
    return different