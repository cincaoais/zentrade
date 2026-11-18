"""Order journal and idempotent executor tests; no OpenD and no network."""

from __future__ import annotations

from pathlib import Path

import pytest
from moomoo import ModifyOrderOp, TrdEnv

from app.forward.broker import SimBroker
from app.forward.orders import Executor, Journal, OrderCapExceeded
from tests.forward.fake_trade import FakeTradeContext
from tests.moomoo_fakes import FakeClock

DAY = "20261118"


def _key(symbol: str = "AAPL", side: str = "B", short: str = "h2s") -> str:
    return f"fw-{short}-{DAY}-{symbol}-{side}"


def _setup(
    tmp_path: Path,
    *,
    account_id: int = 7,
    partial_fill_qty: int | None = None,
    accept_then_raise: BaseException | None = None,
    drop_orders: bool = False,
) -> tuple[Journal, Executor, FakeTradeContext, FakeClock]:
    clock = FakeClock()
    journal = Journal(tmp_path / "orders.jsonl")
    ctx = FakeTradeContext(
        account_id=account_id,
        clock=clock,
        partial_fill_qty=partial_fill_qty,
        accept_then_raise=accept_then_raise,
        drop_orders=drop_orders,
    )
    broker = SimBroker(account_id, ctx_factory=lambda: ctx)
    executor = Executor(broker, journal, clock=clock)
    return journal, executor, ctx, clock


def test_journal_before_submit(tmp_path: Path) -> None:
    journal, executor, ctx, _ = _setup(tmp_path, accept_then_raise=RuntimeError("socket"))
    key = _key()
    seen: list[str | None] = []
    ctx.on_place = lambda: seen.append(journal.state(key))

    result = executor.submit(key, "AAPL", "B", 10)

    assert seen == ["submitting"]
    assert result == "submitted"
    assert journal.state(key) == "submitted"
    assert len(ctx.calls_named("place_order")) == 1

    executor.submit(key, "AAPL", "B", 10)
    assert len(ctx.calls_named("place_order")) == 1
    assert journal.state(key) == "submitted"


def test_unknown_never_resent(tmp_path: Path) -> None:
    journal, executor, ctx, _ = _setup(
        tmp_path, accept_then_raise=RuntimeError("socket"), drop_orders=True
    )
    key = _key()

    assert executor.submit(key, "AAPL", "B", 10) == "unknown"
    assert journal.state(key) == "unknown"
    assert len(ctx.calls_named("place_order")) == 1

    assert executor.submit(key, "AAPL", "B", 10) == "unknown"
    assert len(ctx.calls_named("place_order")) == 1


def test_pacing(tmp_path: Path) -> None:
    _, executor, ctx, _ = _setup(tmp_path)

    executor.submit(_key("AAPL", "B"), "AAPL", "B", 10)
    executor.submit(_key("MSFT", "B"), "MSFT", "B", 10)

    times = [call["time"] for call in ctx.calls_named("place_order")]
    assert len(times) == 2
    assert times[1] - times[0] >= 2.5


def test_partial_exit_retry_once(tmp_path: Path) -> None:
    journal, executor, ctx, clock = _setup(tmp_path, partial_fill_qty=40)
    key = _key("AAPL", "S")

    executor.submit(key, "AAPL", "S", 100)
    result = executor.poll_until(clock.monotonic() + 5)

    assert result[key]["dealt_qty"] == 40
    assert result[key]["status"] in {"partial", "cancelled"}
    remarks = [call["kwargs"]["remark"] for call in ctx.calls_named("place_order")]
    assert remarks == [key, f"{key}-r1"]
    retry = ctx.calls_named("place_order")[1]["kwargs"]
    assert retry["qty"] == 60
    assert journal.state(f"{key}-r1") in {"submitting", "submitted", "partial", "filled"}

    executor.submit(f"{key}-r1", "AAPL", "S", 60)
    assert len(ctx.calls_named("place_order")) == 2


def test_cancel_at_deadline(tmp_path: Path) -> None:
    _, executor, ctx, clock = _setup(tmp_path)
    key = _key()

    executor.submit(key, "AAPL", "B", 10)
    result = executor.poll_until(clock.monotonic() + 5)

    assert result[key]["status"] == "cancelled"
    cancels = ctx.calls_named("modify_order")
    assert len(cancels) == 1
    assert cancels[0]["kwargs"]["modify_order_op"] == ModifyOrderOp.CANCEL
    assert cancels[0]["kwargs"]["order_id"] == "ORD1"


def test_daily_cap(tmp_path: Path) -> None:
    journal, executor, ctx, _ = _setup(tmp_path)
    for index in range(25):
        journal.append(
            {
                "key": _key(f"S{index:02d}"),
                "state": "submitting",
                "symbol": f"S{index:02d}",
                "side": "B",
                "qty": 1,
            }
        )

    assert executor.daily_count("h2s", DAY) == 25
    assert executor.daily_count("h6h43b", DAY) == 0
    assert executor.daily_count("h2s", "20261119") == 0

    with pytest.raises(OrderCapExceeded):
        executor.submit(_key("NEW"), "NEW", "B", 1)
    assert not ctx.calls_named("place_order")


def test_journal_state_is_latest(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "orders.jsonl")
    journal.append({"key": "k", "state": "submitting"})
    journal.append({"key": "k", "state": "submitted"})
    journal.append({"key": "k", "state": "filled"})
    journal.append({"key": "other", "state": "submitting"})

    assert journal.state("k") == "filled"
    assert journal.state("missing") is None
    assert [event["state"] for event in journal.events()] == [
        "submitting",
        "submitted",
        "filled",
        "submitting",
    ]


def test_trd_env_constant_is_simulate() -> None:
    assert TrdEnv.SIMULATE == "SIMULATE"

def _flaky_order_list(ctx: FakeTradeContext, empty_calls: int):
    """Make the first ``empty_calls`` order-list queries return no rows."""
    real = ctx.order_list_query
    seen = {"n": 0}

    def flaky(*args, **kwargs):
        seen["n"] += 1
        if seen["n"] <= empty_calls:
            ctx._record("order_list_query", args, kwargs, trade=True)
            return 0, []
        return real(*args, **kwargs)

    ctx.order_list_query = flaky


def test_transiently_missing_order_still_cancelled_at_deadline(tmp_path: Path) -> None:
    # An order briefly absent from the list is still live: it must not be
    # written off as unknown mid-poll, and it must be cancelled at the deadline.
    _journal, executor, ctx, clock = _setup(tmp_path)
    key = _key()
    executor.submit(key, "AAPL", "B", 10)
    _flaky_order_list(ctx, empty_calls=1)

    result = executor.poll_until(clock.monotonic() + 10)

    assert result[key]["status"] == "cancelled"
    assert ctx.calls_named("modify_order")


def test_order_missing_until_deadline_is_unknown(tmp_path: Path) -> None:
    journal, executor, ctx, clock = _setup(tmp_path)
    key = _key()
    executor.submit(key, "AAPL", "B", 10)
    _flaky_order_list(ctx, empty_calls=10_000)

    result = executor.poll_until(clock.monotonic() + 10)

    assert result[key]["status"] == "unknown"
    assert journal.state(key) == "unknown"
