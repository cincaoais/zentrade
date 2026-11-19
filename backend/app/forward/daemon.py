"""The forward runner's daily loop (spec section 5).

One trading day is: morning decision at 09:30, the -15% stop watch until 16:00,
reconciliation at 16:15, the night preparation at/after 18:00, then sleep to the
next trading day's 09:25. A raised step is written to ``forward/audit.jsonl``,
the affected day is recorded and the loop continues: a broken morning must not
silent-kill the stop watch or the next night.

The module never creates a real broker on its own in tests: ``broker_factory``
and ``quotes_factory`` are injectable, as is ``clock`` (``now()``/``sleep()``).
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.forward import registration
from app.forward.book import LiveBook, reconcile
from app.forward.broker import Quotes, SimBroker
from app.forward.cli_support import (
    SystemClock,
    acquire_runner_lock,
    append_audit,
    default_workers,
)
from app.forward.config import LEADS, forward_dir
from app.forward.days import record
from app.forward.morning import engine_entry_prices, run_morning
from app.forward.night import run_night
from app.forward.orders import Executor, Journal
from app.forward.stopwatch import StopWatch
from app.market_clock import us_clock

logger = logging.getLogger(__name__)

MORNING_OPEN = time(9, 30)
WAKE_BEFORE_OPEN = time(9, 25)
SESSION_CLOSE = time(16, 0)
RECONCILE_AT = time(16, 15)
NIGHT_AT = time(18, 0)
NIGHT_RETRY_S = 1800
NIGHT_DEADLINE = time(7, 0)

__all__ = ["reconcile_books", "run"]


# ---------------------------------------------------------------------------
# Books and journals
# ---------------------------------------------------------------------------


def _capital(reg: dict, lead: str) -> float:
    entry = (reg.get("strategies") or {}).get(lead) or {}
    return float(
        (entry.get("backtest") or {}).get("initial_capital")
        or reg.get("capital_per_strategy")
        or 0.0
    )


def load_books(data_dir: Path, reg: dict) -> dict[str, LiveBook]:
    """Rebuild every strategy's live book from its order journal."""
    books: dict[str, LiveBook] = {}
    for lead, short in LEADS.items():
        journal = Journal(forward_dir(data_dir) / short / "orders.jsonl")
        books[short] = LiveBook.from_journal(short, _capital(reg, lead), journal.events())
    return books


class _StrategyJournals:
    """One Executor journal that routes each event to ``forward/<short>/``.

    The stop watch runs both strategies in one loop but each strategy owns its
    order journal. The key's short id is the routing key, so a stop sell lands
    where the morning book rebuild and the report expect it.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def _path(self, key: Any) -> Path:
        parts = str(key or "").split("-")
        short = parts[1] if len(parts) > 1 and parts[1] in LEADS.values() else "_"
        return forward_dir(self.data_dir) / short / "orders.jsonl"

    def append(self, event: dict[str, Any]) -> None:
        Journal(self._path(event.get("key"))).append(event)

    def state(self, key: str) -> str | None:
        return Journal(self._path(key)).state(key)

    def events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for short in LEADS.values():
            events.extend(
                Journal(forward_dir(self.data_dir) / short / "orders.jsonl").events()
            )
        return events


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _reconcile_reason(outcome: dict[str, Any]) -> str:
    pieces: list[str] = []
    for mismatch in outcome.get("mismatches") or []:
        pieces.append(
            "{symbol}: broker={broker} books={books}".format(
                symbol=mismatch.get("symbol"),
                broker=mismatch.get("broker"),
                books=mismatch.get("books"),
            )
        )
    for issue in outcome.get("attention") or []:
        pieces.append(str(issue.get("issue") or issue))
    return "; ".join(pieces) or "reconcile difference"


def reconcile_books(
    data_dir: Path,
    day: date,
    *,
    broker: Any,
    reg: dict | None = None,
) -> dict[str, Any]:
    """Compare broker positions/cash with both live books and save the record."""
    data_dir = Path(data_dir)
    reg = reg if reg is not None else registration.load_registration(data_dir)
    books = load_books(data_dir, reg)
    positions = broker.positions()
    cash = broker.cash()
    # Adjustment ratios are not wired yet; pass {} so any quantity difference
    # is a mismatch and freezes the symbol.
    outcome = reconcile(list(books.values()), positions, cash, {})
    reason = _reconcile_reason(outcome)
    for short in books:
        payload = {
            "date": day.isoformat(),
            "short": short,
            "broker_cash": cash,
            "broker_positions": positions,
            **outcome,
        }
        _atomic_json(
            forward_dir(data_dir) / short / "reconcile" / f"{day.isoformat()}.json",
            payload,
        )
        if not outcome.get("ok"):
            record(data_dir, short, day, "reconcile_mismatch", reason)
    return outcome


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


def _at(day: date, clock_time: time, tzinfo: Any) -> datetime:
    return datetime.combine(day, clock_time).replace(tzinfo=tzinfo)


def _sleep_until(clock: Any, target: datetime) -> None:
    while True:
        now = clock.now()
        remaining = target.timestamp() - now.timestamp()
        if remaining <= 0:
            return
        chunk = min(remaining, 300.0)
        sleeper = getattr(clock, "sleep", None)
        if callable(sleeper):
            sleeper(chunk)
        else:
            _time.sleep(max(0.0, float(chunk)))


def _next_trading_day(day: date) -> date:
    try:
        return us_clock.next_trading_day(day)
    except ValueError:
        nxt = day + timedelta(days=1)
        while not us_clock.is_trading_day(nxt):
            nxt += timedelta(days=1)
        return nxt


def _close(obj: Any) -> None:
    closer = getattr(obj, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            logger.exception("closing forward daemon resource failed")


def _step(
    data_dir: Path,
    day: date,
    audit_step: str,
    function,
    error_status: str,
) -> Any:
    append_audit(
        data_dir,
        {"event": "step_start", "day": day.isoformat(), "step": audit_step},
    )
    try:
        result = function()
    except Exception as exc:
        logger.exception("forward daemon step %s failed", audit_step)
        append_audit(
            data_dir,
            {
                "event": "step_error",
                "day": day.isoformat(),
                "step": audit_step,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        for short in LEADS.values():
            record(
                data_dir,
                short,
                day,
                error_status,
                f"{audit_step} failed: {type(exc).__name__}: {exc}",
            )
        return None
    append_audit(
        data_dir,
        {"event": "step_ok", "day": day.isoformat(), "step": audit_step},
    )
    return result


def _executor_factory(broker: Any, journal: Any, *, clock: Any) -> Executor:
    return Executor(broker, journal, clock=clock)


def _run_day(
    data_dir: Path,
    day: date,
    *,
    dry_run: bool,
    repo_root: Path,
    clock: Any,
    broker: Any,
    quotes: Any,
    reg: dict,
) -> None:
    tzinfo = clock.now().tzinfo
    _sleep_until(clock, _at(day, MORNING_OPEN, tzinfo))

    _step(
        data_dir,
        day,
        "morning",
        lambda: run_morning(
            data_dir,
            day,
            broker=broker,
            quotes=quotes,
            executor_factory=_executor_factory,
            clock=clock,
            dry_run=dry_run,
            repo_root=repo_root,
        ),
        "no_plan",
    )

    def watch() -> None:
        books = load_books(data_dir, reg)
        prices = {
            short: engine_entry_prices(data_dir, short) for short in LEADS.values()
        }
        executor = Executor(broker, _StrategyJournals(data_dir), clock=clock)
        stopwatch = StopWatch(
            books=list(books.values()),
            quotes=quotes,
            executor=executor,
            engine_entry_prices=prices,
            clock=clock,
            dry_run=dry_run,
            stop_flag=forward_dir(data_dir) / "STOP",
        )
        stopwatch.run_until(_at(day, SESSION_CLOSE, tzinfo))
        errors = list(getattr(stopwatch, "errors", None) or [])
        if errors:
            # Ticks failed: a stop may have been missed while quotes were down.
            reason = f"{len(errors)} stop-watch error(s); first: {errors[0]}"
            for short in LEADS.values():
                record(data_dir, short, day, "stop_missed", reason)

    _step(data_dir, day, "stopwatch", watch, "stop_missed")

    _sleep_until(clock, _at(day, RECONCILE_AT, tzinfo))
    _step(
        data_dir,
        day,
        "reconcile",
        lambda: reconcile_books(data_dir, day, broker=broker, reg=reg),
        "reconcile_mismatch",
    )

    _sleep_until(clock, _at(day, NIGHT_AT, tzinfo))
    deadline = _at(_next_trading_day(day), NIGHT_DEADLINE, tzinfo)
    while True:
        result = _step(
            data_dir,
            day,
            "night",
            lambda: run_night(
                data_dir,
                day,
                repo_root=repo_root,
                workers=default_workers(),
            ),
            "no_plan",
        )
        if isinstance(result, dict) and result.get("ready"):
            break
        next_target = datetime.fromtimestamp(
            clock.now().timestamp() + NIGHT_RETRY_S, tz=tzinfo
        )
        if next_target.timestamp() >= deadline.timestamp():
            break
        _sleep_until(clock, next_target)


def run(
    data_dir: Path,
    *,
    dry_run: bool,
    repo_root: Path,
    clock: Any | None = None,
    broker_factory: Any | None = None,
    quotes_factory: Any | None = None,
) -> dict[str, Any]:
    """Run the forward timeline forever (until Ctrl-C writes an audit stop)."""
    data_dir = Path(data_dir)
    repo_root = Path(repo_root)
    clock = clock or SystemClock()
    broker_factory = broker_factory or (lambda account_id: SimBroker(account_id))
    quotes_factory = quotes_factory or (lambda: Quotes())

    reg = registration.load_registration(data_dir)
    account_id = int(reg["account_id"])

    with acquire_runner_lock(data_dir):
        # Spec 6.8: the registered account must be SIMULATE before any session.
        checker = broker_factory(account_id)
        try:
            check = getattr(checker, "check_account", None)
            if callable(check):
                check()
        finally:
            _close(checker)
        append_audit(data_dir, {"event": "start", "dry_run": bool(dry_run)})

        stopped = False
        try:
            while True:
                now = clock.now()
                day = now.date()
                tzinfo = now.tzinfo

                if not us_clock.is_trading_day(day):
                    _sleep_until(clock, _at(_next_trading_day(day), WAKE_BEFORE_OPEN, tzinfo))
                    continue

                if now.time() < WAKE_BEFORE_OPEN:
                    _sleep_until(clock, _at(day, WAKE_BEFORE_OPEN, tzinfo))
                    continue

                broker = broker_factory(account_id)
                quotes = quotes_factory()
                try:
                    _run_day(
                        data_dir,
                        day,
                        dry_run=dry_run,
                        repo_root=repo_root,
                        clock=clock,
                        broker=broker,
                        quotes=quotes,
                        reg=reg,
                    )
                finally:
                    _close(quotes)
                    _close(broker)

                _sleep_until(clock, _at(_next_trading_day(day), WAKE_BEFORE_OPEN, tzinfo))
        except KeyboardInterrupt:
            stopped = True
            append_audit(data_dir, {"event": "stop", "reason": "keyboard_interrupt"})
        finally:
            if not stopped:
                append_audit(data_dir, {"event": "stop", "reason": "exit"})

        return {"audit": str(forward_dir(data_dir) / "audit.jsonl"), "stopped": stopped}