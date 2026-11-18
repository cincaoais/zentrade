"""Night parity and per-day status records.

Everything here is offline: fixture trades and decision records must reproduce
the morning's actions, and ``days.jsonl`` lives under ``tmp_path``.
"""

from __future__ import annotations

from datetime import date

from app.forward.days import missing_days, record
from app.forward.parity import compare, decision_actions


def _decision(day: date, actions: list[dict], input_hash: str) -> dict:
    return {"date": day.isoformat(), "actions": actions, "input_hash": input_hash}


def _sell(symbol: str, shares: int, price: float, date_text: str) -> dict:
    return {
        "kind": "sell",
        "symbol": symbol,
        "shares": shares,
        "price": price,
        "reason": "signal",
        "signal_date": "2026-01-05",
    }


def _buy(symbol: str, shares: int, price: float) -> dict:
    return {
        "kind": "buy",
        "symbol": symbol,
        "shares": shares,
        "price": price,
        "reason": "entry",
        "signal_date": "2026-01-05",
    }


def _trade(
    symbol: str,
    *,
    entry_date: str,
    exit_date: str,
    shares: int,
    entry_price: float,
    exit_price: float,
    exit_reason: str,
) -> dict:
    return {
        "symbol": symbol,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "shares": shares,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }


def _happy_path(day: date) -> tuple[list[dict], dict[date, dict], dict[date, str]]:
    trades = [
        _trade(
            "AAA",
            entry_date="2025-12-01",
            exit_date=day.isoformat(),
            shares=10,
            entry_price=90.0,
            exit_price=100.0,
            exit_reason="signal",
        ),
        _trade(
            "BBB",
            entry_date=day.isoformat(),
            exit_date="2026-02-01",
            shares=20,
            entry_price=50.0,
            exit_price=60.0,
            exit_reason="signal",
        ),
    ]
    decisions = {
        day: _decision(day, [_sell("AAA", 10, 100.0, day.isoformat()), _buy("BBB", 20, 50.0)], "h1")
    }
    return trades, decisions, {day: "h1"}


def test_equal_is_clean() -> None:
    day = date(2026, 1, 6)
    trades, decisions, hashes = _happy_path(day)

    result = compare(trades, decisions, hashes)

    assert result == {"logic_mismatch": [], "data_revision": [], "intraday_stops": []}


def test_shares_difference_is_logic_mismatch() -> None:
    day = date(2026, 1, 6)
    trades, decisions, hashes = _happy_path(day)
    trades[1]["shares"] = 21

    result = compare(trades, decisions, hashes)

    assert result["data_revision"] == []
    assert result["intraday_stops"] == []
    assert any(
        item["symbol"] == "BBB" and item["field"] == "shares"
        for item in result["logic_mismatch"]
    )


def test_changed_input_is_data_revision() -> None:
    day = date(2026, 1, 6)
    trades, decisions, _ = _happy_path(day)
    trades[1]["shares"] = 21

    result = compare(trades, decisions, {day: "h2"})

    assert result["logic_mismatch"] == []
    assert result["data_revision"]
    revision = result["data_revision"][0]
    assert revision["date"] == day.isoformat()
    assert revision["saved_hash"] == "h1"
    assert revision["current_hash"] == "h2"
    assert any(item["symbol"] == "BBB" for item in revision["mismatches"])


def test_intraday_stop_listed_separately() -> None:
    day = date(2026, 1, 6)
    trades = [
        _trade(
            "AAA",
            entry_date="2026-01-02",
            exit_date=day.isoformat(),
            shares=10,
            entry_price=100.0,
            exit_price=85.0,
            exit_reason="stop_loss",
        )
    ]
    decisions = {day: _decision(day, [], "h1")}

    result = compare(trades, decisions, {day: "h1"})

    assert result["logic_mismatch"] == []
    assert result["data_revision"] == []
    assert len(result["intraday_stops"]) == 1
    stop = result["intraday_stops"][0]
    assert stop["symbol"] == "AAA"
    assert stop["date"] == day.isoformat()
    assert stop["reason"] == "stop_loss"
    assert stop["shares"] == 10


def test_decision_actions_reads_saved_records() -> None:
    day = date(2026, 1, 6)
    actions = decision_actions(
        _decision(day, [_sell("AAA", 10, 100.0, day.isoformat()), _buy("BBB", 20, 50.0)], "h1")
    )

    assert [(action.kind, action.symbol, action.shares) for action in actions] == [
        ("sell", "AAA", 10),
        ("buy", "BBB", 20),
    ]


def test_missing_days_found(tmp_path) -> None:
    data_dir = tmp_path / "data"
    monday = date(2026, 1, 5)
    tuesday = date(2026, 1, 6)
    wednesday = date(2026, 1, 7)
    thursday = date(2026, 1, 8)
    friday = date(2026, 1, 9)

    record(data_dir, "h2s", monday, "ok")
    record(data_dir, "h2s", wednesday, "no_plan", "night was not ready")
    # A different strategy's day is not this strategy's record.
    record(data_dir, "h6h43b", tuesday, "ok")

    assert missing_days(data_dir, "h2s", monday, friday) == [tuesday, thursday, friday]
    assert missing_days(data_dir, "h2s", tuesday, wednesday) == [tuesday]

def test_day_keeps_every_status(tmp_path) -> None:
    # One day can be both missed_open and logic_mismatch; neither may be lost.
    from app.forward.days import statuses_on

    day = date(2026, 11, 18)
    record(tmp_path, "h2s", day, "missed_open", "PC asleep")
    record(tmp_path, "h2s", day, "logic_mismatch", "shares differ")
    record(tmp_path, "h2s", day, "missed_open", "PC asleep (again)")

    assert statuses_on(tmp_path, "h2s", day) == {
        "missed_open": "PC asleep (again)",
        "logic_mismatch": "shares differ",
    }


def test_replay_mismatch_is_a_status(tmp_path) -> None:
    record(tmp_path, "h2s", date(2026, 11, 18), "replay_mismatch", "AAPL differs")
