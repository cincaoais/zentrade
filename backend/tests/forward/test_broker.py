"""SIMULATE-only wrapper tests; no OpenD and no network."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from moomoo import OrderType, TrdEnv, TrdSide

import app.forward as forward_pkg
from app.forward.broker import NotSimulate, Quotes, SimBroker
from tests.forward.fake_trade import FakeTradeContext
from tests.moomoo_fakes import FakeClock, FakeQuoteContext


def _broker(ctx: FakeTradeContext) -> SimBroker:
    return SimBroker(ctx.account_id, ctx_factory=lambda: ctx)


def test_every_call_is_simulate() -> None:
    ctx = FakeTradeContext(account_id=123)
    broker = _broker(ctx)

    account = broker.check_account()
    assert account["acc_id"] == 123
    assert str(account["trd_env"]).upper() == "SIMULATE"
    assert broker.cash() == 1_000_000.0
    assert broker.positions() == {"AAPL": 100}
    order_id = broker.place_market("AAPL", "B", 10, "fw-h2s-20261118-AAPL-B")
    assert order_id == "ORD1"
    assert broker.orders_today()[0]["code"] == "AAPL"
    broker.cancel(order_id)

    for name in (
        "accinfo_query",
        "position_list_query",
        "place_order",
        "order_list_query",
        "modify_order",
    ):
        calls = ctx.calls_named(name)
        assert calls, f"{name} was not called"
        for call in calls:
            assert call["kwargs"]["trd_env"] == TrdEnv.SIMULATE
            assert call["kwargs"]["acc_id"] == 123
    assert ctx.calls_named("accinfo_query")[0]["kwargs"]["currency"] == "USD"
    place = ctx.calls_named("place_order")[0]["kwargs"]
    assert place["order_type"] == OrderType.MARKET
    assert place["trd_side"] == TrdSide.BUY
    assert place["price"] == 0
    assert place["time_in_force"] == "DAY"
    assert place["fill_outside_rth"] is False
    assert place["remark"] == "fw-h2s-20261118-AAPL-B"


def test_check_account_rejects_real() -> None:
    ctx = FakeTradeContext(account_id=9, accounts=[{"acc_id": 9, "trd_env": "REAL"}])
    with pytest.raises(NotSimulate):
        _broker(ctx).check_account()

    missing = FakeTradeContext(account_id=9, accounts=[{"acc_id": 10, "trd_env": "SIMULATE"}])
    with pytest.raises(NotSimulate):
        _broker(missing).check_account()


def test_no_real_strings_in_package() -> None:
    package = Path(forward_pkg.__file__).resolve().parent
    for path in package.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "unlock_trade" not in text, path.name
        assert "TrdEnv.REAL" not in text, path.name


def test_only_broker_imports_moomoo_trade() -> None:
    package = Path(forward_pkg.__file__).resolve().parent
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        moomoo = [name for name in modules if name == "moomoo" or name.startswith("moomoo.")]
        if path.name == "broker.py":
            assert moomoo, "broker.py must own the moomoo imports"
        else:
            assert not moomoo, f"{path.name} imports moomoo: {moomoo}"


def test_quotes_snapshot_reads_fields() -> None:
    clock = FakeClock()
    updated = datetime(2026, 11, 18, 14, 35, tzinfo=UTC)
    quote_ctx = FakeQuoteContext(
        clock=clock,
        returns={
            "get_market_snapshot": (
                0,
                [
                    {
                        "code": "US.AAPL",
                        "open_price": 100.0,
                        "last_price": 101.25,
                        "update_time": updated,
                    }
                ],
            )
        },
    )
    quotes = Quotes(ctx_factory=lambda: quote_ctx)

    snapshot = quotes.snapshot(["AAPL"])

    assert snapshot["AAPL"]["open_price"] == 100.0
    assert snapshot["AAPL"]["last_price"] == 101.25
    assert snapshot["AAPL"]["update_time"] == pytest.approx(updated.timestamp())
    assert quote_ctx.calls_named("get_market_snapshot")[0]["args"][0] == ["US.AAPL"]

def test_quotes_snapshot_chunks_400() -> None:
    clock = FakeClock()
    quote_ctx = FakeQuoteContext(clock=clock, returns={"get_market_snapshot": (0, [])})
    quotes = Quotes(ctx_factory=lambda: quote_ctx)

    quotes.snapshot([f"S{i}" for i in range(450)])

    calls = quote_ctx.calls_named("get_market_snapshot")
    assert len(calls) == 2
    assert [len(call["args"][0]) for call in calls] == [400, 50]
