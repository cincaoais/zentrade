"""Task 4 contract: subscription rotation reading raw + QFQ daily bars.

No test touches a real OpenD: ``FakeQuoteContext`` and ``FakeClock`` stand in,
so subscription batches, hold time and failure handling are deterministic.
"""

from __future__ import annotations

import moomoo
import polars as pl
import pytest

from tests.moomoo_fakes import FakeClock, FakeQuoteContext

_COLUMNS = ("time_key", "open", "high", "low", "close", "volume", "turnover")


def _frame(code: str, close: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "time_key": ["2026-11-13 00:00:00", "2026-11-14 00:00:00"],
            "open": [close - 1.0, close - 0.5],
            "high": [close + 1.0, close + 0.5],
            "low": [close - 2.0, close - 1.0],
            "close": [close, close + 0.25],
            "volume": [1000, 1200],
            "turnover": [1.5e5, 1.8e5],
            "code": [code, code],
        }
    )


def _kline_return(autype: object) -> object:
    """``get_cur_kline`` fake: distinct frames for raw vs QFQ."""

    def _call(*args: object, **_kwargs: object) -> object:
        code = str(args[0])
        requested = args[3] if len(args) > 3 else None
        return _frame(code, 100.0 if requested == autype else 90.0)

    return _call


def _fake(
    remain: int, *, subscribe: object | None = None, clock: FakeClock | None = None
) -> FakeQuoteContext:
    returns: dict[str, object] = {
        "query_subscription": (0, {"remain": remain}),
        "get_cur_kline": _kline_return(moomoo.AuType.NONE),
    }
    if subscribe is not None:
        returns["subscribe"] = subscribe
    return FakeQuoteContext(clock=clock, returns=returns)


def _rotation(ctx: FakeQuoteContext, codes: list[str]) -> list[object]:
    from app.plugins.moomoo.rotation import rotate_daily

    return list(rotate_daily(ctx, codes, clock=ctx.clock.monotonic, sleep=ctx.clock.sleep))


def test_batch_size_follows_free_quota() -> None:
    ctx = _fake(remain=70)
    codes = [f"US.S{i:03d}" for i in range(150)]

    batches = _rotation(ctx, codes)

    assert [len(b.bars) + len(b.failed) for b in batches] == [70, 70, 10]
    assert len(ctx.calls_named("subscribe")) == 3
    subscribed = [call["args"][0] for call in ctx.calls_named("subscribe")]
    assert [len(batch) for batch in subscribed] == [70, 70, 10]
    assert len(ctx.calls_named("query_subscription")) == 3


def test_zero_free_quota_raises() -> None:
    ctx = _fake(remain=0)

    with pytest.raises(RuntimeError, match="quota exhausted"):
        _rotation(ctx, ["US.AAPL"])


def test_hold_at_least_61s() -> None:
    ctx = _fake(remain=2)

    _rotation(ctx, ["US.AAPL", "US.MSFT", "US.NVDA"])

    subscribes = ctx.calls_named("subscribe")
    unsubscribes = ctx.calls_named("unsubscribe")
    assert len(subscribes) == len(unsubscribes) == 2
    for sub, unsub in zip(subscribes, unsubscribes, strict=True):
        assert unsub["time"] - sub["time"] >= 61.0


def test_rotation_batch_error_marks_failed_and_continues() -> None:
    calls = {"n": 0}

    def _subscribe(*_args: object, **_kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return -1, "subscribe failed"
        return 0, []

    ctx = _fake(remain=2, subscribe=_subscribe)
    codes = ["US.AAPL", "US.MSFT", "US.NVDA", "US.TSLA"]

    batches = _rotation(ctx, codes)

    assert batches[0].failed == ["US.AAPL", "US.MSFT"]
    assert batches[0].bars == {}
    assert batches[1].failed == []
    assert set(batches[1].bars) == {"US.NVDA", "US.TSLA"}
    assert len(ctx.calls_named("subscribe")) == 2


def test_raw_and_qfq_both_fetched() -> None:
    ctx = _fake(remain=10)
    code = "US.AAPL"

    batches = _rotation(ctx, [code])

    raw, qfq = batches[0].bars[code]
    assert tuple(raw.columns)[: len(_COLUMNS)] == _COLUMNS
    assert raw["close"][0] == 100.0
    assert qfq["close"][0] == 90.0
    autypes = [call["args"][3] for call in ctx.calls_named("get_cur_kline")]
    assert autypes == [moomoo.AuType.NONE, moomoo.AuType.QFQ]
