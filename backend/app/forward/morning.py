"""Morning decision and execution (M1-M4).

The unchanged research engine decides the day: the real official opens become
one open-only bar for ``t1`` (plus a pad bar so the engine keeps running), the
engine runs over the registered window, and its actions dated ``t1`` become
orders. Sells use this strategy's live lot quantity (capped by the executor at
the broker's shares); buys use the engine's share count. A live lot the engine
no longer holds is sold as a catch-up exit. Entries stop on a missing or
unready night plan, an unacknowledged logic/replay mismatch, or a frozen
symbol. A pin mismatch stops the whole morning; the kill flag stops every order
(spec 6.8).

The clock is injected: ``now()`` returns an aware US/Eastern datetime and
``sleep`` advances it. Tests use a fake clock and never sleep for real.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.forward.book import (
    LiveBook,
    check_saved_matches_journal,
    save_book,
    saved_journal_events,
)
from app.forward.config import LEADS, forward_dir
from app.forward.data import load_forward_bars
from app.forward.days import record, rows_for, statuses_on
from app.forward.engine_run import (
    Action,
    actions_on,
    build_engine_store,
    engine_holdings,
    next_pad_day,
    open_only_rows,
    run_engine,
)
from app.forward.orders import Executor, Journal, OrderCapExceeded
from app.forward.registration import load_registration, require_pins
from app.forward.signals import load_points_through
from app.forward.tables import build_inputs, forward_eligibility, forward_table
from app.market_clock import us_clock

WARMUP_DAYS = 400
OPEN_START = time(9, 30)
OPEN_DEADLINE = time(9, 35)
ORDER_DEADLINE = time(10, 0)
RETRY_DEADLINE = time(10, 5)
ENTRY_CAP_FRACTION = 1.1
HOLD_STATUSES = frozenset({"logic_mismatch", "replay_mismatch"})

def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()

def _table_hash(table: pl.DataFrame) -> str:
    if table.is_empty():
        payload = ""
    elif "date" in table.columns and "symbol" in table.columns:
        payload = table.sort(["date", "symbol"]).write_csv()
    else:
        payload = table.write_csv()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def review(actions: list[Action]) -> list[Action]:
    """Reserved review hook (section 11); identity for now."""
    return list(actions)

def _action_dict(action: Action) -> dict[str, Any]:
    return {
        "kind": action.kind,
        "symbol": action.symbol,
        "shares": action.shares,
        "price": action.price,
        "reason": action.reason,
        "signal_date": action.signal_date,
    }

def _capital(reg: dict, lead: str) -> float:
    return float(reg["strategies"][lead]["backtest"]["initial_capital"])

def _held_books(data_dir: Path, reg: dict) -> dict[str, LiveBook]:
    books: dict[str, LiveBook] = {}
    for lead, short in LEADS.items():
        journal = Journal(forward_dir(data_dir) / short / "orders.jsonl")
        books[short] = LiveBook.from_journal(short, _capital(reg, lead), journal.events())
    return books

def _night_record(data_dir: Path, short: str, t: date) -> dict | None:
    path = forward_dir(data_dir) / short / "night" / f"{t.isoformat()}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def _eligibility_path(data_dir: Path, t: date) -> Path:
    return forward_dir(data_dir) / "night" / f"{t.isoformat()}-elig.parquet"

def _load_eligibility(
    data_dir: Path, t: date, t1: date, pad_day: date
) -> pl.DataFrame | None:
    """Read the night's full-universe eligibility, or ``None`` when missing.

    ``t1`` and the pad day never decide an entry: an entry dated there is not
    executable on ``t1``, so both are added as ineligible.
    """
    path = _eligibility_path(data_dir, t)
    if not path.is_file():
        return None
    try:
        elig = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError):
        return None
    required = {"symbol", "date", "eligible", "rank"}
    if not required <= set(elig.columns):
        return None
    return _with_future_rows(elig.select("symbol", "date", "eligible", "rank"), t1, pad_day)


def _with_future_rows(elig: pl.DataFrame, t1: date, pad_day: date) -> pl.DataFrame:
    """Add ``t1`` and pad-day rows as ineligible (entries there never fill on ``t1``)."""
    symbols = sorted(set(elig["symbol"].to_list()))
    if symbols:
        extended = pl.DataFrame(
            {
                "symbol": [symbol for symbol in symbols for _ in (t1, pad_day)],
                "date": [day for _ in symbols for day in (t1, pad_day)],
                "eligible": [False] * (2 * len(symbols)),
                "rank": [None] * (2 * len(symbols)),
            },
            schema={
                "symbol": pl.String,
                "date": pl.Date,
                "eligible": pl.Boolean,
                "rank": pl.Int64,
            },
        )
        elig = pl.concat([elig, extended], how="vertical")
    return elig

def _fallback_eligibility(data_dir: Path, t: date, t1: date, pad_day: date) -> pl.DataFrame:
    """Full-universe eligibility recomputed in the morning when the night file is missing.

    Eligibility ranks the top ``UniverseParams.top_n`` symbols by dollar volume
    across the whole forward universe, so it must never be computed on the
    restricted morning set (every symbol would rank inside the cap). This is
    slower than reading the night file but keeps the engine's history exact for
    held positions; new entries are blocked separately on such a day.
    """
    bars = load_forward_bars(data_dir, end=t)
    elig = forward_eligibility(data_dir, bars, t)
    return _with_future_rows(elig.select("symbol", "date", "eligible", "rank"), t1, pad_day)


def _deadline_seconds(clock: Any, t1: date, at: time) -> float:
    now = clock.now()
    target = datetime.combine(t1, at, tzinfo=now.tzinfo)
    return float(clock.monotonic() + max(0.0, target.timestamp() - now.timestamp()))

def _poll_opens(
    quotes: Any,
    symbols: list[str],
    t1: date,
    clock: Any,
) -> dict[str, float]:
    """Read official opens from 09:30 until all are present or 09:35.

    A snapshot whose ``update_time`` is before 09:30 ET on ``t1`` has not seen
    today's session yet (its ``open_price`` can still be the previous day's),
    so it is ignored.
    """
    wanted = sorted(set(symbols))
    if not wanted:
        return {}

    now = clock.now()
    open_at = datetime.combine(t1, OPEN_START, tzinfo=now.tzinfo)
    deadline = datetime.combine(t1, OPEN_DEADLINE, tzinfo=now.tzinfo)
    if now < open_at:
        clock.sleep(open_at.timestamp() - now.timestamp())
        now = clock.now()
    session_start = open_at.timestamp()

    opens: dict[str, float] = {}
    while True:
        snapshot = quotes.snapshot(wanted) or {}
        for symbol, row in snapshot.items():
            if not isinstance(row, dict):
                continue
            try:
                price = float(row.get("open_price"))
                stamp = float(row.get("update_time"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(stamp) and stamp >= session_start):
                continue
            if math.isfinite(price) and price > 0:
                opens[str(symbol)] = price
        if len(opens) == len(wanted):
            break
        now = clock.now()
        if now >= deadline:
            break
        remaining = deadline.timestamp() - now.timestamp()
        if remaining <= 0:
            break
        before = clock.now()
        clock.sleep(min(5.0, remaining))
        if clock.now() <= before:
            break
    return opens

def _engine_equity(result: Any, capital: float) -> float:
    curve = getattr(result, "equity_curve", None) or []
    if curve:
        last = curve[-1]
        value = last.get("value", last.get("equity")) if isinstance(last, dict) else None
        try:
            parsed = float(value)
            if math.isfinite(parsed):
                return parsed
        except (TypeError, ValueError):
            pass
    stats = getattr(result, "stats", None) or {}
    for key in ("final_equity", "equity"):
        try:
            return float(stats[key])
        except (KeyError, TypeError, ValueError):
            continue
    return float(capital)

def _scan_day_rows(glob: str, t: date, symbols: list[str]) -> pl.DataFrame:
    try:
        return (
            pl.scan_parquet(glob, hive_partitioning=False)
            .filter((pl.col("date") == t) & pl.col("symbol").is_in(symbols))
            .collect()
        )
    except Exception:  # missing or unreadable partition set
        return pl.DataFrame()

def _full_day_t(data_dir: Path, t: date, symbols: list[str]) -> pl.DataFrame:
    """Full enriched day-``t`` rows (the engine store carries every column).

    The store is the primary source; ``forward/bars`` is the fallback for a
    symbol whose store rows were pruned after ``preserve_bars`` copied them.
    """
    source_glob = str(data_dir / "kline_daily_enriched" / "date=*" / "part.parquet")
    forward_glob = str(data_dir / "forward" / "bars" / "date=*" / "part.parquet")
    frames = [
        frame
        for frame in (
            _scan_day_rows(source_glob, t, symbols),
            _scan_day_rows(forward_glob, t, symbols),
        )
        if not frame.is_empty()
    ]
    if not frames:
        return pl.DataFrame()
    combined = frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal_relaxed")
    return combined.unique(subset=["symbol"], keep="first", maintain_order=True).sort(
        "symbol"
    )

def _align_extra(extra: pl.DataFrame, data_dir: Path) -> pl.DataFrame:
    """Give the open-only rows the store's full column set."""
    glob = str(data_dir / "kline_daily_enriched" / "date=*" / "part.parquet")
    forward_glob = str(data_dir / "forward" / "bars" / "date=*" / "part.parquet")
    schema = None
    for candidate in (glob, forward_glob):
        try:
            schema = pl.scan_parquet(candidate, hive_partitioning=False).collect_schema()
            break
        except Exception:  # try the next source; no store means keep the frame
            continue
    if schema is None:
        return extra
    missing = [
        pl.lit(None, dtype=schema[name]).alias(name)
        for name in schema.names()
        if name not in extra.columns
    ]
    if missing:
        extra = extra.with_columns(missing)
    present = [name for name in schema.names() if name in extra.columns]
    return extra.select(present)

def _compose(
    data_dir: Path,
    lead: str,
    t: date,
    t1: date,
    pad_day: date,
    elig: pl.DataFrame | None,
    symbols: list[str],
    opens: dict[str, float],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    load_symbols = sorted(set(symbols) | {"SPY"})
    bars = load_forward_bars(data_dir, symbols=load_symbols, end=t)
    if bars.is_empty():
        empty = pl.DataFrame()
        return empty, empty
    bars_t = bars.filter(pl.col("date") == t)
    present = set(bars_t["symbol"].to_list()) if not bars_t.is_empty() else set()
    usable_opens = {symbol: price for symbol, price in opens.items() if symbol in present}
    full_t = _full_day_t(data_dir, t, load_symbols)
    row_source = full_t if not full_t.is_empty() else bars_t
    extra = open_only_rows(row_source, usable_opens, t1, pad_day)
    if extra.is_empty():
        table_bars = bars
        store_extra = extra
    else:
        store_extra = _align_extra(extra, data_dir)
        table_extra = extra.select(
            [column for column in bars.columns if column in extra.columns]
        )
        table_bars = pl.concat([bars, table_extra], how="vertical_relaxed")
    spy = table_bars.filter(pl.col("symbol") == "SPY")
    if elig is None:
        elig = _fallback_eligibility(data_dir, t, t1, pad_day)
    chan, events, ranges = load_points_through(data_dir, t)
    inputs = build_inputs(table_bars, spy, chan, events, ranges)
    table, _log = forward_table(lead, inputs, elig, table_bars)
    return table, store_extra

def _order_key(short: str, t1: date, symbol: str, side: str) -> str:
    return f"fw-{short}-{t1:%Y%m%d}-{symbol}-{side}"

def _submit_sells(
    data_dir: Path,
    short: str,
    t1: date,
    actions: list[Action],
    book: LiveBook,
    executor: Executor,
) -> set[str]:
    """Sell each lot named by ``actions``; the executor caps at the broker's shares."""
    attempted: set[str] = set()
    for action in actions:
        lot = book.lots.get(action.symbol)
        if lot is None:
            continue
        key = _order_key(short, t1, action.symbol, "S")
        try:
            outcome = executor.submit(key, action.symbol, "S", lot.shares)
        except OrderCapExceeded as exc:
            record(data_dir, short, t1, "no_plan", f"order cap: {exc}")
            continue
        except Exception as exc:  # one symbol must not stop the other exits
            record(
                data_dir,
                short,
                t1,
                "exit_incomplete",
                f"{action.symbol} sell failed: {type(exc).__name__}: {exc}",
            )
            continue
        if outcome == "skipped":
            record(
                data_dir,
                short,
                t1,
                "reconcile_mismatch",
                f"{action.symbol}: the book holds {lot.shares} but the broker has no unclaimed shares",
            )
        attempted.add(action.symbol)
    return attempted

def _submit_buys(
    data_dir: Path,
    short: str,
    t1: date,
    actions: list[Action],
    book: LiveBook,
    executor: Executor,
    *,
    engine_equity: float,
    opens: dict[str, float],
) -> list[Action]:
    cap = ENTRY_CAP_FRACTION * engine_equity / 10
    book_equity = book.equity(opens)
    open_notional = sum(
        lot.shares * opens.get(lot.symbol, lot.entry_price)
        for lot in book.lots.values()
    )
    submitted: list[Action] = []
    for action in actions:
        notional = float(action.price) * int(action.shares)
        if notional > cap:
            record(
                data_dir,
                short,
                t1,
                "no_plan",
                f"entry cap: {action.symbol} notional {notional:.2f} > {cap:.2f}",
            )
            continue
        if open_notional + notional > book_equity:
            record(
                data_dir,
                short,
                t1,
                "no_plan",
                f"open notional cap: {action.symbol} would exceed live equity "
                f"{book_equity:.2f}",
            )
            continue
        key = _order_key(short, t1, action.symbol, "B")
        try:
            executor.submit(key, action.symbol, "B", int(action.shares))
        except OrderCapExceeded as exc:
            record(data_dir, short, t1, "no_plan", f"order cap: {exc}")
            continue
        except Exception as exc:  # one symbol must not stop the other entries
            record(
                data_dir,
                short,
                t1,
                "no_plan",
                f"{action.symbol} buy failed: {type(exc).__name__}: {exc}",
            )
            continue
        submitted.append(action)
        open_notional += notional
    return submitted

def engine_entry_prices(data_dir: Path, short: str) -> dict[str, float]:
    """Latest recorded engine buy price per symbol still held by ``short``.

    The stop watch uses the engine's entry price, not the live fill, so engine
    and live share the same -15% line. A symbol whose live entry failed has no
    lot and therefore no price.
    """
    data_dir = Path(data_dir)
    reg = load_registration(data_dir)
    lead = next((name for name, value in LEADS.items() if value == short), None)
    capital = _capital(reg, lead) if lead is not None else 0.0
    journal = Journal(forward_dir(data_dir) / short / "orders.jsonl")
    book = LiveBook.from_journal(short, capital, journal.events())

    directory = forward_dir(data_dir) / short / "decision"
    prices: dict[str, float] = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for action in payload.get("actions") or []:
                if action.get("kind") != "buy":
                    continue
                try:
                    price = float(action.get("price"))
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    prices[str(action.get("symbol"))] = price
    return {
        symbol: prices[symbol]
        for symbol in sorted(book.lots)
        if symbol in prices
    }


# ── entry holds ─────────────────────────────────────────────────


def _entry_hold(data_dir: Path, short: str, t1: date) -> str | None:
    """A recorded logic/replay mismatch stops entries until the user acknowledges it.

    The acknowledgement is ``forward/<short>/ack.txt`` holding an ISO date:
    mismatches recorded on or before that date are cleared (spec 6.7: entries
    stop "until the user looks").
    """
    ack_path = forward_dir(data_dir) / short / "ack.txt"
    ack: date | None = None
    if ack_path.is_file():
        try:
            ack = date.fromisoformat(ack_path.read_text(encoding="utf-8").strip()[:10])
        except ValueError:
            ack = None
    open_rows = []
    for row in rows_for(data_dir, short):
        if row.get("status") not in HOLD_STATUSES:
            continue
        try:
            day = date.fromisoformat(str(row.get("date"))[:10])
        except ValueError:
            continue
        if day < t1 and (ack is None or day > ack):
            open_rows.append((day, str(row.get("status")), str(row.get("reason", ""))))
    if not open_rows:
        return None
    day, status, reason = max(open_rows)
    return (
        f"{status} on {day.isoformat()} ({reason}); entries stay off until "
        f"forward/{short}/ack.txt holds {day.isoformat()} or later"
    )


def _frozen_symbols(data_dir: Path, t: date) -> set[str]:
    """Symbols the last reconciliation froze (unexplained broker/book difference)."""
    frozen: set[str] = set()
    for short in LEADS.values():
        path = forward_dir(data_dir) / short / "reconcile" / f"{t.isoformat()}.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        frozen.update(str(symbol) for symbol in payload.get("frozen_symbols") or [])
    return frozen


def _check_saved_book(data_dir: Path, short: str, capital: float, t1: date) -> None:
    """The saved book must equal a rebuild from the journal events it was saved from."""
    book_path = forward_dir(data_dir) / short / "book.json"
    if not book_path.is_file():
        return
    events = Journal(forward_dir(data_dir) / short / "orders.jsonl").events()
    count = saved_journal_events(book_path)
    if count is not None:
        events = events[:count]
    rebuilt = LiveBook.from_journal(short, capital, events)
    if not check_saved_matches_journal(book_path, rebuilt):
        record(
            data_dir,
            short,
            t1,
            "reconcile_mismatch",
            "saved book.json differs from a rebuild of the order journal",
        )


# ── decision ────────────────────────────────────────────────────


def _decide(
    data_dir: Path,
    lead: str,
    short: str,
    *,
    reg: dict,
    start: date,
    t: date,
    t1: date,
    pad_day: date,
    book: LiveBook,
    night: dict | None,
    night_elig: pl.DataFrame | None,
    opens: dict[str, float],
    pins: dict,
) -> dict[str, Any]:
    """Run the engine for one strategy and save its decision record."""
    capital = _capital(reg, lead)
    held = set(book.lots)
    ready = bool(night and night.get("ready"))
    candidates = list(night.get("candidates") or []) if (night and ready) else []

    planned = set(held) | set(candidates)
    for symbol in sorted(planned - set(opens)):
        record(data_dir, short, t1, "no_open", f"{symbol} had no open by 09:35")

    buy_block: str | None = None
    if night is None:
        buy_block = f"no night record for {t.isoformat()}"
    elif not ready:
        buy_block = str(night.get("reason") or "night was not ready")
    elif night_elig is None:
        buy_block = f"no eligibility file for {t.isoformat()}"
    else:
        buy_block = _entry_hold(data_dir, short, t1)
    if buy_block is not None:
        record(data_dir, short, t1, "no_plan", buy_block)

    symbols = set(held) | set(opens)
    if night:
        symbols.update(night.get("relevant_symbols") or [])
        symbols.update(candidates)

    table, extra = _compose(
        data_dir, lead, t, t1, pad_day, night_elig, sorted(symbols), opens
    )
    engine_held: set[str] | None = None
    if table.is_empty() or not symbols:
        actions: list[Action] = []
        engine_equity = float(capital)
    else:
        store_symbols = sorted(symbols | set(table["symbol"].to_list()) | set(opens))
        with tempfile.TemporaryDirectory(prefix=f"forward-morning-{short}-") as work:
            store = build_engine_store(
                data_dir,
                Path(work) / "store",
                store_symbols,
                start - timedelta(days=WARMUP_DAYS),
                pad_day,
                extra,
            )
            engine_result = run_engine(store, table, start, pad_day, capital)
        actions = review(actions_on(engine_result, t1))
        engine_equity = _engine_equity(engine_result, capital)
        engine_held = engine_holdings(engine_result, t1)

    # A live lot the engine no longer holds (a failed exit, a missed stop, a
    # late morning): the engine sold it in its own run and no action will ever
    # name it again, so it is sold now.
    catch_up: list[Action] = []
    if engine_held is not None:
        sold = {action.symbol for action in actions if action.kind == "sell"}
        for symbol, lot in sorted(book.lots.items()):
            if symbol in engine_held or symbol in sold or lot.entry_date >= t1:
                continue
            catch_up.append(
                Action(
                    kind="sell",
                    symbol=symbol,
                    shares=int(lot.shares),
                    price=float(opens.get(symbol, 0.0)),
                    reason="catch_up",
                    signal_date="",
                )
            )
    if catch_up:
        names = ", ".join(action.symbol for action in catch_up)
        record(
            data_dir,
            short,
            t1,
            "exit_delayed",
            f"catch-up sell of {names}: the engine no longer holds them",
        )

    decision = {
        "date": t1.isoformat(),
        "short": short,
        "actions": [_action_dict(action) for action in actions],
        "catch_up": [_action_dict(action) for action in catch_up],
        "input_hash": (
            str(night.get("table_hash"))
            if night and night.get("table_hash")
            else _table_hash(table)
        ),
        "opens": opens,
        "engine_equity": engine_equity,
        "pins": pins,
    }
    decision_path = forward_dir(data_dir) / short / "decision" / f"{t1.isoformat()}.json"
    _atomic_json(decision_path, decision)
    return {
        "capital": capital,
        "book": book,
        "actions": actions,
        "catch_up": catch_up,
        "buy_block": buy_block,
        "engine_equity": engine_equity,
        "result": {
            "decision": str(decision_path),
            "actions": len(actions),
            "catch_up": len(catch_up),
            "orders": {},
            "engine_equity": engine_equity,
        },
    }


def run_morning(
    data_dir: Path,
    t1: date,
    *,
    broker: Any,
    quotes: Any,
    executor_factory: Callable[..., Any],
    clock: Any,
    dry_run: bool,
    repo_root: Path,
) -> dict[str, Any]:
    """Decide and (unless ``dry_run``) execute the morning of ``t1``.

    Both strategies are decided first, then the 10:00 deadline is checked once,
    every order of both strategies is sent, and only then are they polled, so a
    slow first strategy never pushes the second past the deadline. One
    strategy's failure is recorded and does not stop the other.
    """
    data_dir = Path(data_dir)
    reg = load_registration(data_dir)
    require_pins(reg, repo_root, data_dir)
    pins = dict(reg.get("pins") or {})
    start = date.fromisoformat(str(reg["start_date"])[:10])
    t = us_clock.previous_trading_day(t1)
    pad_day = next_pad_day(t1)
    stop_path = forward_dir(data_dir) / "STOP"

    result: dict[str, Any] = {
        "date": t1.isoformat(),
        "night_date": t.isoformat(),
        "dry_run": bool(dry_run),
        "strategies": {},
    }

    journals = {
        short: Journal(forward_dir(data_dir) / short / "orders.jsonl")
        for short in LEADS.values()
    }
    executors: dict[str, Any] = (
        {}
        if dry_run
        else {
            short: executor_factory(broker, journals[short], clock=clock)
            for short in LEADS.values()
        }
    )

    for lead, short in LEADS.items():
        _check_saved_book(data_dir, short, _capital(reg, lead), t1)

    # Orders an earlier day left open (crash, late restart, unpolled stop).
    for short, executor in executors.items():
        resolver = getattr(executor, "resolve_stale", None)
        if not callable(resolver):
            continue
        try:
            settled = resolver(t1)
        except Exception as exc:
            record(
                data_dir,
                short,
                t1,
                "reconcile_mismatch",
                f"order history unreadable: {type(exc).__name__}: {exc}",
            )
            continue
        if settled:
            result.setdefault("settled", {})[short] = settled

    books = _held_books(data_dir, reg)
    nights = {short: _night_record(data_dir, short, t) for short in books}
    night_elig = _load_eligibility(data_dir, t, t1, pad_day)

    wanted: set[str] = set()
    for short, book in books.items():
        wanted.update(book.lots)
        if nights.get(short):
            wanted.update(nights[short].get("relevant_symbols") or [])
            if nights[short].get("ready"):
                wanted.update(nights[short].get("candidates") or [])
    opens = _poll_opens(quotes, sorted(wanted), t1, clock)
    frozen = _frozen_symbols(data_dir, t)

    plans: dict[str, dict[str, Any]] = {}
    for lead, short in LEADS.items():
        try:
            plans[short] = _decide(
                data_dir,
                lead,
                short,
                reg=reg,
                start=start,
                t=t,
                t1=t1,
                pad_day=pad_day,
                book=books[short],
                night=nights.get(short),
                night_elig=night_elig,
                opens=opens,
                pins=pins,
            )
        except Exception as exc:  # the other strategy still trades
            reason = f"decision failed: {type(exc).__name__}: {exc}"
            record(data_dir, short, t1, "no_plan", reason)
            result["strategies"][short] = {"error": reason}

    after_order_deadline = clock.now().time() >= ORDER_DEADLINE
    attempted_sells: dict[str, set[str]] = {}
    for short, plan in plans.items():
        strategy_result = plan["result"]
        result["strategies"][short] = strategy_result
        actions = plan["actions"]
        sells = [action for action in actions if action.kind == "sell"] + plan["catch_up"]
        buys = [action for action in actions if action.kind == "buy"]

        if dry_run:
            strategy_result["sent"] = "dry_run"
            continue
        if stop_path.exists():
            record(data_dir, short, t1, "no_plan", "kill flag present")
            strategy_result["sent"] = "kill_flag"
            continue
        executor = executors[short]
        if after_order_deadline:
            if buys:
                record(data_dir, short, t1, "missed_open", "M3 started after 10:00")
            if sells:
                record(data_dir, short, t1, "exit_delayed", "M3 started after 10:00")
            try:
                # Settle (and cancel) today's orders an earlier run left open.
                executor.poll_until(clock.monotonic())
            except Exception as exc:
                record(
                    data_dir,
                    short,
                    t1,
                    "reconcile_mismatch",
                    f"settling today's orders failed: {type(exc).__name__}: {exc}",
                )
            strategy_result["sent"] = "after_deadline"
            continue

        if plan["buy_block"] is not None:
            buys = []
        blocked = sorted({action.symbol for action in buys} & frozen)
        if blocked:
            record(
                data_dir,
                short,
                t1,
                "no_plan",
                f"frozen after a reconcile difference on {t.isoformat()}: {', '.join(blocked)}",
            )
            buys = [action for action in buys if action.symbol not in frozen]
        attempted_sells[short] = (
            _submit_sells(data_dir, short, t1, sells, plan["book"], executor)
            if sells
            else set()
        )
        if buys:
            _submit_buys(
                data_dir,
                short,
                t1,
                buys,
                plan["book"],
                executor,
                engine_equity=plan["engine_equity"],
                opens=opens,
            )
        strategy_result["sent"] = "live"

    for at in (ORDER_DEADLINE, RETRY_DEADLINE):
        for short in attempted_sells:
            try:
                executors[short].poll_until(_deadline_seconds(clock, t1, at))
            except Exception as exc:
                record(
                    data_dir,
                    short,
                    t1,
                    "reconcile_mismatch",
                    f"order polling failed: {type(exc).__name__}: {exc}",
                )

    for short, attempted in attempted_sells.items():
        plan = plans[short]
        events = journals[short].events()
        book = LiveBook.from_journal(short, plan["capital"], events)
        book_path = forward_dir(data_dir) / short / "book.json"
        save_book(book_path, book, journal_events=len(events))
        for symbol in sorted(attempted):
            if symbol in book.lots:
                record(
                    data_dir,
                    short,
                    t1,
                    "exit_incomplete",
                    f"{symbol} still holds {book.lots[symbol].shares} shares",
                )
        plan["result"]["book"] = str(book_path)

    for short in plans:
        if not statuses_on(data_dir, short, t1):
            record(data_dir, short, t1, "ok", "dry_run" if dry_run else "")

    return result

__all__ = ["engine_entry_prices", "review", "run_morning"]
