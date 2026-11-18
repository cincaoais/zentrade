"""Live book and reconciliation tests; no OpenD and no network."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.forward.book import (
    LiveBook,
    Lot,
    check_saved_matches_journal,
    load_book,
    reconcile,
    save_book,
)


def test_rebuild_from_journal() -> None:
    events = [
        {
            "key": "fw-h2s-20261118-AAPL-B",
            "state": "submitting",
            "symbol": "AAPL",
            "side": "B",
            "qty": 100,
        },
        {
            "key": "fw-h2s-20261118-AAPL-B",
            "state": "submitted",
            "order_id": "ORD1",
            "symbol": "AAPL",
            "side": "B",
            "qty": 100,
        },
        {
            "key": "fw-h2s-20261118-AAPL-B",
            "state": "filled",
            "order_id": "ORD1",
            "symbol": "AAPL",
            "side": "B",
            "qty": 100,
            "dealt_qty": 100,
            "dealt_avg_price": 150.0,
        },
        {
            "key": "fw-h2s-20261118-MSFT-B",
            "state": "submitting",
            "symbol": "MSFT",
            "side": "B",
            "qty": 50,
        },
        {
            "key": "fw-h2s-20261118-MSFT-B",
            "state": "submitted",
            "order_id": "ORD2",
            "symbol": "MSFT",
            "side": "B",
            "qty": 50,
        },
        {
            "key": "fw-h2s-20261118-MSFT-B",
            "state": "partial",
            "order_id": "ORD2",
            "symbol": "MSFT",
            "side": "B",
            "qty": 50,
            "dealt_qty": 20,
            "dealt_avg_price": 300.0,
        },
        {
            "key": "fw-h2s-20261118-MSFT-B",
            "state": "cancelled",
            "order_id": "ORD2",
            "symbol": "MSFT",
            "side": "B",
            "qty": 50,
            "dealt_qty": 20,
            "dealt_avg_price": 300.0,
        },
        {
            "key": "fw-h2s-20261125-AAPL-S",
            "state": "submitting",
            "symbol": "AAPL",
            "side": "S",
            "qty": 40,
        },
        {
            "key": "fw-h2s-20261125-AAPL-S",
            "state": "filled",
            "order_id": "ORD3",
            "symbol": "AAPL",
            "side": "S",
            "qty": 40,
            "dealt_qty": 40,
            "dealt_avg_price": 160.0,
        },
        {
            "key": "fw-h2s-20261125-GOOG-B",
            "state": "submitting",
            "symbol": "GOOG",
            "side": "B",
            "qty": 10,
        },
        {
            "key": "fw-h2s-20261125-GOOG-B",
            "state": "failed",
            "symbol": "GOOG",
            "side": "B",
            "qty": 10,
            "dealt_qty": 0,
        },
        {
            "key": "fw-flatten-20261125-NVDA",
            "state": "filled",
            "symbol": "NVDA",
            "side": "S",
            "qty": 10,
            "dealt_qty": 10,
            "dealt_avg_price": 100.0,
        },
        {
            "key": "fw-h6h43b-20261125-AMZN-B",
            "state": "filled",
            "symbol": "AMZN",
            "side": "B",
            "qty": 15,
            "dealt_qty": 15,
            "dealt_avg_price": 200.0,
        },
    ]

    book = LiveBook.from_journal("h2s", capital=100_000.0, events=events)

    assert book.short == "h2s"
    assert "AAPL" in book.lots
    assert book.lots["AAPL"].shares == 60
    assert book.lots["AAPL"].entry_price == 150.0
    assert book.lots["AAPL"].entry_date == date(2026, 11, 18)
    assert book.lots["AAPL"].key == "fw-h2s-20261118-AAPL-B"

    assert "MSFT" in book.lots
    assert book.lots["MSFT"].shares == 20
    assert book.lots["MSFT"].entry_price == 300.0

    assert "GOOG" not in book.lots
    assert "NVDA" not in book.lots
    assert "AMZN" not in book.lots

    expected_cash = 100_000.0 - (100 * 150.0) - (20 * 300.0) + (40 * 160.0)
    assert abs(book.cash - expected_cash) < 1e-4

    expected_equity = expected_cash + 60 * 160.0 + 20 * 310.0
    assert abs(book.equity({"AAPL": 160.0, "MSFT": 310.0}) - expected_equity) < 1e-4


def test_two_strategies_same_symbol_reconcile_ok() -> None:
    h2s_book = LiveBook("h2s", capital=100_000.0, cash=85_000.0)
    h2s_book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=100,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h2s-20261118-AAPL-B",
    )

    h6_book = LiveBook("h6h43b", capital=100_000.0, cash=70_000.0)
    h6_book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=200,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h6h43b-20261118-AAPL-B",
    )

    res = reconcile(
        [h2s_book, h6_book],
        broker_positions={"AAPL": 300},
        broker_cash=155_000.0,
        adj_events={},
    )

    assert res["ok"] is True
    assert res["mismatches"] == []
    assert res["frozen_symbols"] == []
    assert res["scaled"] == []
    assert res["attention"] == []


def test_exit_sells_only_own_lot() -> None:
    h2s_book = LiveBook("h2s", capital=100_000.0, cash=85_000.0)
    h2s_book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=100,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h2s-20261118-AAPL-B",
    )

    h6_book = LiveBook("h6h43b", capital=100_000.0, cash=70_000.0)
    h6_book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=200,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h6h43b-20261118-AAPL-B",
    )

    exit_qty = h2s_book.lots["AAPL"].shares
    assert exit_qty == 100

    h2s_book.apply_fill(
        {
            "key": "fw-h2s-20261125-AAPL-S",
            "state": "filled",
            "symbol": "AAPL",
            "side": "S",
            "qty": exit_qty,
            "dealt_qty": exit_qty,
            "dealt_avg_price": 165.0,
        }
    )

    assert "AAPL" not in h2s_book.lots
    assert h6_book.lots["AAPL"].shares == 200

    res = reconcile(
        [h2s_book, h6_book],
        broker_positions={"AAPL": 200},
        broker_cash=155_000.0 + 16500.0,
        adj_events={},
    )
    assert res["ok"] is True
    assert res["mismatches"] == []


def test_split_scales_lots() -> None:
    book = LiveBook("h2s", capital=100_000.0, cash=85_000.0)
    book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=100,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h2s-20261118-AAPL-B",
    )

    res = reconcile(
        [book],
        broker_positions={"AAPL": 200},
        broker_cash=85_000.0,
        adj_events={"AAPL": 2.0},
    )

    assert res["ok"] is True
    assert book.lots["AAPL"].shares == 200
    assert abs(book.lots["AAPL"].entry_price - 75.0) < 1e-4
    assert len(res["scaled"]) == 1
    assert res["scaled"][0]["symbol"] == "AAPL"
    assert res["scaled"][0]["ratio"] == 2.0
    assert res["mismatches"] == []
    assert res["frozen_symbols"] == []


def test_unexplained_mismatch_freezes_symbol() -> None:
    book = LiveBook("h2s", capital=100_000.0, cash=85_000.0)
    book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=100,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h2s-20261118-AAPL-B",
    )

    res = reconcile(
        [book],
        broker_positions={"AAPL": 150},
        broker_cash=85_000.0,
        adj_events={},
    )

    assert res["ok"] is False
    assert res["frozen_symbols"] == ["AAPL"]
    assert res["mismatches"] == [{"symbol": "AAPL", "broker": 150, "books": 100}]


def test_failed_entry_no_lot_and_later_engine_exit_ignored() -> None:
    book = LiveBook("h2s", capital=100_000.0)
    book.apply_fill(
        {
            "key": "fw-h2s-20261118-AAPL-B",
            "state": "failed",
            "symbol": "AAPL",
            "side": "B",
            "qty": 100,
            "dealt_qty": 0,
        }
    )

    assert "AAPL" not in book.lots
    assert book.cash == 100_000.0

    # Engine later issues exit fill, but book has no lot: ignored
    book.apply_fill(
        {
            "key": "fw-h2s-20261125-AAPL-S",
            "state": "filled",
            "symbol": "AAPL",
            "side": "S",
            "qty": 100,
            "dealt_qty": 100,
            "dealt_avg_price": 160.0,
        }
    )

    assert "AAPL" not in book.lots
    assert book.cash == 100_000.0

    res = reconcile([book], broker_positions={}, broker_cash=100_000.0, adj_events={})
    assert res["ok"] is True


def test_halted_exit_stays_open() -> None:
    book = LiveBook("h2s", capital=100_000.0)
    book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=100,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h2s-20261118-AAPL-B",
    )

    # Cancelled exit with dealt_qty == 0
    book.apply_fill(
        {
            "key": "fw-h2s-20261125-AAPL-S",
            "state": "cancelled",
            "symbol": "AAPL",
            "side": "S",
            "qty": 100,
            "dealt_qty": 0,
        }
    )

    assert "AAPL" in book.lots
    assert book.lots["AAPL"].shares == 100


def test_saved_book_drift_detected(tmp_path: Path) -> None:
    book = LiveBook("h2s", capital=100_000.0, cash=85_000.0)
    book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=100,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h2s-20261118-AAPL-B",
    )
    path = tmp_path / "book.json"

    save_book(path, book)
    assert check_saved_matches_journal(path, book) is True

    loaded = load_book(path)
    assert loaded.lots["AAPL"] == book.lots["AAPL"]
    assert loaded.cash == book.cash

    # Drift in cash
    book.cash = 90_000.0
    assert check_saved_matches_journal(path, book) is False

    # Drift in lot shares
    book.cash = 85_000.0
    book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=90,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h2s-20261118-AAPL-B",
    )
    assert check_saved_matches_journal(path, book) is False


def test_unknown_order_needs_attention() -> None:
    events = [
        {
            "key": "fw-h2s-20261118-TSLA-B",
            "state": "unknown",
            "symbol": "TSLA",
            "side": "B",
            "qty": 10,
        }
    ]
    book = LiveBook.from_journal("h2s", capital=100_000.0, events=events)

    assert "TSLA" not in book.lots
    res = reconcile([book], broker_positions={}, broker_cash=100_000.0, adj_events={})

    assert res["ok"] is False
    assert len(res["attention"]) >= 1
    assert any(
        a.get("key") == "fw-h2s-20261118-TSLA-B" or a.get("symbol") == "TSLA"
        for a in res["attention"]
    )


def test_close_corporate() -> None:
    book = LiveBook("h2s", capital=100_000.0, cash=85_000.0)
    book.lots["AAPL"] = Lot(
        symbol="AAPL",
        shares=100,
        entry_date=date(2026, 11, 18),
        entry_price=150.0,
        key="fw-h2s-20261118-AAPL-B",
    )

    book.close_corporate("AAPL", 16000.0)

    assert "AAPL" not in book.lots
    assert abs(book.cash - 101_000.0) < 1e-4
