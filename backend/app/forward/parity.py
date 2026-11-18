"""Night parity: the full-bar engine run must reproduce the morning's actions.

The morning engine (``app.forward.engine_run``) sees an open-only bar for
``t+1``, so it can decide open exits and entries but not an intraday -15% stop.
The night run (N4) replays the normal research backtest over the forward window
through ``t`` on real bars. Its trades dated on a decision day must equal the
actions saved in ``decision/<date>.json``; the only allowed differences are

* intraday stops, which only the full bar can show (listed separately and
  compared with the stop watch by the caller), and
* data revisions: the decision's saved ``input_hash`` differs from the current
  hash of that date's input. Those are recorded, never a halt.

Everything else is a ``logic_mismatch``: new entries stop until the user looks.
"""

from __future__ import annotations

from datetime import date, datetime

from app.forward.engine_run import Action
from app.strategy.builtin.research_signal_table import STOP_LOSS

_MISSING = object()

def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])

def _field(item: object, name: str, default: object = None) -> object:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)

def _date_text(value: object) -> str:
    return "" if value is None else str(value)[:10]

def decision_actions(record: dict) -> list[Action]:
    """The actions saved in a decision record, as :class:`Action` values.

    ``decision/<date>.json`` stores ``{"date", "actions", "input_hash"}`` where
    each action is an ``Action`` field dict. Task 12 writes the record with this
    shape and both it and night parity read it through this one function, so the
    two sides cannot drift apart.
    """
    raw_actions = _field(record, "actions", []) or []
    actions: list[Action] = []
    for raw in raw_actions:
        actions.append(
            Action(
                kind=str(_field(raw, "kind", "")),
                symbol=str(_field(raw, "symbol", "")),
                shares=round(float(_field(raw, "shares", 0.0) or 0.0)),
                price=float(_field(raw, "price", 0.0) or 0.0),
                reason=str(_field(raw, "reason", "")),
                signal_date=str(_field(raw, "signal_date", "") or ""),
            )
        )
    return actions

def _action_dict(action: Action) -> dict:
    return {
        "kind": action.kind,
        "symbol": action.symbol,
        "shares": action.shares,
        "price": action.price,
        "reason": action.reason,
        "signal_date": action.signal_date,
    }

def _sells_by_date(trades: list[dict]) -> dict[date, list[Action]]:
    result: dict[date, list[Action]] = {}
    for trade in trades:
        exit_date = _field(trade, "exit_date")
        reason = str(_field(trade, "exit_reason", ""))
        if exit_date is None or reason == "end":
            continue
        result.setdefault(_as_date(exit_date), []).append(
            Action(
                kind="sell",
                symbol=str(_field(trade, "symbol", "")),
                shares=round(float(_field(trade, "shares", 0.0) or 0.0)),
                price=float(_field(trade, "exit_price", 0.0) or 0.0),
                reason=reason,
                signal_date=_date_text(_field(trade, "exit_signal_date")),
            )
        )
    return result

def _buys_by_date(trades: list[dict]) -> dict[date, list[Action]]:
    result: dict[date, list[Action]] = {}
    for trade in trades:
        entry_date = _field(trade, "entry_date")
        if entry_date is None:
            continue
        result.setdefault(_as_date(entry_date), []).append(
            Action(
                kind="buy",
                symbol=str(_field(trade, "symbol", "")),
                shares=round(float(_field(trade, "shares", 0.0) or 0.0)),
                price=float(_field(trade, "entry_price", 0.0) or 0.0),
                reason="entry",
                signal_date=_date_text(_field(trade, "entry_signal_date")),
            )
        )
    return result

def _mismatch(
    day: date,
    action: Action,
    field: str,
    expected: object,
    actual: object,
) -> dict:
    return {
        "date": day.isoformat(),
        "kind": action.kind,
        "symbol": action.symbol,
        "field": field,
        "expected": expected,
        "actual": actual,
    }

def _same_price(left: float, right: float) -> bool:
    return round(float(left), 4) == round(float(right), 4)

def _sell_stop_lines(trades: list[dict]) -> dict[tuple[date, str], float]:
    """The engine's -15% stop line for every sell, keyed by ``(date, symbol)``."""
    lines: dict[tuple[date, str], float] = {}
    for trade in trades:
        exit_date = _field(trade, "exit_date")
        if exit_date is None or str(_field(trade, "exit_reason", "")) == "end":
            continue
        entry_price = float(_field(trade, "entry_price", 0.0) or 0.0)
        if entry_price > 0:
            lines[(_as_date(exit_date), str(_field(trade, "symbol", "")))] = (
                entry_price * (1 + STOP_LOSS)
            )
    return lines

def _at_stop_line(price: float, line: float | None) -> bool:
    """Whether an exit filled at exactly the stop line, i.e. during the day.

    An at-open stop fills at the open, which is at or below the line; an
    intraday stop fills at the line itself.
    """
    if line is None:
        return False
    return abs(float(price) - float(line)) <= max(1e-3, abs(float(line)) * 1e-9)

def _compare_day(
    day: date,
    expected: list[Action],
    actual: list[Action],
    stop_lines: dict[tuple[date, str], float],
) -> tuple[list[dict], list[Action]]:
    """Differences between one day's expected and actual actions.

    Returns ``(mismatches, intraday_stops)``. An actual sell with reason
    ``stop_loss`` that has no expected action can only be the intraday stop the
    open-only bar could not show (an at-open stop would have been a morning
    action); it is returned separately instead of as a mismatch.
    """
    expected_by_key = {(action.kind, action.symbol): action for action in expected}
    actual_by_key = {(action.kind, action.symbol): action for action in actual}
    mismatches: list[dict] = []
    intraday: list[Action] = []

    for key in sorted(set(expected_by_key) | set(actual_by_key)):
        wanted = expected_by_key.get(key)
        found = actual_by_key.get(key)
        if wanted is None:
            if (
                found is not None
                and found.kind == "sell"
                and found.reason == "stop_loss"
                and _at_stop_line(found.price, stop_lines.get((day, found.symbol)))
            ):
                intraday.append(found)
            else:
                mismatches.append(
                    _mismatch(day, found, "action", None, _action_dict(found))
                )
            continue
        if found is None:
            mismatches.append(_mismatch(day, wanted, "action", _action_dict(wanted), None))
            continue
        if wanted.shares != found.shares:
            mismatches.append(
                _mismatch(day, wanted, "shares", wanted.shares, found.shares)
            )
        if not _same_price(wanted.price, found.price):
            mismatches.append(
                _mismatch(day, wanted, "price", wanted.price, found.price)
            )
        if wanted.reason != found.reason:
            mismatches.append(
                _mismatch(day, wanted, "reason", wanted.reason, found.reason)
            )

    return mismatches, intraday

def _hash_for(hashes: dict[date, str], day: date) -> str | None:
    if day in hashes:
        return hashes[day]
    text = day.isoformat()
    if text in hashes:
        return hashes[text]
    return None

def _stop_dict(action: Action, day: date) -> dict:
    return {
        "date": day.isoformat(),
        "symbol": action.symbol,
        "shares": action.shares,
        "price": action.price,
        "reason": action.reason,
    }

def compare(
    engine_trades: list[dict],
    decisions: dict[date, dict],
    bars_hash_by_date: dict[date, str],
    degraded_dates: set[date] | frozenset[date] = frozenset(),
) -> dict:
    """Compare the night engine's trades with the saved morning decisions.

    ``engine_trades`` are the full-bar run's trades over the forward window
    through ``t``; ``decisions`` maps a decision date to its saved record
    (``{"date", "actions", "input_hash"}``) and ``bars_hash_by_date`` holds the
    current hash of each date's engine input. The result always has the keys
    ``logic_mismatch``, ``data_revision``, ``degraded_day`` and
    ``intraday_stops``.

    A mismatch on a date whose saved input hash differs from the current hash is
    classified as ``data_revision`` (one entry per date, with the offending
    mismatch details); every other mismatch is a ``logic_mismatch``. A mismatch
    on a date in ``degraded_dates`` (the morning had no full plan: ``no_plan``,
    ``missed_open`` or ``no_open``) is recorded under ``degraded_day`` instead:
    it never halts entries. Intraday stops are listed separately and never as
    mismatches.
    """
    sells = _sells_by_date(engine_trades)
    buys = _buys_by_date(engine_trades)
    stop_lines = _sell_stop_lines(engine_trades)
    degraded = {_as_date(day) for day in degraded_dates}

    normalized: dict[date, dict] = {}
    for key, record in decisions.items():
        normalized[_as_date(key)] = record

    logic_mismatch: list[dict] = []
    data_revision: list[dict] = []
    degraded_day: list[dict] = []
    intraday_stops: list[dict] = []

    for day in sorted(normalized):
        record = normalized[day]
        expected = decision_actions(record)
        actual = sells.get(day, []) + buys.get(day, [])
        mismatches, intraday = _compare_day(day, expected, actual, stop_lines)
        intraday_stops.extend(_stop_dict(action, day) for action in intraday)

        if day in degraded:
            if mismatches:
                degraded_day.append(
                    {"date": day.isoformat(), "mismatches": mismatches}
                )
            continue

        saved_hash = _field(record, "input_hash", _MISSING)
        current_hash = _hash_for(bars_hash_by_date, day)
        if (
            saved_hash is not _MISSING
            and saved_hash is not None
            and current_hash is not None
            and saved_hash != current_hash
        ):
            data_revision.append(
                {
                    "date": day.isoformat(),
                    "saved_hash": saved_hash,
                    "current_hash": current_hash,
                    "mismatches": mismatches,
                }
            )
        else:
            logic_mismatch.extend(mismatches)

    return {
        "logic_mismatch": logic_mismatch,
        "data_revision": data_revision,
        "degraded_day": degraded_day,
        "intraday_stops": intraday_stops,
    }

__all__ = ["compare", "decision_actions"]