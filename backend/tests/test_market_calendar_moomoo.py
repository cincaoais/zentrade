"""US trading calendar from moomoo OpenD (design: 2026-11-16-moomoo-calendar-design.md).

No test touches a real OpenD: a tiny local quote-context fake serves
``request_trading_days`` and ``set_ctx_factory`` injects it.
"""
from __future__ import annotations

import datetime as dt
import logging
from datetime import date, time

import pytest

from app.config import settings
from app.market_clock import calendar as cal


class FakeCalendarCtx:
    """The tiny part of ``OpenQuoteContext`` this calendar path uses."""

    def __init__(self, days: list[dict] | None = None, *, ret: int = 0, error: BaseException | None = None):
        self.days = days or []
        self.ret = ret
        self.error = error
        self.calls: list[tuple] = []
        self.closed = False

    def request_trading_days(self, market, start, end):
        self.calls.append((market, start, end))
        if self.error is not None:
            raise self.error
        if self.ret != 0:
            return self.ret, "boom"
        return 0, list(self.days)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def install_ctx(monkeypatch):
    """Install a factory returning ``ctx`` (or raising when it is an exception)."""
    from app.plugins.moomoo import provider

    def install(ctx) -> None:
        def factory(host, port):
            if isinstance(ctx, BaseException):
                raise ctx
            return ctx
        provider.set_ctx_factory(factory)

    yield install
    provider.set_ctx_factory(None)


@pytest.fixture
def no_alpaca_keys(monkeypatch):
    monkeypatch.setattr(cal, "_secrets", lambda: ("", "", "https://paper-api.alpaca.markets/v2"))


# ── moomoo rows → Alpaca shape ──────────────────────────────

def test_moomoo_rows_map_to_dates_and_early_closes(install_ctx):
    ctx = FakeCalendarCtx([
        {"time": "2025-07-03", "trade_date_type": "MORNING"},
        {"time": "2025-11-26", "trade_date_type": "WHOLE"},
    ])
    install_ctx(ctx)

    rows = cal._fetch_calendar_rows(date(2025, 7, 1), date(2025, 11, 30))

    assert rows == [
        {"date": "2025-07-03", "close": "13:00"},
        {"date": "2025-11-26", "close": "16:00"},
    ]
    import moomoo

    market, start, end = ctx.calls[0]
    assert market == moomoo.TradeDateMarket.US
    assert (start, end) == ("2025-07-01", "2025-11-30")
    assert ctx.closed is True


def test_unknown_trade_date_type_is_full_day_and_warned_once(install_ctx, caplog):
    ctx = FakeCalendarCtx([{"time": "2026-01-05", "trade_date_type": "AFTERNOON"}])
    install_ctx(ctx)

    with caplog.at_level(logging.WARNING, logger=cal.__name__):
        first = cal._fetch_calendar_rows(date(2026, 1, 5), date(2026, 1, 5))
        second = cal._fetch_calendar_rows(date(2026, 1, 5), date(2026, 1, 5))

    assert first == [{"date": "2026-01-05", "close": "16:00"}]
    assert second == first
    assert len([r for r in caplog.records if "trade_date_type" in r.getMessage()]) == 1


# ── failure paths ─────────────────────────────────────────

def test_moomoo_connection_failure_without_alpaca_keys_raises(install_ctx, no_alpaca_keys):
    install_ctx(ConnectionError("opend down"))

    with pytest.raises(ConnectionError, match="opend down"):
        cal._fetch_calendar_rows(date(2025, 11, 1), date(2025, 11, 30))


def test_moomoo_ret_error_without_alpaca_keys_raises(install_ctx, no_alpaca_keys):
    install_ctx(FakeCalendarCtx(ret=1))

    with pytest.raises(RuntimeError, match="moomoo"):
        cal._fetch_calendar_rows(date(2025, 11, 1), date(2025, 11, 30))


def test_moomoo_failure_with_alpaca_keys_uses_alpaca(install_ctx, monkeypatch):
    monkeypatch.setattr(cal, "_secrets", lambda: ("key", "secret", "https://paper-api.alpaca.markets/v2"))
    install_ctx(ConnectionError("opend down"))
    monkeypatch.setattr(
        cal, "_alpaca_calendar_rows",
        lambda s, e: [{"date": "2025-11-26", "open": "09:30", "close": "16:00"}],
    )

    assert cal._fetch_calendar_rows(date(2025, 11, 1), date(2025, 11, 30)) == [
        {"date": "2025-11-26", "open": "09:30", "close": "16:00"},
    ]


# ── end to end through the cache ───────────────────────────

@pytest.fixture
def e2e_calendar(tmp_path, monkeypatch, install_ctx):
    """Temporary data dir, frozen today, and moomoo data that stops at 2026-12-31."""
    from app.plugins.moomoo import provider

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    class _D(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 16)

    monkeypatch.setattr(cal, "date", _D)
    monkeypatch.setattr(cal, "_early_closes", None)
    monkeypatch.setattr(cal, "_last_failed_fetch", None, raising=False)

    morning = {date(2025, 11, 28), date(2025, 12, 24)}
    days: list[dict] = []
    cur, end = date(2025, 11, 17), date(2026, 12, 31)
    while cur <= end:
        if cur.weekday() < 5 and cur != date(2025, 11, 27):
            days.append({"time": cur.isoformat(),
                         "trade_date_type": "MORNING" if cur in morning else "WHOLE"})
        cur += dt.timedelta(days=1)
    install_ctx(FakeCalendarCtx(days))

    yield

    provider.set_ctx_factory(None)
    cal._early_closes = None
    cal._last_failed_fetch = None


def test_e2e_thanksgiving_is_not_a_trading_day(e2e_calendar):
    days = cal.alpaca_trading_days(date(2025, 11, 24), date(2025, 11, 28))
    assert date(2025, 11, 27) not in days
    assert date(2025, 11, 28) in days


def test_e2e_day_after_thanksgiving_early_close(e2e_calendar):
    cal.alpaca_trading_days(date(2025, 11, 28), date(2025, 11, 28))
    assert cal.alpaca_session_close(date(2025, 11, 28)) == time(13, 0)
    assert cal.alpaca_session_close(date(2025, 11, 26)) is None


def test_e2e_beyond_returned_range_is_trading_weekday(e2e_calendar):
    assert cal.alpaca_trading_days(date(2027, 1, 4), date(2027, 1, 4)) == [date(2027, 1, 4)]