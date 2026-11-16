"""Task 1 contract: moomoo plugin skeleton, OpenD address, availability, symbol mapping.

No test here touches a real OpenD: ``availability`` must not connect, and the
status test injects a fake quote context.
"""
from __future__ import annotations

import pytest

from app import secrets_store


@pytest.fixture(autouse=True)
def _empty_secrets(monkeypatch):
    """Keep the developer's real secrets.json out of these tests."""
    monkeypatch.setattr(secrets_store, "load", lambda: {})


def test_symbol_roundtrip():
    from app.plugins.moomoo import provider

    assert provider.to_moomoo("BRK.B") == "US.BRK.B"
    assert provider.to_moomoo("AAPL") == "US.AAPL"
    assert provider.from_moomoo("US.AAPL") == "AAPL"
    assert provider.from_moomoo("US.BRK.B") == "BRK.B"
    with pytest.raises(ValueError):
        provider.from_moomoo("HK.00700")


def test_opend_address_default_and_env(monkeypatch):
    from app.plugins.moomoo import provider

    monkeypatch.delenv("ZENTRADE_MOOMOO_OPEND", raising=False)
    assert provider.opend_address() == ("127.0.0.1", 11111)

    monkeypatch.setenv("ZENTRADE_MOOMOO_OPEND", "10.0.0.5:22222")
    assert provider.opend_address() == ("10.0.0.5", 22222)

    monkeypatch.setenv("ZENTRADE_MOOMOO_OPEND", "bad")
    with pytest.raises(ValueError):
        provider.opend_address()


def test_availability_true_without_opend(monkeypatch):
    from app.plugins.moomoo import provider

    monkeypatch.delenv("ZENTRADE_MOOMOO_OPEND", raising=False)
    ok, reason = provider.availability()
    assert ok is True
    assert reason


def test_opend_status_reports_login(monkeypatch):
    from app.plugins.moomoo import provider
    from tests.moomoo_fakes import FakeQuoteContext

    provider.set_ctx_factory(
        lambda host, port: FakeQuoteContext(
            returns={"get_global_state": {"qot_logined": True, "market_us": "AFTER_HOURS_END"}}
        )
    )
    try:
        status = provider.MoomooProvider().opend_status()
    finally:
        provider.set_ctx_factory(None)

    assert status["ok"] is True
    assert status["qot_logined"] is True
    assert status["market_us"] == "AFTER_HOURS_END"
    assert status["error"] is None

    def unreachable(host, port):
        raise ConnectionError("opend not reachable")

    provider.set_ctx_factory(unreachable)
    try:
        failed = provider.MoomooProvider().opend_status()
    finally:
        provider.set_ctx_factory(None)

    assert failed["ok"] is False
    assert failed["error"]


def test_plugin_registered():
    from app.data_providers import custom as cs

    assert cs.provider_has_dataset("moomoo", "daily") is True
    assert cs.provider_has_dataset("moomoo", "minute") is False

def test_default_ctx_fails_fast_when_opend_down(monkeypatch):
    """A closed OpenD port must raise within ~1 s instead of the SDK's long retry loop."""
    import time

    from app.plugins.moomoo import provider as p

    monkeypatch.setenv("ZENTRADE_MOOMOO_OPEND", "127.0.0.1:1")
    p.set_ctx_factory(None)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        with p.open_ctx():
            pass
    assert time.monotonic() - t0 < 3.0
