"""Per-day status records (``days.jsonl``) and the missing-day check.

The report treats a trading day with no line for a strategy as an error. A day
can carry several statuses (for example ``missed_open`` and ``logic_mismatch``),
so the file keeps one line per (strategy, date, status); recording the same
status again replaces its reason instead of appending a duplicate.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

from app.forward.config import forward_dir
from app.market_clock import us_clock

STATUSES: tuple[str, ...] = (
    "ok",
    "no_plan",
    "missed_open",
    "exit_delayed",
    "exit_incomplete",
    "stop_missed",
    "no_open",
    "gapped_through",
    "logic_mismatch",
    "data_revision",
    "reconcile_mismatch",
    "replay_mismatch",
)

def _path(data_dir: Path) -> Path:
    return forward_dir(data_dir) / "days.jsonl"

def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON") from exc
    return rows

def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise

def record(data_dir: Path, short: str, d: date, status: str, reason: str = "") -> None:
    """Record one strategy's status for one trading day.

    ``status`` must be one of :data:`STATUSES`; ``reason`` is free text for the
    report. An existing line for the same strategy, date and status is replaced;
    other statuses of that day are kept.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown forward day status: {status!r}")
    day = d.isoformat()
    path = _path(data_dir)
    rows = [
        row
        for row in _read_rows(path)
        if not (
            row.get("short") == short
            and row.get("date") == day
            and row.get("status") == status
        )
    ]
    rows.append({"date": day, "short": short, "status": status, "reason": reason})
    _write_rows(path, rows)

def statuses_on(data_dir: Path, short: str, d: date) -> dict[str, str]:
    """``{status: reason}`` recorded for one strategy and day."""
    day = d.isoformat()
    return {
        str(row.get("status")): str(row.get("reason", ""))
        for row in _read_rows(_path(data_dir))
        if row.get("short") == short and row.get("date") == day
    }


def missing_days(data_dir: Path, short: str, start: date, end: date) -> list[date]:
    """Trading days in ``[start, end]`` with no record for ``short``."""
    recorded = {
        row.get("date")
        for row in _read_rows(_path(data_dir))
        if row.get("short") == short
    }
    return [
        day
        for day in us_clock.trading_days_between(start, end)
        if day.isoformat() not in recorded
    ]

__all__ = ["STATUSES", "missing_days", "record", "statuses_on"]