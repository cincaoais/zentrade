"""Monthly forward report: header, missing days, gate, execution, reference numbers.

Everything is offline: fixture records live under ``tmp_path`` and the engine
book is supplied by a fake function; no OpenD, no network, no real data dir.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.forward import report
from app.forward.days import record
from app.market_clock import us_clock
from app.research.metrics import curve_stats

LEADS = {"H2-spring": "h2s", "H6-H4-3B": "h6h43b"}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _registration(
    data: Path,
    *,
    start: date = date(2026, 11, 19),
    reference: dict | None = None,
    var_sr_trials: float = 0.0123,
) -> Path:
    strategies: dict[str, dict] = {}
    for lead, short in LEADS.items():
        entry: dict = {"short": short, "variant_id": lead}
        if reference is not None and lead == "H2-spring":
            entry["reference"] = reference
        strategies[lead] = entry
    payload = {
        "account_id": 12345678,
        "capital_per_strategy": 1_000_000.0,
        "evaluation": {
            "verdict_book": "engine",
            "min_months": 12,
            "min_closed_trades": 100,
            "gate": [
                "cagr_vs_bh",
                "cagr_vs_rand95",
                "trades_ge_100",
                "avg_r_pos",
                "dsr_ge_0_9",
            ],
            "n_trials": 71,
            "var_sr_trials": var_sr_trials,
        },
        "pins": {"chan_version": "abc", "bridge_hash": "def"},
        "real_never_used": True,
        "start_date": start.isoformat(),
        "strategies": strategies,
    }
    path = data / "forward" / "registration.json"
    _write_json(path, payload)
    return path


def _trade(
    symbol: str,
    entry_date: str,
    exit_date: str,
    *,
    shares: int = 10,
    entry_price: float = 100.0,
    exit_price: float = 110.0,
    exit_reason: str = "signal",
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


def _few_trades(short: str, start: date, end: date) -> list[dict]:
    return [
        _trade("AAA", "2026-11-20", "2026-11-23", exit_price=110.0),
        _trade("BBB", "2026-11-23", "2026-11-30", exit_price=95.0),
    ]


def _equity(start: date, end: date) -> pl.DataFrame:
    days = us_clock.trading_days_between(start, end)
    return pl.DataFrame(
        {"date": days, "equity": [1_000_000.0 * (1.0 + 0.001 * i) for i in range(len(days))]},
        schema={"date": pl.Date, "equity": pl.Float64},
    )


def _book(trades: list[dict], start: date, end: date, table: pl.DataFrame | None = None) -> dict:
    return {
        "trades": trades,
        "equity": _equity(start, end),
        "table": table if table is not None else pl.DataFrame(),
    }


def _few_book(short: str, start: date, end: date) -> dict:
    return _book(_few_trades(short, start, end), start, end)


def _decision(
    data: Path, short: str, day: date, opens: dict[str, float], actions: list[dict]
) -> None:
    _write_json(
        data / "forward" / short / "decision" / f"{day.isoformat()}.json",
        {
            "date": day.isoformat(),
            "opens": opens,
            "actions": actions,
            "input_hash": "hash",
        },
    )


def _event(
    key: str, state: str, *, symbol: str, side: str, qty: int, price: float | None = None
) -> dict:
    event: dict = {
        "key": key,
        "state": state,
        "symbol": symbol,
        "side": side,
        "qty": qty,
    }
    if price is not None:
        event["dealt_qty"] = qty
        event["dealt_avg_price"] = price
    return event


def test_interim_header_before_evaluation(tmp_path: Path) -> None:
    _registration(tmp_path)
    text = report.build_report(
        tmp_path,
        "2026-12",
        as_of=date(2026, 12, 31),
        engine_book_fn=_few_book,
    )
    assert "**INTERIM, not a verdict**" in text
    assert "**VERDICT**" not in text


def test_verdict_after_evaluation_with_100_closed_trades(tmp_path: Path) -> None:
    _registration(tmp_path)

    def full_year(short: str, start: date, end: date) -> dict:
        trades = [
            _trade(
                f"S{i}",
                "2026-11-20",
                f"2027-{1 + i // 28:02d}-{1 + i % 28:02d}",
                shares=10,
                entry_price=100.0,
                exit_price=110.0,
            )
            for i in range(100)
        ]
        return _book(trades, start, end)

    text = report.build_report(
        tmp_path,
        "2028-01",
        as_of=date(2028, 1, 3),
        engine_book_fn=full_year,
    )
    assert "**VERDICT**" in text
    assert "**INTERIM, not a verdict**" not in text


def test_missing_day_is_an_error_line(tmp_path: Path) -> None:
    _registration(tmp_path)
    for short in LEADS.values():
        record(tmp_path, short, date(2026, 11, 19), "ok")
        record(tmp_path, short, date(2026, 11, 23), "ok")
    text = report.build_report(
        tmp_path,
        "2026-11",
        as_of=date(2026, 11, 23),
        engine_book_fn=_few_book,
    )
    assert "missing day" in text.lower()
    assert "2026-11-20" in text


def test_gate_uses_registered_n_trials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registration(tmp_path, var_sr_trials=0.0123)
    calls: list[tuple[int, float]] = []
    real_summarize = report.summarize

    def spy(*args: object, **kwargs: object) -> dict:
        calls.append((int(args[3]), float(args[4])))  # type: ignore[arg-type]
        return real_summarize(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(report, "summarize", spy)
    text = report.build_report(
        tmp_path,
        "2026-11",
        as_of=date(2026, 11, 23),
        engine_book_fn=_few_book,
    )
    assert (71, 0.0123) in calls
    assert "n_trials=71" in text
    assert "var_sr_trials=0.0123" in text
    for name in ("cagr_vs_bh", "cagr_vs_rand95", "trades_ge_100", "avg_r_pos", "dsr_ge_0_9"):
        assert name in text


def test_execution_bps(tmp_path: Path) -> None:
    _registration(tmp_path)
    day = date(2026, 11, 20)
    _decision(
        tmp_path,
        "h2s",
        day,
        {"AAA": 100.0, "BBB": 100.0},
        [
            {"kind": "buy", "symbol": "AAA", "shares": 10, "price": 100.0, "reason": "entry"},
            {"kind": "sell", "symbol": "BBB", "shares": 5, "price": 100.0, "reason": "signal"},
        ],
    )
    journal = tmp_path / "forward" / "h2s" / "orders.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _event("fw-h2s-20261120-AAA-B", "submitting", symbol="AAA", side="B", qty=10),
        _event("fw-h2s-20261120-AAA-B", "filled", symbol="AAA", side="B", qty=10, price=101.0),
        _event("fw-h2s-20261120-BBB-S", "submitting", symbol="BBB", side="S", qty=5),
        _event("fw-h2s-20261120-BBB-S", "filled", symbol="BBB", side="S", qty=5, price=99.5),
    ]
    journal.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    text = report.build_report(
        tmp_path,
        "2026-11",
        as_of=day,
        engine_book_fn=_few_book,
    )
    assert "+100.0" in text
    assert "+50.0" in text
    assert "Execution" in text


def test_reference_numbers_labelled(tmp_path: Path) -> None:
    _registration(
        tmp_path,
        reference={
            "cagr": 0.431,
            "sharpe": 1.234,
            "max_drawdown": -0.123,
            "trades": 74,
            "win_rate": 0.5,
            "avg_r": 0.42,
        },
    )
    text = report.build_report(
        tmp_path,
        "2026-11",
        as_of=date(2026, 11, 23),
        engine_book_fn=_few_book,
    )
    assert "Research final year (reference only" in text
    assert "43.1%" in text
    assert "74" in text
    assert "not available" in text  # the H6-H4-3B reference block


def test_write_report_uses_given_directory(tmp_path: Path) -> None:
    _registration(tmp_path)
    path = report.write_report(
        tmp_path,
        "2026-11",
        as_of=date(2026, 11, 23),
        engine_book_fn=_few_book,
        report_dir=tmp_path / "docs",
    )
    assert path == tmp_path / "docs" / "2026-11.md"
    assert "**INTERIM, not a verdict**" in path.read_text(encoding="utf-8")


def _signal_table(symbol: str, signal_date: date, invalidation: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [signal_date],
            "symbol": [symbol],
            "entry": [True],
            "invalidation": [invalidation],
            "atr14": [1.0],
        },
        schema={
            "date": pl.Date,
            "symbol": pl.Utf8,
            "entry": pl.Boolean,
            "invalidation": pl.Float64,
            "atr14": pl.Float64,
        },
    )


def _book_row(text: str, label: str) -> list[str]:
    line = next(line for line in text.splitlines() if line.startswith(f"| {label} "))
    return [cell.strip() for cell in line.strip("|").split("|")]


def test_avg_r_uses_table(tmp_path: Path) -> None:
    _registration(tmp_path)
    start, as_of = date(2026, 11, 19), date(2026, 11, 23)
    trade = _trade("AAA", "2026-11-20", "2026-11-23", exit_price=110.0)
    trade["entry_signal_date"] = "2026-11-19"
    table = _signal_table("AAA", date(2026, 11, 19), invalidation=90.0)
    book = _book([trade], start, as_of, table)
    text = report.build_report(
        tmp_path,
        "2026-11",
        as_of=as_of,
        engine_book_fn=lambda short, s, e: book,
    )
    # (110 - 100) / (100 - 90) = 1.0; the empty-table path used to drop this trade.
    assert _book_row(text, "Engine")[3] == "1.00"
    assert "- avg_r_pos: pass" in text


def test_live_avg_r_uses_decision_signal_date(tmp_path: Path) -> None:
    _registration(tmp_path)
    day = date(2026, 11, 20)
    _decision(
        tmp_path,
        "h2s",
        day,
        {"AAA": 100.0},
        [
            {
                "kind": "buy",
                "symbol": "AAA",
                "shares": 10,
                "price": 100.0,
                "reason": "entry",
                "signal_date": "2026-11-19",
            }
        ],
    )
    journal = tmp_path / "forward" / "h2s" / "orders.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _event("fw-h2s-20261120-AAA-B", "submitting", symbol="AAA", side="B", qty=10),
        _event("fw-h2s-20261120-AAA-B", "filled", symbol="AAA", side="B", qty=10, price=100.0),
        _event("fw-h2s-20261123-AAA-S", "submitting", symbol="AAA", side="S", qty=10),
        _event("fw-h2s-20261123-AAA-S", "filled", symbol="AAA", side="S", qty=10, price=110.0),
    ]
    journal.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    table = _signal_table("AAA", date(2026, 11, 19), invalidation=90.0)
    book = {"trades": [], "equity": _equity(day, day), "table": table}
    text = report.build_report(
        tmp_path,
        "2026-11",
        as_of=date(2026, 11, 23),
        engine_book_fn=lambda short, s, e: book,
    )
    assert _book_row(text, "Live")[3] == "1.00"


def test_engine_equity_used_as_given(tmp_path: Path) -> None:
    start, as_of = date(2025, 1, 2), date(2026, 11, 23)
    _registration(tmp_path, start=start)
    days = us_clock.trading_days_between(start, as_of)
    equity = pl.DataFrame(
        {
            "date": days,
            "equity": [1_000_000.0 * (1.0 + 0.1 * i / (len(days) - 1)) for i in range(len(days))],
        },
        schema={"date": pl.Date, "equity": pl.Float64},
    )
    trades = _few_trades("h2s", start, as_of)
    book = {"trades": trades, "equity": equity, "table": pl.DataFrame()}
    text = report.build_report(
        tmp_path,
        "2026-11",
        as_of=as_of,
        engine_book_fn=lambda short, s, e: book,
    )
    expected = report._pct(curve_stats(equity)["cagr"])
    rebuilt = report._trade_curve(report._closed(trades), start, as_of, 1_000_000.0, {})
    rebuilt_cagr = report._pct(curve_stats(rebuilt)["cagr"])
    assert rebuilt_cagr != expected
    assert _book_row(text, "Engine")[4] == expected
