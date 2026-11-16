"""Task 3 contract: ``get_instruments`` universe filter for moomoo.

The fake ``get_stock_basicinfo`` returns dict rows shaped like the SDK's
DataFrame; only US NYSE/NASDAQ equities with a real ticker survive.
"""
from __future__ import annotations

import datetime as dt
import logging

ROWS = [
    {
        "code": "US.AAPL",
        "name": "Apple Inc.",
        "exchange_type": "US_NASDAQ",
        "delisting": False,
        "listing_date": "1980-12-12",
        "suspension": "N/A",
    },
    {
        "code": "US.BRK.B",
        "name": "Berkshire Hathaway Inc.",
        "exchange_type": "US_NYSE",
        "delisting": False,
        "listing_date": "1980-01-01",
        "suspension": "N/A",
    },
    {
        "code": "US.1061",
        "name": "Odd numeric code",
        "exchange_type": "US_NASDAQ",
        "delisting": False,
        "listing_date": "2020-01-01",
        "suspension": "N/A",
    },
    {
        "code": "US.2618996D",
        "name": "Odd alphanumeric code",
        "exchange_type": "US_NASDAQ",
        "delisting": False,
        "listing_date": "2020-01-01",
        "suspension": "N/A",
    },
    {
        "code": "US.XYZ",
        "name": "Pink sheet",
        "exchange_type": "US_PINK",
        "delisting": False,
        "listing_date": "2020-01-01",
        "suspension": "N/A",
    },
    {
        "code": "US.OLD",
        "name": "Delisted Corp.",
        "exchange_type": "US_NYSE",
        "delisting": True,
        "listing_date": "1990-01-01",
        "suspension": "N/A",
    },
    {
        "code": "US.NEW",
        "name": "Unknown listing date",
        "exchange_type": "US_NASDAQ",
        "delisting": False,
        "listing_date": "1970-01-01",
        "suspension": "N/A",
    },
]


def _provider_with(rows):
    from app.plugins.moomoo import provider
    from tests.moomoo_fakes import FakeQuoteContext

    ctx = FakeQuoteContext(returns={"get_stock_basicinfo": rows})
    provider.set_ctx_factory(lambda host, port: ctx)
    return provider.MoomooProvider(), ctx, provider


def test_instruments_filter(caplog):
    caplog.set_level(logging.INFO, logger="app.plugins.moomoo.provider")
    provider_obj, ctx, provider = _provider_with(ROWS)
    try:
        items = provider_obj.get_instruments("stock")
    finally:
        provider.set_ctx_factory(None)

    assert [item["symbol"] for item in items] == ["AAPL", "BRK.B", "NEW"]

    by_symbol = {item["symbol"]: item for item in items}
    assert by_symbol["BRK.B"]["exchange"] == "NYSE"
    assert by_symbol["AAPL"]["exchange"] == "NASDAQ"
    assert by_symbol["AAPL"]["listing_date"] == dt.date(1980, 12, 12)
    assert by_symbol["NEW"]["listing_date"] is None

    for item in items:
        assert item["asset_type"] == "stock"
        assert item["source"] == "moomoo"
        assert item["region"] == "US"
        assert item["name"]
    assert by_symbol["AAPL"]["code"] == "AAPL"

    # OpenD context is always closed.
    assert ctx.closed is True

    # Suspension values are logged for the Task 9 live check.
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "suspension" in messages.lower()


def test_instruments_non_stock_returns_empty():
    from app.plugins.moomoo import provider

    def boom(host, port):
        raise AssertionError("non-stock asset_type must not open an OpenD context")

    provider.set_ctx_factory(boom)
    try:
        assert provider.MoomooProvider().get_instruments("etf") == []
    finally:
        provider.set_ctx_factory(None)