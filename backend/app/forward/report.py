"""Monthly forward-test report.

The report is a plain renderer over the forward records and the injected
engine book. It never recomputes the research rules: the engine book supplies
the engine's trades, its exact equity curve (``equity_frame`` shape) and the
composed signal table it traded from ``engine_book_fn``; the research final-year
numbers are read from the registration's optional ``reference`` block, labelled
reference only. A missing day is itself an error in the report.
"""

from __future__ import annotations

import calendar
import json
import math
import os
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.forward import config, registration
from app.forward.book import load_book
from app.forward.config import forward_dir
from app.forward.data import forward_tradable, load_forward_bars
from app.forward.days import missing_days
from app.forward.stopwatch import STOP_FRACTION
from app.market_clock import us_clock
from app.research.baselines import b0_spy, buy_and_hold_curve
from app.research.evaluate import gate_checks
from app.research.metrics import curve_stats, summarize

EngineBookFn = Callable[[str, date, date], dict]
RandomCagrsFn = Callable[[str, date, date], list[float]]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_REPORT_DIR = _REPO_ROOT / "docs" / "forward"
_EMPTY = pl.DataFrame()


# ── small formatting helpers ─────────────────────────────────
def _num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}"


def _pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "—"
    return f"{number * 100:.{digits}f}%"


def _bps(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:+.1f}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _field(item: object, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + int(months)
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _parse_month(month: str) -> tuple[date, date]:
    try:
        year_text, month_text = month.split("-", 1)
        year, month_number = int(year_text), int(month_text)
        first = date(year, month_number, 1)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"month must be YYYY-MM, got {month!r}") from exc
    last = date(year, month_number, calendar.monthrange(year, month_number)[1])
    return first, last


# ── records ──────────────────────────────────────────────────
def _load_decisions(data_dir: Path, short: str, start: date, end: date) -> dict[date, dict]:
    directory = forward_dir(data_dir) / short / "decision"
    decisions: dict[date, dict] = {}
    if not directory.is_dir():
        return decisions
    for path in sorted(directory.glob("*.json")):
        day = _as_date(path.stem)
        if day is None or day < start or day > end:
            continue
        try:
            decisions[day] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return decisions


def _key_parts(key: str) -> tuple[str, date | None, str, str]:
    parts = str(key or "").split("-")
    if len(parts) < 5 or parts[0] != "fw":
        return "", None, "", ""
    short = parts[1]
    raw_date = parts[2]
    day = None
    if len(raw_date) == 8 and raw_date.isdigit():
        try:
            day = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        except ValueError:
            day = None
    symbol = parts[3]
    side = parts[4][:1].upper()
    return short, day, symbol, side


def _journal_fills(data_dir: Path, short: str, start: date, end: date) -> list[dict]:
    """One fill per order key, from the append-only order journal."""
    path = forward_dir(data_dir) / short / "orders.jsonl"
    if not path.is_file():
        return []
    latest: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = str(event.get("key") or "")
        key_short, day, symbol, side = _key_parts(key)
        if key_short != short or day is None or day < start or day > end:
            continue
        bag = latest.setdefault(key, {"key": key})
        for name in ("symbol", "side", "qty", "dealt_qty", "dealt_avg_price"):
            if event.get(name) is not None:
                bag[name] = event[name]
        if not bag.get("symbol"):
            bag["symbol"] = symbol
        if not bag.get("side"):
            bag["side"] = side

    fills: list[dict] = []
    for key, bag in latest.items():
        _, day, symbol, side = _key_parts(key)
        try:
            qty = int(bag.get("dealt_qty") or 0)
            price = float(bag.get("dealt_avg_price") or 0.0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0.0 or day is None:
            continue
        fills.append(
            {
                "key": key,
                "date": day,
                "symbol": str(bag.get("symbol") or symbol),
                "side": str(bag.get("side") or side).upper()[:1],
                "shares": qty,
                "price": price,
            }
        )
    return fills


def _entry_signal_date(decisions: dict[date, dict], day: date, symbol: str) -> str | None:
    """The entry signal date from the decision record of the fill date."""
    record = decisions.get(day) or {}
    for action in record.get("actions") or []:
        if str(_field(action, "kind", "")) != "buy":
            continue
        if str(_field(action, "symbol", "")) != symbol:
            continue
        signal_date = _field(action, "signal_date")
        if signal_date is not None:
            return str(signal_date)[:10]
    return None


def _live_trades(fills: list[dict], decisions: dict[date, dict] | None = None) -> list[dict]:
    """FIFO round trips from the live fills; sells beyond the lots are ignored."""
    decisions = decisions or {}
    open_lots: dict[str, list[dict]] = {}
    trades: list[dict] = []
    for fill in sorted(fills, key=lambda item: (item["date"], item["key"])):
        symbol = fill["symbol"]
        if fill["side"] == "B":
            open_lots.setdefault(symbol, []).append(
                {
                    "shares": int(fill["shares"]),
                    "price": float(fill["price"]),
                    "date": fill["date"],
                }
            )
            continue
        remaining = int(fill["shares"])
        lots = open_lots.setdefault(symbol, [])
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, int(lot["shares"]))
            trades.append(
                {
                    "symbol": symbol,
                    "entry_date": lot["date"],
                    "exit_date": fill["date"],
                    "shares": take,
                    "entry_price": lot["price"],
                    "exit_price": fill["price"],
                    "exit_reason": "stop" if str(fill["key"]).endswith("-stop") else "order",
                    "entry_signal_date": _entry_signal_date(decisions, lot["date"], symbol),
                }
            )
            remaining -= take
            lot["shares"] -= take
            if lot["shares"] <= 0:
                lots.pop(0)
    return trades


def _load_book(data_dir: Path, short: str):
    path = forward_dir(data_dir) / short / "book.json"
    try:
        return load_book(path)
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


# ── metrics ──────────────────────────────────────────────────
def _close_lookup(bars: pl.DataFrame | None) -> dict[tuple[str, date], float]:
    if bars is None or bars.is_empty() or "close" not in bars.columns:
        return {}
    lookup: dict[tuple[str, date], float] = {}
    for row in bars.select("symbol", "date", "close").iter_rows(named=True):
        day = _as_date(row["date"])
        close = row.get("close")
        if day is None or close is None:
            continue
        lookup[(str(row["symbol"]), day)] = float(close)
    return lookup


def _trade_curve(
    trades: list[dict],
    start: date,
    end: date,
    capital: float,
    closes: dict[tuple[str, date], float] | None = None,
) -> pl.DataFrame:
    """Daily equity from trade cash flows, marking open lots at close or entry."""
    days = us_clock.trading_days_between(start, end)
    if not days:
        return pl.DataFrame(schema={"date": pl.Date, "equity": pl.Float64})

    closes = closes or {}
    rows: list[dict] = []
    for trade in trades:
        entry_day = _as_date(_field(trade, "entry_date"))
        exit_day = _as_date(_field(trade, "exit_date"))
        if entry_day is None or entry_day > end:
            continue
        if exit_day is not None and exit_day < start:
            continue
        rows.append(
            {
                "symbol": str(_field(trade, "symbol", "")),
                "entry": entry_day,
                "exit": exit_day,
                "shares": int(float(_field(trade, "shares", 0) or 0)),
                "entry_price": float(_field(trade, "entry_price", 0.0) or 0.0),
                "exit_price": float(_field(trade, "exit_price", 0.0) or 0.0),
            }
        )

    cash = float(capital)
    open_lots: dict[int, dict] = {}
    values: list[float] = []
    first = days[0]
    for index, day in enumerate(days):
        for lot in rows:
            if lot["shares"] <= 0 or lot["entry_price"] <= 0:
                continue
            # Trades already open when the window starts are entered on day one.
            if (lot["entry"] <= first and index == 0) or lot["entry"] == day:
                cash -= lot["shares"] * lot["entry_price"]
                open_lots[id(lot)] = lot

        for lot in list(open_lots.values()):
            if lot["exit"] == day:
                cash += lot["shares"] * lot["exit_price"]
                open_lots.pop(id(lot), None)

        mark = cash
        for lot in open_lots.values():
            price = closes.get((lot["symbol"], day), lot["entry_price"])
            mark += lot["shares"] * price
        values.append(float(mark))

    return pl.DataFrame(
        {"date": days, "equity": values}, schema={"date": pl.Date, "equity": pl.Float64}
    )


def _summary(
    trades: list[dict],
    equity: pl.DataFrame,
    table: pl.DataFrame,
    n_trials: int,
    var_sr_trials: float,
) -> dict:
    """Exactly research's metric call, so R joins the same signal table."""
    return summarize(trades, equity, table, n_trials, var_sr_trials, slots=10)


def _closed(trades: list[dict]) -> list[dict]:
    return [trade for trade in trades if _as_date(_field(trade, "exit_date")) is not None]


# ── baselines ────────────────────────────────────────────────
def _baselines(
    data_dir: Path,
    start: date,
    end: date,
    capital: float,
    bars: pl.DataFrame | None,
) -> tuple[dict | None, dict | None, pl.DataFrame | None]:
    """Forward-universe buy-and-hold, SPY, and the bars used (or ``None``)."""
    if bars is None:
        try:
            bars = load_forward_bars(data_dir, start=start, end=end)
        except Exception:  # no store yet (or an unreadable one): not available
            return None, None, None
    if bars is None or bars.is_empty():
        return None, None, None

    try:
        universe = sorted(forward_tradable(data_dir, end))
    except FileNotFoundError:
        universe = sorted(
            str(symbol) for symbol in bars["symbol"].unique().to_list() if symbol != "SPY"
        )

    bh_curve = buy_and_hold_curve(bars, universe, start, end, capital=capital)
    bh = curve_stats(bh_curve) if not bh_curve.is_empty() else None

    spy_curve = b0_spy(bars.filter(pl.col("symbol") == "SPY"), start, end, capital=capital)
    spy = curve_stats(spy_curve) if not spy_curve.is_empty() else None
    return bh, spy, bars


def _random_percentile(
    random_cagrs: list[float],
    cagr: Any,
) -> tuple[float | None, float | None]:
    if not random_cagrs:
        return None, None
    rand95 = float(np.percentile(np.asarray(random_cagrs, dtype=float), 95))
    if cagr is None:
        return rand95, None
    below = sum(1 for value in random_cagrs if float(value) < float(cagr))
    return rand95, below / len(random_cagrs) * 100.0


# ── execution ────────────────────────────────────────────────
def _official_open(record: dict | None, symbol: str, side: str) -> float | None:
    if not record:
        return None
    opens = record.get("opens") or {}
    value = opens.get(symbol) if isinstance(opens, dict) else None
    if value is not None:
        try:
            price = float(value)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            return price
    wanted = "buy" if side == "B" else "sell"
    for action in record.get("actions") or []:
        if str(_field(action, "symbol", "")) != symbol:
            continue
        if str(_field(action, "kind", "")) != wanted:
            continue
        try:
            price = float(_field(action, "price", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def _execution_bps(
    fills: list[dict],
    decisions: dict[date, dict],
) -> dict[str, dict[str, float | int | None]]:
    by_side: dict[str, list[float]] = {"B": [], "S": []}
    for fill in fills:
        if str(fill["key"]).endswith("-stop"):
            continue  # stop fills are compared with the stop line, not the open
        official = _official_open(decisions.get(fill["date"]), fill["symbol"], fill["side"])
        if official is None or official <= 0:
            continue
        if fill["side"] == "B":
            value = (fill["price"] - official) / official * 10_000.0
        else:
            value = (official - fill["price"]) / official * 10_000.0
        by_side.setdefault(fill["side"], []).append(value)

    result: dict[str, dict[str, float | int | None]] = {}
    for side, values in by_side.items():
        if not values:
            result[side] = {"n": 0, "median": None, "p90": None}
            continue
        result[side] = {
            "n": len(values),
            "median": float(np.median(np.asarray(values, dtype=float))),
            "p90": float(np.percentile(np.asarray(values, dtype=float), 90)),
        }
    return result


def _stop_fills(
    fills: list[dict],
    decisions: dict[date, dict],
) -> list[dict]:
    rows: list[dict] = []
    for fill in fills:
        if not str(fill["key"]).endswith("-stop"):
            continue
        line = None
        for day in sorted((day for day in decisions if day <= fill["date"]), reverse=True):
            entry = _official_open(decisions[day], fill["symbol"], "B")
            if entry is not None and entry > 0:
                line = entry * STOP_FRACTION
                break
        rows.append(
            {
                "date": fill["date"],
                "symbol": fill["symbol"],
                "fill": fill["price"],
                "line": line,
                "bps": None if line is None else (fill["price"] - line) / line * 10_000.0,
            }
        )
    return rows


# ── days and parity ──────────────────────────────────────────
def _day_rows(data_dir: Path) -> list[dict]:
    path = forward_dir(data_dir) / "days.jsonl"
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _status_counts(
    data_dir: Path,
    short: str,
    start: date,
    end: date,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _day_rows(data_dir):
        if row.get("short") != short:
            continue
        day = _as_date(row.get("date"))
        if day is None or day < start or day > end:
            continue
        status = str(row.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _parity_counts(data_dir: Path, short: str, start: date, end: date) -> dict[str, int]:
    counts = {"logic_mismatch": 0, "data_revision": 0, "intraday_stops": 0}
    directory = forward_dir(data_dir) / short / "parity"
    if not directory.is_dir():
        return counts
    for path in directory.glob("*.json"):
        day = _as_date(path.stem)
        if day is None or day < start or day > end:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key in counts:
            value = record.get(key)
            if isinstance(value, list):
                counts[key] += len(value)
    return counts


# ── report sections ──────────────────────────────────────────
def _strategy_entry(reg: dict, lead: str) -> dict:
    entry = dict((reg.get("strategies") or {}).get(lead) or {})
    entry.setdefault("short", config.LEADS.get(lead, lead))
    return entry


def _ordered_strategies(reg: dict) -> list[tuple[str, dict]]:
    entries = reg.get("strategies") or {}
    result = [(lead, _strategy_entry(reg, lead)) for lead in config.LEADS if lead in entries]
    result.extend(
        (lead, dict(entry)) for lead, entry in entries.items() if lead not in config.LEADS
    )
    return result


def _metric_row(
    label: str,
    trades: list[dict],
    summary: dict | None,
) -> list[str]:
    if summary is None:
        return [label, "not available", "—", "—", "—", "—", "—"]
    return [
        label,
        str(len(trades)),
        _pct(summary.get("win_rate")),
        _num(summary.get("avg_r")),
        _pct(summary.get("cagr")),
        _num(summary.get("sharpe_ann")),
        _pct(summary.get("max_drawdown")),
    ]


def _reference_lines(entry: dict) -> list[str]:
    reference = entry.get("reference")
    if not isinstance(reference, dict):
        return ["- not available"]
    rows = [
        ["CAGR", _pct(reference.get("cagr"))],
        ["Sharpe", _num(reference.get("sharpe"))],
        ["Max drawdown", _pct(reference.get("max_drawdown"))],
        ["Trades", str(reference.get("trades", "—"))],
        ["Win rate", _pct(reference.get("win_rate"))],
        ["Average R", _num(reference.get("avg_r"))],
    ]
    return _table(["Metric", "Final-year research"], rows)


def _gate_lines(
    summary: dict | None,
    bh_cagr: float | None,
    random_cagrs: list[float],
    n_trials: int,
    var_sr_trials: float,
) -> list[str]:
    lines = [
        f"Gate (n_trials={n_trials}, var_sr_trials={var_sr_trials:g}):",
    ]
    if summary is None:
        lines.append("- not available")
        return lines
    checks = gate_checks(summary, bh_cagr, random_cagrs)
    for name, passed in checks.items():
        lines.append(f"- {name}: {'pass' if passed else 'fail'}")
    lines.append(f"- DSR: {_num(summary.get('dsr'))}")
    return lines


def build_report(
    data_dir: Path,
    month: str,
    *,
    as_of: date,
    engine_book_fn: EngineBookFn | None = None,
    random_cagrs_fn: RandomCagrsFn | None = None,
    bars: pl.DataFrame | None = None,
) -> str:
    """Render the monthly forward report as markdown.

    ``engine_book_fn(short, start, end)`` supplies the engine book: its trades
    (with ``entry_signal_date``), its exact equity curve shaped like
    ``app.research.evaluate.equity_frame`` and the composed signal table it
    traded. ``random_cagrs_fn`` optionally supplies random-entry CAGRs for the
    percentile; without it the percentile is reported as not available.
    Nothing here reads the real data directory beyond the forward records under
    ``data_dir``.
    """
    data_dir = Path(data_dir)
    month_start, month_end = _parse_month(month)
    reg = registration.load_registration(data_dir)
    sha = registration.registration_sha256(data_dir)
    start = _as_date(reg.get("start_date"))
    if start is None:
        raise ValueError("registration has no valid start_date")
    evaluation = reg.get("evaluation") or {}
    n_trials = int(evaluation.get("n_trials", 71))
    var_sr_trials = float(evaluation.get("var_sr_trials", 0.0))
    min_months = int(evaluation.get("min_months", 12))
    min_closed = int(evaluation.get("min_closed_trades", 100))
    capital = float(reg.get("capital_per_strategy", 1_000_000.0))
    strategies = _ordered_strategies(reg)

    if bars is None:
        try:
            bars = load_forward_bars(data_dir, start=start, end=as_of)
        except Exception:
            bars = None
    closes = _close_lookup(bars)

    # Collect every strategy's books before the header: the verdict depends on
    # the engine book's closed-trade count.
    books: list[dict] = []
    for lead, entry in strategies:
        short = str(entry.get("short") or config.LEADS.get(lead, lead))
        engine_book = engine_book_fn(short, start, as_of) if engine_book_fn is not None else {}
        engine_trades = list(engine_book.get("trades") or [])
        engine_equity = engine_book.get("equity")
        if not isinstance(engine_equity, pl.DataFrame):
            engine_equity = _EMPTY
        engine_table = engine_book.get("table")
        if not isinstance(engine_table, pl.DataFrame):
            engine_table = _EMPTY
        closed_engine = _closed(engine_trades)
        engine_summary = (
            _summary(closed_engine, engine_equity, engine_table, n_trials, var_sr_trials)
            if closed_engine
            else None
        )

        fills = _journal_fills(data_dir, short, start, as_of)
        decisions = _load_decisions(data_dir, short, start, as_of)
        live_trades = _live_trades(fills, decisions)
        book = _load_book(data_dir, short)
        live_capital = float(book.capital) if book is not None else capital
        curve_trades = list(live_trades)
        if book is not None:
            for lot in book.lots.values():
                curve_trades.append(
                    {
                        "symbol": lot.symbol,
                        "entry_date": lot.entry_date,
                        "exit_date": None,
                        "shares": lot.shares,
                        "entry_price": lot.entry_price,
                    }
                )
        live_curve = _trade_curve(curve_trades, start, as_of, live_capital, closes)
        live_summary = (
            _summary(live_trades, live_curve, engine_table, n_trials, var_sr_trials)
            if live_trades
            else None
        )

        books.append(
            {
                "lead": lead,
                "short": short,
                "entry": entry,
                "engine_trades": engine_trades,
                "closed_engine": closed_engine,
                "engine_summary": engine_summary,
                "live_trades": live_trades,
                "live_summary": live_summary,
                "book": book,
                "decisions": decisions,
                "fills": fills,
            }
        )

    eval_date = _add_months(start, min_months)
    enough_trades = bool(books) and all(len(book["closed_engine"]) >= min_closed for book in books)
    verdict = bool(books) and as_of >= eval_date and enough_trades
    header = "**VERDICT**" if verdict else "**INTERIM, not a verdict**"

    lines: list[str] = [
        f"# Forward test report — {month}",
        "",
        header,
        "",
        f"- Registration sha256: `{sha}`",
        f"- Start date: {start.isoformat()}",
        f"- Evaluation date: {eval_date.isoformat()} "
        f"(extended until every engine book has ≥ {min_closed} closed trades)",
        f"- As of: {as_of.isoformat()}",
        "",
        "## Pins",
        "",
    ]
    pins = reg.get("pins") or {}
    if not pins:
        lines.append("- not available")
    else:
        lines.extend(f"- {name}: `{value}`" for name, value in sorted(pins.items()))

    bh, spy, _bars = _baselines(data_dir, start, as_of, capital, bars)
    lines.extend(["", "## Baselines", "", f"Window: {start.isoformat()} → {as_of.isoformat()}", ""])
    baseline_rows = [
        [
            "Forward-universe equal-weight buy-and-hold",
            _pct(bh.get("cagr")) if bh else "not available",
            _num(bh.get("sharpe_ann")) if bh else "—",
            _pct(bh.get("max_drawdown")) if bh else "—",
        ],
        [
            "SPY (information only)",
            _pct(spy.get("cagr")) if spy else "not available",
            _num(spy.get("sharpe_ann")) if spy else "—",
            _pct(spy.get("max_drawdown")) if spy else "—",
        ],
    ]
    lines.extend(_table(["Baseline", "CAGR", "Sharpe", "Max drawdown"], baseline_rows))

    for book in books:
        lead = book["lead"]
        short = book["short"]
        entry = book["entry"]
        closed_engine = book["closed_engine"]
        engine_summary = book["engine_summary"]
        live_trades = book["live_trades"]
        live_summary = book["live_summary"]
        random_cagrs: list[float] = []
        random_note = "not available"
        if random_cagrs_fn is not None:
            try:
                random_cagrs = [float(value) for value in random_cagrs_fn(short, start, as_of)]
            except Exception:
                random_cagrs = []
            if random_cagrs:
                random_note = "available"

        rand95, percentile = _random_percentile(
            random_cagrs, engine_summary.get("cagr") if engine_summary else None
        )

        lines.extend(
            [
                "",
                f"## {lead} (`{short}`)",
                "",
                "Engine book is the verdict book; the live book shows execution on top.",
                "",
                *_table(
                    ["Book", "Trades", "Win rate", "Avg R", "CAGR", "Sharpe", "Max drawdown"],
                    [
                        _metric_row("Engine", closed_engine, engine_summary),
                        _metric_row("Live", live_trades, live_summary),
                    ],
                ),
                "",
                "Research final year (reference only; never pooled with forward data):",
                "",
                *_reference_lines(entry),
                "",
                "Random entry (same forward window):",
                f"- 95th percentile CAGR: {_pct(rand95) if rand95 is not None else random_note}",
                f"- Engine percentile: {_num(percentile, 1) + '%' if percentile is not None else random_note}",
                "",
                *_gate_lines(
                    engine_summary,
                    bh.get("cagr") if bh else None,
                    random_cagrs,
                    n_trials,
                    var_sr_trials,
                ),
            ]
        )

    # Execution section.
    all_fills: list[dict] = []
    all_decisions: dict[str, dict[date, dict]] = {}
    for book in books:
        all_fills.extend(book["fills"])
        all_decisions[book["short"]] = book["decisions"]
    execution_rows: list[list[str]] = []
    fill_by_short: dict[str, list[dict]] = {book["short"]: [] for book in books}
    for fill in all_fills:
        for book in books:
            if str(fill["key"]).startswith(f"fw-{book['short']}-"):
                fill_by_short[book["short"]].append(fill)
                break
    for short, fills in fill_by_short.items():
        bps = _execution_bps(fills, all_decisions.get(short, {}))
        for side, label in (("B", "Buy"), ("S", "Sell")):
            row = bps.get(side) or {"n": 0, "median": None, "p90": None}
            execution_rows.append(
                [
                    f"{short} {label}",
                    str(row["n"]),
                    _bps(row["median"]),
                    _bps(row["p90"]),
                ]
            )
    lines.extend(
        [
            "",
            "## Execution",
            "",
            "Live fill vs official open (signed bps; positive means paid above / sold below the open).",
            "",
            *_table(["Side", "N", "Median bps", "p90 bps"], execution_rows),
        ]
    )

    stop_rows: list[list[str]] = []
    for short, decisions in all_decisions.items():
        for stop in _stop_fills(fill_by_short.get(short, []), decisions):
            stop_rows.append(
                [
                    short,
                    stop["date"].isoformat(),
                    stop["symbol"],
                    _num(stop["fill"], 4),
                    _num(stop["line"], 4) if stop["line"] is not None else "not available",
                    _bps(stop["bps"]),
                ]
            )
    lines.extend(["", "Stop fills vs stop line:"])
    if stop_rows:
        lines.extend(_table(["Strategy", "Date", "Symbol", "Fill", "Stop line", "bps"], stop_rows))
    else:
        lines.append("- none")

    # Status counts and missing days.
    lines.extend(["", "## Days", ""])
    status_rows: list[list[str]] = []
    missing: list[str] = []
    parity_bits: list[str] = []
    for book in books:
        short = book["short"]
        window_start = max(start, month_start)
        window_end = min(as_of, month_end)
        counts = (
            _status_counts(data_dir, short, window_start, window_end)
            if window_start <= window_end
            else {}
        )
        for status in sorted(counts):
            status_rows.append([short, status, str(counts[status])])
        if window_start <= window_end:
            for day in missing_days(data_dir, short, window_start, window_end):
                missing.append(f"{day.isoformat()} ({short})")
        parity = _parity_counts(data_dir, short, window_start, window_end)
        parity_bits.append(
            f"{short}: logic_mismatch={parity['logic_mismatch']}, "
            f"data_revision={parity['data_revision']}, intraday_stops={parity['intraday_stops']}"
        )
    if status_rows:
        lines.extend(_table(["Strategy", "Status", "Count"], status_rows))
    else:
        lines.append("- no status records")
    lines.append("")
    if missing:
        lines.append("**ERROR: missing day(s):** " + ", ".join(missing))
    else:
        lines.append("No missing days.")
    lines.append("")
    lines.append("Parity: " + "; ".join(parity_bits))
    lines.append("")
    return "\n".join(lines)


def write_report(
    data_dir: Path,
    month: str,
    *,
    as_of: date,
    engine_book_fn: EngineBookFn | None = None,
    random_cagrs_fn: RandomCagrsFn | None = None,
    bars: pl.DataFrame | None = None,
    report_dir: Path | None = None,
) -> Path:
    """Write ``<report_dir>/<yyyy-mm>.md`` (default ``docs/forward``)."""
    _parse_month(month)  # validate before building a path
    text = build_report(
        data_dir,
        month,
        as_of=as_of,
        engine_book_fn=engine_book_fn,
        random_cagrs_fn=random_cagrs_fn,
        bars=bars,
    )
    directory = Path(report_dir) if report_dir is not None else _DEFAULT_REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{month}.md"
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


__all__ = ["build_report", "write_report"]
