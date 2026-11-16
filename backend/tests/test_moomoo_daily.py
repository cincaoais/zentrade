"""Task 5 contract: ``iter_daily`` / ``get_daily`` + per-day event cache.

No test touches a real OpenD: ``FakeQuoteContext`` serves polars or pandas
frames exactly as ``get_cur_kline`` would, and ``FakeClock`` makes the 61 s
subscription hold instantaneous.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock
from tests.moomoo_fakes import FakeClock, FakeQuoteContext

CANONICAL = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}

FIRST_DAY = dt.date(2026, 10, 1)


def _frame(
    code: str,
    closes: list[float],
    *,
    volumes: list[float] | None = None,
    turnovers: list[float] | None = None,
    as_pandas: bool = False,
):
    """A ``get_cur_kline`` frame with consecutive calendar days from 2026-10-01."""
    days = [FIRST_DAY + dt.timedelta(days=i) for i in range(len(closes))]
    volumes = volumes or [1000.0] * len(days)
    turnovers = turnovers or [1.0e5] * len(days)
    data = {
        "code": [code] * len(days),
        "name": ["Test Corp"] * len(days),
        "time_key": [f"{day.isoformat()} 00:00:00" for day in days],
        "open": closes,
        "close": closes,
        "high": closes,
        "low": closes,
        "volume": volumes,
        "turnover": turnovers,
    }
    if as_pandas:
        import pandas as pd

        return pd.DataFrame(data)
    return pl.DataFrame(data)


def _kline(frames: dict[str, tuple[object, object]]):
    """``get_cur_kline`` fake: raw vs QFQ frames per code."""

    def _call(*args, **kwargs):
        import moomoo

        code = str(args[0])
        autype = args[3] if len(args) > 3 else kwargs.get("autype")
        raw, qfq = frames[code]
        return 0, raw if autype == moomoo.AuType.NONE else qfq

    return _call


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    from app.plugins.moomoo import provider as provider_module

    monkeypatch.setattr(provider_module, "settings", SimpleNamespace(data_dir=tmp_path))
    yield tmp_path
    provider_module.set_ctx_factory(None)


def _provider(frames: dict[str, tuple[object, object]], *, remain: int = 10):
    from app.plugins.moomoo import provider as provider_module

    clock = FakeClock()
    ctx = FakeQuoteContext(
        clock=clock,
        returns={
            "query_subscription": (0, {"remain": remain}),
            "get_cur_kline": _kline(frames),
        },
    )
    provider_module.set_ctx_factory(lambda host, port: ctx)
    provider = provider_module.MoomooProvider()
    provider._clock = clock.monotonic
    provider._sleep = clock.sleep
    return provider, ctx


def _rows(frames):
    out = []
    for frame in frames:
        out.extend(frame.select("symbol", "date").iter_rows())
    return out


def test_iter_daily_canonical_columns():
    raw = _frame("US.AAPL", [100.0, 101.0], turnovers=[1.5e5, 1.8e5])
    provider, _ctx = _provider({"US.AAPL": (raw, raw)})

    frames = list(provider.iter_daily(["AAPL"]))

    assert len(frames) == 1
    df = frames[0]
    assert df.schema == CANONICAL
    assert df["amount"].to_list() == [1.5e5, 1.8e5]
    assert df["symbol"].to_list() == ["AAPL", "AAPL"]


def test_iter_daily_reemits_repair_window():
    closes = [100.0 + i * 0.1 for i in range(44)]  # 2026-10-01 … 2026-11-13
    raw = _frame("US.AAPL", closes)
    provider, _ctx = _provider({"US.AAPL": (raw, raw)})

    frames = list(
        provider.iter_daily(
            ["AAPL"],
            start_time=dt.datetime(2026, 11, 13),
            end_time=dt.datetime(2026, 11, 13),
        )
    )

    assert len(frames) == 1
    df = frames[0]
    assert df["date"].min() == dt.date(2026, 10, 30)  # 13 Nov - 14 calendar days
    assert df["date"].max() == dt.date(2026, 11, 13)


def test_iter_daily_writes_event_cache(tmp_path):
    from app.plugins.moomoo import events_cache

    raw = _frame("US.AAPL", [100.0, 10.1], volumes=[1000.0, 1000.0])
    qfq = _frame("US.AAPL", [10.0, 10.1], volumes=[10000.0, 1000.0])
    provider, _ctx = _provider({"US.AAPL": (raw, qfq)})

    list(provider.iter_daily(["AAPL"]))

    cached = events_cache.read(tmp_path, us_clock.today())
    assert cached is not None
    events, covered = cached
    assert covered == {"AAPL"}
    assert events.height == 1
    row = events.row(0, named=True)
    assert row["symbol"] == "AAPL"
    assert row["trade_date"] == dt.date(2026, 10, 2)
    assert row["ex_factor"] == pytest.approx(10.0)


def test_failed_codes_absent_not_raised():
    raw = _frame("US.AAPL", [100.0, 101.0])

    def _call(*args, **kwargs):
        if str(args[0]) == "US.MSFT":
            raise RuntimeError("kline unavailable")
        return 0, raw

    from app.plugins.moomoo import provider as provider_module

    ctx = FakeQuoteContext(
        returns={
            "query_subscription": (0, {"remain": 10}),
            "get_cur_kline": _call,
        }
    )
    provider_module.set_ctx_factory(lambda host, port: ctx)
    provider = provider_module.MoomooProvider()
    provider._clock = ctx.clock.monotonic
    provider._sleep = ctx.clock.sleep

    frames = list(provider.iter_daily(["AAPL", "MSFT"]))

    assert {symbol for symbol, _day in _rows(frames)} == {"AAPL"}
    assert len(ctx.calls_named("get_cur_kline")) == 3  # raw AAPL + raw/qfq MSFT


def test_iter_daily_accepts_pandas_frames(tmp_path):
    from app.plugins.moomoo import events_cache

    raw = _frame("US.AAPL", [100.0, 10.1], volumes=[1000.0, 1000.0], as_pandas=True)
    qfq = _frame("US.AAPL", [10.0, 10.1], volumes=[10000.0, 1000.0], as_pandas=True)
    provider, _ctx = _provider({"US.AAPL": (raw, qfq)})

    frames = list(provider.iter_daily(["AAPL"]))

    assert len(frames) == 1
    assert frames[0].schema == CANONICAL
    assert frames[0]["date"].to_list() == [FIRST_DAY, FIRST_DAY + dt.timedelta(days=1)]

    cached = events_cache.read(tmp_path, us_clock.today())
    assert cached is not None
    events, covered = cached
    assert covered == {"AAPL"}
    assert events.height == 1
    assert events.row(0, named=True)["ex_factor"] == pytest.approx(10.0)


def test_get_daily_concatenates_batches():
    frames = {
        "US.AAPL": (_frame("US.AAPL", [100.0]), _frame("US.AAPL", [100.0])),
        "US.MSFT": (_frame("US.MSFT", [50.0]), _frame("US.MSFT", [50.0])),
    }
    provider, ctx = _provider(frames, remain=1)  # forces one batch per code

    df = provider.get_daily(["AAPL", "MSFT"])

    assert len(ctx.calls_named("subscribe")) == 2
    assert df.schema == CANONICAL
    assert df["symbol"].to_list() == ["AAPL", "MSFT"]