"""Intraday -15% stop watch tests; no OpenD and no network."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.forward.book import LiveBook, Lot
from app.forward.broker import SimBroker
from app.forward.orders import Executor, Journal
from app.forward.stopwatch import StopWatch, stop_line
from tests.forward.fake_trade import FakeTradeContext
from tests.moomoo_fakes import FakeClock

TZ = ZoneInfo("America/New_York")
DAY = date(2026, 11, 18)
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)


class _Clock:
    """Market clock double: ``now`` advances when ``sleep`` is called."""

    def __init__(self, now: datetime) -> None:
        self._now = now
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self._now += timedelta(seconds=float(seconds))


class _Quotes:
    """Snapshot double recording the symbols asked for."""

    def __init__(self, snapshots: dict[str, dict[str, object]]) -> None:
        self.snapshots = dict(snapshots)
        self.calls: list[list[str]] = []

    def snapshot(self, symbols: list[str]) -> dict[str, dict[str, object]]:
        self.calls.append(list(symbols))
        return {s: self.snapshots[s] for s in symbols if s in self.snapshots}


def _book(
    short: str = "h2s",
    *,
    symbol: str = "AAPL",
    shares: int = 100,
    entry_date: date = date(2026, 11, 17),
    entry_price: float = 200.0,
) -> LiveBook:
    lot = Lot(
        symbol=symbol,
        shares=shares,
        entry_date=entry_date,
        entry_price=entry_price,
        key=f"fw-{short}-{entry_date:%Y%m%d}-{symbol}-B",
    )
    return LiveBook(short=short, capital=1_000_000.0, lots={symbol: lot})


def _setup(
    tmp_path: Path,
    snapshots: dict[str, dict[str, object]],
    *,
    books: list[LiveBook] | None = None,
    engine_prices: dict[str, dict[str, float]] | None = None,
    clock: _Clock | None = None,
) -> tuple[StopWatch, _Quotes, FakeTradeContext, _Clock, Journal]:
    exec_clock = FakeClock()
    journal = Journal(tmp_path / "orders.jsonl")
    ctx = FakeTradeContext(account_id=7, clock=exec_clock)
    broker = SimBroker(7, ctx_factory=lambda: ctx)
    executor = Executor(broker, journal, clock=exec_clock)
    quotes = _Quotes(snapshots)
    market_clock = clock or _Clock(NOW)
    watch = StopWatch(
        books if books is not None else [_book()],
        quotes,
        executor,
        engine_prices if engine_prices is not None else {"h2s": {"AAPL": 100.0}},
        clock=market_clock,
    )
    return watch, quotes, ctx, market_clock, journal


def _snapshot(last: float, *, age_s: float = 0.0, now: datetime = NOW) -> dict[str, object]:
    return {
        "open_price": last,
        "last_price": last,
        "update_time": now.timestamp() - age_s,
    }


def test_stop_line_is_fifteen_percent_below_entry() -> None:
    assert stop_line(100.0) == 85.0
    assert stop_line(37.5) == 37.5 * 0.85


def test_stop_triggers_once(tmp_path: Path) -> None:
    watch, _quotes, ctx, _clock, _journal = _setup(
        tmp_path, {"AAPL": _snapshot(84.9)}
    )

    records = watch.tick(NOW)

    assert len(records) == 1
    record = records[0]
    assert record["symbol"] == "AAPL"
    assert record["stop"] == 85.0
    assert record["last"] == 84.9
    assert record["key"] == "fw-h2s-20261118-AAPL-S-stop"
    places = ctx.calls_named("place_order")
    assert len(places) == 1
    assert places[0]["kwargs"]["remark"] == "fw-h2s-20261118-AAPL-S-stop"
    assert places[0]["kwargs"]["qty"] == 100

    assert watch.tick(NOW + timedelta(seconds=5)) == []
    assert len(ctx.calls_named("place_order")) == 1


def test_entry_day_skipped(tmp_path: Path) -> None:
    watch, _quotes, ctx, _clock, _journal = _setup(
        tmp_path,
        {"AAPL": _snapshot(50.0)},
        books=[_book(entry_date=DAY)],
        engine_prices={"h2s": {"AAPL": 100.0}},
    )

    assert watch.tick(NOW) == []
    assert ctx.calls_named("place_order") == []


def test_uses_engine_entry_price(tmp_path: Path) -> None:
    # The live fill was 200 (stop 170) but the engine entered at 100 (stop 85).
    watch, _quotes, ctx, _clock, _journal = _setup(
        tmp_path,
        {"AAPL": _snapshot(100.0)},
        books=[_book(entry_price=200.0)],
        engine_prices={"h2s": {"AAPL": 100.0}},
    )
    assert watch.tick(NOW) == []
    assert ctx.calls_named("place_order") == []

    watch, _quotes, ctx, _clock, _journal = _setup(
        tmp_path / "second",
        {"AAPL": _snapshot(80.0)},
        books=[_book(entry_price=200.0)],
        engine_prices={"h2s": {"AAPL": 100.0}},
    )
    records = watch.tick(NOW)
    assert len(records) == 1
    assert records[0]["stop"] == 85.0
    assert records[0]["last"] == 80.0
    assert len(ctx.calls_named("place_order")) == 1


def test_stale_quote_ignored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    watch, _quotes, ctx, _clock, _journal = _setup(
        tmp_path, {"AAPL": _snapshot(50.0, age_s=120.0)}
    )

    with caplog.at_level(logging.WARNING):
        records = watch.tick(NOW)

    assert records == []
    assert ctx.calls_named("place_order") == []
    assert any("stale" in record.getMessage().lower() for record in caplog.records)


def test_missing_engine_price_skipped(tmp_path: Path) -> None:
    watch, _quotes, ctx, _clock, _journal = _setup(
        tmp_path,
        {"AAPL": _snapshot(10.0)},
        books=[_book(entry_price=200.0)],
        engine_prices={"h2s": {}},
    )

    records = watch.tick(NOW)

    assert ctx.calls_named("place_order") == []
    assert len(records) == 1
    assert records[0]["symbol"] == "AAPL"
    assert records[0]["reason"] == "no_engine_entry_price"


def test_run_until_polls_until_end(tmp_path: Path) -> None:
    clock = _Clock(NOW)
    watch, quotes, ctx, _clock, _journal = _setup(
        tmp_path,
        {"AAPL": _snapshot(80.0)},
        clock=clock,
    )

    watch.run_until(NOW + timedelta(seconds=12))

    assert clock.now() == NOW + timedelta(seconds=12)
    assert len(ctx.calls_named("place_order")) == 1
    assert len(quotes.calls) >= 3


def test_stop_key_differs_from_failed_morning_exit(tmp_path: Path) -> None:
    # A morning exit key for the same symbol and day that ended failed must not
    # block the intraday stop: the stop uses its own key.
    watch, _quotes, ctx, _clock, journal = _setup(tmp_path, {"AAPL": _snapshot(84.0)})
    journal.append({"key": "fw-h2s-20261118-AAPL-S", "state": "submitting", "symbol": "AAPL", "side": "S", "qty": 100})
    journal.append({"key": "fw-h2s-20261118-AAPL-S", "state": "failed"})

    records = watch.tick(NOW)

    assert [r["key"] for r in records] == ["fw-h2s-20261118-AAPL-S-stop"]
    assert len(ctx.calls_named("place_order")) == 1


def test_missing_engine_price_recorded_once_per_day(tmp_path: Path) -> None:
    watch, _quotes, _ctx, _clock, _journal = _setup(
        tmp_path, {"AAPL": _snapshot(50.0)}, engine_prices={}
    )
    first = watch.tick(NOW)
    second = watch.tick(NOW + timedelta(seconds=5))
    assert [r["reason"] for r in first] == ["no_engine_entry_price"]
    assert second == []
