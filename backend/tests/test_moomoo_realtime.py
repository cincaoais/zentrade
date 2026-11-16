"""Task 7 contract: moomoo realtime snapshots + quote poll-interval floor.

No test touches a real OpenD: ``FakeQuoteContext`` stands in for the SDK, and
the instrument universe is injected so only snapshot behaviour is exercised.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tests.moomoo_fakes import FakeClock, FakeQuoteContext

_ET = ZoneInfo("America/New_York")


def _snapshot(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "code": "US.AAPL",
        "update_time": "2026-11-13 16:00:00",
        "last_price": 101.5,
        "prev_close_price": 100.0,
        "open_price": 100.5,
        "high_price": 102.0,
        "low_price": 99.5,
        "volume": 123456,
        "turnover": 12_500_000.0,
    }
    row.update(overrides)
    return row


def _install(monkeypatch, ctx: FakeQuoteContext, instruments: list[dict]) -> object:
    from app.plugins.moomoo import provider as provider_mod

    provider_mod.set_ctx_factory(lambda host, port: ctx)
    instance = provider_mod.MoomooProvider()
    monkeypatch.setattr(
        instance,
        "get_instruments",
        lambda asset_type="stock": instruments,
    )
    return instance


def test_realtime_maps_snapshot(monkeypatch):
    ctx = FakeQuoteContext(
        clock=FakeClock(), returns={"get_market_snapshot": (0, [_snapshot()])}
    )
    provider = _install(monkeypatch, ctx, [{"symbol": "AAPL"}])

    records = provider.get_realtime()

    assert len(records) == 1
    row = records[0]
    expected_ts = int(
        dt.datetime(2026, 11, 13, 16, 0, 0, tzinfo=_ET).timestamp() * 1000
    )
    assert row["symbol"] == "AAPL"
    assert row["last_price"] == 101.5
    assert row["prev_close"] == 100.0
    assert row["open"] == 100.5
    assert row["high"] == 102.0
    assert row["low"] == 99.5
    assert row["volume"] == 123456
    assert row["amount"] == 12_500_000.0
    assert row["quote_ts"] == expected_ts
    assert row["volume_ts"] == expected_ts
    assert row["timestamp"] == expected_ts
    assert "feed" not in row
    assert row["data_delay"] >= 0

    calls = ctx.calls_named("get_market_snapshot")
    assert len(calls) == 1
    assert calls[0]["args"][0] == ["US.AAPL"]


def test_realtime_accepts_pandas_frame(monkeypatch):
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame([_snapshot()])
    ctx = FakeQuoteContext(
        clock=FakeClock(), returns={"get_market_snapshot": (0, frame)}
    )
    provider = _install(monkeypatch, ctx, [{"symbol": "AAPL"}])

    records = provider.get_realtime()

    assert len(records) == 1
    assert records[0]["symbol"] == "AAPL"
    assert records[0]["last_price"] == 101.5


def test_realtime_chunks_of_400(monkeypatch):
    instruments = [{"symbol": f"SYM{i:03d}"} for i in range(900)]
    ctx = FakeQuoteContext(clock=FakeClock(), returns={"get_market_snapshot": (0, [])})
    provider = _install(monkeypatch, ctx, instruments)

    records = provider.get_realtime()

    assert records == []
    calls = ctx.calls_named("get_market_snapshot")
    assert len(calls) == 3
    assert [len(call["args"][0]) for call in calls] == [400, 400, 100]


def test_realtime_caches_universe_for_one_hour(monkeypatch):
    clock = FakeClock()
    ctx = FakeQuoteContext(clock=clock, returns={"get_market_snapshot": (0, [])})
    provider = _install(monkeypatch, ctx, [{"symbol": "AAPL"}])
    provider._clock = clock.monotonic
    provider._sleep = clock.sleep
    universe_calls: list[str] = []

    def _instruments(asset_type="stock"):
        universe_calls.append(asset_type)
        return [{"symbol": "AAPL"}]

    monkeypatch.setattr(provider, "get_instruments", _instruments)

    provider.get_realtime()
    provider.get_realtime()
    assert len(universe_calls) == 1

    clock.advance(3601)
    provider.get_realtime()
    assert len(universe_calls) == 2


def test_quote_interval_floor_for_provider(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    from app.services.quote_service import QuoteService

    class _SlowProvider:
        min_realtime_interval_seconds = 15.0

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "moomoo")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: _SlowProvider())

    qs = QuoteService.__new__(QuoteService)
    assert qs._clamp_interval(6.0) == 15.0
    assert qs._clamp_interval(30.0) == 30.0