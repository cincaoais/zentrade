"""Daemon timeline and parallel seeding, entirely on fakes.

The daemon gets a fake ET clock, fake broker/quotes factories and monkeypatched
steps, so no OpenD or network is touched and no real data dir is read.
"""

from __future__ import annotations

import json

# Import the pool module before any test patches multiprocessing.get_context:
# multiprocessing.pool binds get_context at import time, so a first import
# while patched would keep the patch for the rest of the session.
import multiprocessing.pool  # noqa: F401
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.forward import daemon, signals
from app.forward.config import LEADS

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 18)  # Wednesday


class StopLoop(KeyboardInterrupt):
    """Ends the daemon's infinite loop after the first simulated day."""


class FakeUSClock:
    tz = ET

    def is_trading_day(self, d: date | None = None) -> bool:
        return (d or DAY).weekday() < 5

    def next_trading_day(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt

    def previous_trading_day(self, d: date) -> date:
        prev = d - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
        return prev

    def trading_days_between(self, start: date, end: date) -> list[date]:
        days: list[date] = []
        cursor = start
        while cursor <= end:
            if self.is_trading_day(cursor):
                days.append(cursor)
            cursor += timedelta(days=1)
        return days


class DaemonClock:
    """ET wall clock that stops the loop when a sleep crosses ``stop_at``."""

    def __init__(self, start: datetime, stop_at: datetime | None = None) -> None:
        self._now = start
        self._mono = 1_000.0
        self.stop_at = stop_at
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        target = self._now + timedelta(seconds=seconds)
        self.sleeps.append(seconds)
        self._mono += seconds
        if self.stop_at is not None and target > self.stop_at:
            self._now = self.stop_at
            raise StopLoop
        self._now = target


class FakeBroker:
    def __init__(self) -> None:
        self.account_id = 123456

    def positions(self) -> dict[str, int]:
        return {}

    def cash(self) -> float:
        return 250_000.0


class FakeQuotes:
    def snapshot(self, symbols: list[str]) -> dict[str, dict]:
        return {}


def _write_registration(data: Path) -> None:
    path = data / "forward" / "registration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "account_id": 123456,
        "capital_per_strategy": 250_000.0,
        "start_date": "2026-11-16",
        "strategies": {
            lead: {"backtest": {"initial_capital": 250_000.0}}
            for lead in LEADS
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _fake_stopwatch(order: list[str], seen: dict[str, object]):
    class FakeStopWatch:
        def __init__(self, books, quotes, executor, engine_entry_prices, *, clock, **kwargs):
            self.clock = clock
            seen["stopwatch_kwargs"] = dict(kwargs)

        def run_until(self, end_time: datetime) -> None:
            order.append("stopwatch")
            remaining = (end_time - self.clock.now()).total_seconds()
            if remaining > 0:
                self.clock.sleep(remaining)

    return FakeStopWatch


def _drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    morning_error: bool = False,
    stopwatch: type | None = None,
) -> tuple[list[str], dict[str, object]]:
    data = tmp_path / "data"
    data.mkdir()
    _write_registration(data)
    order: list[str] = []
    seen: dict[str, object] = {}
    clock = DaemonClock(
        datetime(DAY.year, DAY.month, DAY.day, 9, 29, tzinfo=ET),
        stop_at=datetime(DAY.year, DAY.month, DAY.day, 23, 0, tzinfo=ET),
    )
    calendar = FakeUSClock()
    monkeypatch.setattr(daemon, "us_clock", calendar)
    monkeypatch.setattr(daemon, "StopWatch", stopwatch or _fake_stopwatch(order, seen))
    monkeypatch.setattr(daemon, "engine_entry_prices", lambda data_dir, short: {})

    def fake_morning(*args, **kwargs):
        order.append("morning")
        seen["morning_kwargs"] = dict(kwargs)
        if morning_error:
            raise RuntimeError("morning exploded")
        return {}

    def fake_reconcile(books, positions, cash, adj):
        order.append("reconcile")
        seen["reconcile_adj"] = adj
        return {"ok": True, "mismatches": [], "scaled": [], "frozen_symbols": []}

    def fake_night(data_dir, t, *, repo_root, workers, **kwargs):
        order.append("night")
        seen["night_date"] = t
        seen["night_workers"] = workers
        return {"ready": True, "date": t.isoformat()}

    monkeypatch.setattr(daemon, "run_morning", fake_morning)
    monkeypatch.setattr(daemon, "reconcile", fake_reconcile)
    monkeypatch.setattr(daemon, "run_night", fake_night)

    result = daemon.run(
        data,
        dry_run=False,
        repo_root=tmp_path / "repo",
        clock=clock,
        broker_factory=lambda account_id: FakeBroker(),
        quotes_factory=lambda: FakeQuotes(),
    )
    seen["result"] = result
    seen["data"] = data
    return order, seen


def test_daemon_runs_steps_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, seen = _drive(tmp_path, monkeypatch)

    assert order == ["morning", "stopwatch", "reconcile", "night"]
    assert seen["reconcile_adj"] == {}
    assert seen["night_date"] == DAY
    assert seen["night_workers"] >= 1
    assert seen["stopwatch_kwargs"]["dry_run"] is False

    audit = seen["data"] / "forward" / "audit.jsonl"
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    steps = [event.get("step") for event in events if event.get("event") == "step_ok"]
    assert steps == ["morning", "stopwatch", "reconcile", "night"]
    assert any(event.get("event") == "start" for event in events)
    assert any(event.get("event") == "stop" for event in events)


def test_daemon_step_error_is_audited_and_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, seen = _drive(tmp_path, monkeypatch, morning_error=True)

    assert order == ["morning", "stopwatch", "reconcile", "night"]
    data = seen["data"]
    audit = data / "forward" / "audit.jsonl"
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    errors = [event for event in events if event.get("event") == "step_error"]
    assert errors and errors[0]["step"] == "morning"
    assert "morning exploded" in str(errors[0]["error"])

    records = [
        json.loads(line)
        for line in (data / "forward" / "days.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["short"] for row in records} == set(LEADS.values())
    assert all(row["status"] == "no_plan" for row in records)


# ── parallel seeding ────────────────────────────────────────────
_START = date(2024, 1, 1)


def _bars(n: int = 130) -> list[dict]:
    rows: list[dict] = []
    for index in range(n):
        day = _START + timedelta(days=index)
        close = 10.0 + 0.01 * index
        rows.append(
            {
                "symbol": "AAA",
                "date": day,
                "open": close,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 1_000_000.0,
                "amount": 10_000_000.0,
                "raw_close": close,
            }
        )
    return rows


def _write_bars(root: Path, rows: list[dict]) -> None:
    path = root / "kline_daily_enriched" / "date=2024-01-01"
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path / "part.parquet")


def _fake_snapshot(frame) -> dict:
    """Module-level (picklable) fake for the research snapshot function.

    Emits points that appear every 5 bars, get confirmed 5 bars later and drop
    out after 15, so the parallel and sequential paths compare real rows.
    """
    n = len(frame)
    points = []
    for born in range(120, n + 1, 5):
        age = n - born
        if age > 15:
            continue
        points.append(
            {
                "type": "2B",
                "side": "buy",
                "dt": frame["date"].iloc[born - 3],
                "price": float(frame["close"].iloc[born - 1]),
                "invalidation_price": float(frame["low"].iloc[born - 3]),
                "basis": "fake",
                "state": "confirmed" if age >= 5 else "pending",
            }
        )
    return {"status": "available", "buy_sell_points": points, "recent_bis": []}


def test_parallel_seed_equals_sequential(tmp_path: Path, monkeypatch) -> None:
    pools: list[str] = []
    real_get_context = signals.mp.get_context

    def recording_get_context(method=None):
        pools.append(method)
        return real_get_context(method)

    monkeypatch.setattr(signals.mp, "get_context", recording_get_context)
    rows = _bars()
    through = max(row["date"] for row in rows)
    roots = []
    for name in ("sequential", "parallel"):
        root = tmp_path / name
        root.mkdir()
        _write_bars(root, rows)
        roots.append(root)

    sequential = signals.extend_points(
        roots[0], ["AAA"], through, snapshot_fn=_fake_snapshot, workers=1
    )
    parallel = signals.extend_points(
        roots[1], ["AAA"], through, snapshot_fn=_fake_snapshot, workers=2
    )

    assert sequential["failed"] == [] and parallel["failed"] == []
    assert pools == ["spawn"]  # the parallel run really used the pool
    left = signals.load_points_through(roots[0], through)
    right = signals.load_points_through(roots[1], through)
    assert left[0].height > 0  # real Chan rows were compared, not two empty frames
    for a, b in zip(left, right, strict=True):
        assert a.equals(b)
    assert signals.load_stored_chan(roots[0], "AAA")[0].equals(
        signals.load_stored_chan(roots[1], "AAA")[0]
    )


def test_sleep_until_crosses_spring_forward() -> None:
    class SpringForwardClock:
        def __init__(self, start_dt: datetime) -> None:
            self.tz = start_dt.tzinfo
            self.start_utc = start_dt.timestamp()
            self.elapsed = 0.0
            self.sleeps: list[float] = []

        def now(self) -> datetime:
            utc_dt = datetime.fromtimestamp(self.start_utc + self.elapsed, tz=UTC)
            return utc_dt.astimezone(self.tz)

        def sleep(self, s: float) -> None:
            self.sleeps.append(s)
            self.elapsed += s

    start = datetime(2027, 3, 12, 19, 0, tzinfo=ET)
    target = daemon._at(date(2027, 3, 15), daemon.WAKE_BEFORE_OPEN, ET)
    clock = SpringForwardClock(start)

    daemon._sleep_until(clock, target)

    end_now = clock.now()
    assert end_now.date() == date(2027, 3, 15)
    assert end_now.hour == 9
    assert end_now.minute == 25
    assert len(clock.sleeps) > 0
    assert all(s <= 300.0 for s in clock.sleeps)


def test_night_retries_until_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _write_registration(data)
    calls: list[datetime] = []
    clock = DaemonClock(
        datetime(DAY.year, DAY.month, DAY.day, 9, 29, tzinfo=ET),
        stop_at=datetime(DAY.year, DAY.month, DAY.day, 23, 0, tzinfo=ET),
    )
    monkeypatch.setattr(daemon, "us_clock", FakeUSClock())
    monkeypatch.setattr(daemon, "StopWatch", _fake_stopwatch([], {}))
    monkeypatch.setattr(daemon, "engine_entry_prices", lambda data_dir, short: {})
    monkeypatch.setattr(daemon, "run_morning", lambda *a, **kw: {})
    monkeypatch.setattr(daemon, "reconcile", lambda *a, **kw: {"ok": True})

    def fake_night(data_dir, t, *, repo_root, workers, **kwargs):
        calls.append(clock.now())
        if len(calls) < 3:
            return {"ready": False, "date": t.isoformat()}
        return {"ready": True, "date": t.isoformat()}

    monkeypatch.setattr(daemon, "run_night", fake_night)

    daemon.run(
        data,
        dry_run=False,
        repo_root=tmp_path / "repo",
        clock=clock,
        broker_factory=lambda account_id: FakeBroker(),
        quotes_factory=lambda: FakeQuotes(),
    )

    assert len(calls) == 3
    assert (calls[1] - calls[0]).total_seconds() == 1800.0
    assert (calls[2] - calls[1]).total_seconds() == 1800.0


def test_night_retry_stops_at_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _write_registration(data)
    calls: list[datetime] = []
    clock = DaemonClock(
        datetime(DAY.year, DAY.month, DAY.day, 9, 29, tzinfo=ET),
        stop_at=datetime(DAY.year, DAY.month, DAY.day + 1, 8, 0, tzinfo=ET),
    )
    monkeypatch.setattr(daemon, "us_clock", FakeUSClock())
    monkeypatch.setattr(daemon, "StopWatch", _fake_stopwatch([], {}))
    monkeypatch.setattr(daemon, "engine_entry_prices", lambda data_dir, short: {})
    monkeypatch.setattr(daemon, "run_morning", lambda *a, **kw: {})
    monkeypatch.setattr(daemon, "reconcile", lambda *a, **kw: {"ok": True})

    def fake_night(data_dir, t, *, repo_root, workers, **kwargs):
        calls.append(clock.now())
        return {"ready": False, "date": t.isoformat()}

    monkeypatch.setattr(daemon, "run_night", fake_night)

    daemon.run(
        data,
        dry_run=False,
        repo_root=tmp_path / "repo",
        clock=clock,
        broker_factory=lambda account_id: FakeBroker(),
        quotes_factory=lambda: FakeQuotes(),
    )

    deadline = datetime(DAY.year, DAY.month, DAY.day + 1, 7, 0, tzinfo=ET)
    assert len(calls) > 1
    assert calls[-1] < deadline
    assert all(c < deadline for c in calls)



def test_daemon_refuses_a_non_simulate_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.forward.broker import NotSimulate

    data = tmp_path / "data"
    data.mkdir()
    _write_registration(data)
    clock = DaemonClock(
        datetime(DAY.year, DAY.month, DAY.day, 9, 29, tzinfo=ET),
        stop_at=datetime(DAY.year, DAY.month, DAY.day, 23, 0, tzinfo=ET),
    )
    monkeypatch.setattr(daemon, "us_clock", FakeUSClock())
    ran: list[str] = []
    monkeypatch.setattr(daemon, "run_morning", lambda *a, **k: ran.append("morning"))

    class RealAccount(FakeBroker):
        def check_account(self):
            raise NotSimulate("account 123456 is REAL")

    with pytest.raises(NotSimulate):
        daemon.run(
            data,
            dry_run=True,
            repo_root=tmp_path / "repo",
            clock=clock,
            broker_factory=lambda account_id: RealAccount(),
            quotes_factory=lambda: FakeQuotes(),
        )
    assert ran == []


def test_stop_watch_errors_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.forward.days import statuses_on

    seen: dict[str, object] = {}

    class ErroringWatch:
        def __init__(self, books, quotes, executor, engine_entry_prices, *, clock, **kwargs):
            self.clock = clock
            seen["kwargs"] = dict(kwargs)
            self.errors = ["10:01 RuntimeError: quote socket closed"]

        def run_until(self, end_time: datetime) -> None:
            remaining = end_time.timestamp() - self.clock.now().timestamp()
            if remaining > 0:
                self.clock.sleep(remaining)

    _order, info = _drive(tmp_path, monkeypatch, stopwatch=ErroringWatch)

    data = info["data"]
    assert seen["kwargs"]["stop_flag"] == data / "forward" / "STOP"
    for short in LEADS.values():
        assert "quote socket closed" in statuses_on(data, short, DAY)["stop_missed"]
