"""Historical rehearsal: replay the morning procedure over the final year.

The rehearsal is the main proof that the forward procedure reproduces the
research run. For every trading day ``d`` in ``[start, end)`` it does exactly
what the night and morning steps will do, using only data that existed on
``d``:

* inputs through ``d`` (research bars, research points filtered to
  ``known_date <= d``, the research eligibility rule over
  ``tradable_symbols``) — not the forward universe snapshots;
* one open-only bar for the next stored trading day ``t1`` built from the
  stored real open, plus the engine's PAD bar, and a table composed over those
  bars;
* a private engine store restricted to ``relevant_symbols`` and a full engine
  replay from ``start`` to the pad day; the actions dated ``t1`` are collected.

The reference is one extra engine run over ``[start, end]`` on the full real
bars with the research table rebuilt by ``forward_table``. The rebuilt table is
also compared with the frozen research table file (the one
``scripts/research/evaluate._latest_table`` would pick), because a table
mismatch means the forward inputs are not the research inputs.

Collected actions and reference trades are compared by
``(symbol, date, kind, shares, reason)``. Two kinds of reference trade are not
actions: ``end`` sales (an artefact of ending the run) and intraday stop-loss
exits (the open-only bar cannot show the day's low). Intraday stops are checked
separately against ``low <= entry_price * (1 - 0.15)`` on the full bars and
counted as ``intraday_stops``.

Nothing is written outside ``work_dir``; the research registry and store are
never touched.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import polars as pl

from app.forward.engine_run import (
    actions_on,
    build_engine_store,
    next_pad_day,
    open_only_rows,
    relevant_symbols,
    run_engine,
)
from app.forward.tables import build_inputs, candidates_on, forward_table
from app.research.bars import load_bars, tradable_symbols
from app.research.chan_replay import load_chan_points
from app.research.hypotheses import VARIANTS
from app.research.universe import UniverseParams, eligibility
from app.research.wyckoff.replay import (
    load_wyckoff_events,
    load_wyckoff_ranges,
)

STOP_LOSS_PCT = 0.15

ActionKey = tuple[str, str, str, int, str]


class Reference(NamedTuple):
    actions: Counter[ActionKey]
    intraday: list[dict]
    total_trades: int


# Same warm-up window the morning runner uses (Task 12).
WARMUP_DAYS = 400


def _field(trade: object, name: str, default: object = None) -> object:
    if isinstance(trade, dict):
        return trade.get(name, default)
    return getattr(trade, name, default)


def _as_date(value: object) -> date | None:
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


def _through(frame: pl.DataFrame, d: date) -> pl.DataFrame:
    """Rows whose knowledge date is on or before ``d``."""
    if frame.is_empty() or "known_date" not in frame.columns:
        return frame
    return frame.filter(pl.col("known_date") <= d)


def _latest_table(data_dir: Path, variant_id: str) -> Path | None:
    """The frozen table path chosen by ``scripts/research/evaluate._latest_table``."""
    directory = Path(data_dir) / "research" / "variants" / variant_id
    files = list(directory.glob("*.parquet")) if directory.is_dir() else []
    if not files:
        return None
    return max(files, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _tables_equal(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    keys = ["date", "symbol"]
    left = left.select(sorted(left.columns)).sort(keys)
    right = right.select(sorted(right.columns)).sort(keys)
    return left.equals(right)


def _quote_lookup(
    bars: pl.DataFrame, keys: set[tuple[str, date]]
) -> dict[tuple[str, date], tuple[float, float]]:
    """``(open, low)`` for the requested ``(symbol, date)`` pairs only."""
    if not keys:
        return {}
    wanted = pl.DataFrame(
        {
            "symbol": [symbol for symbol, _day in keys],
            "date": [day for _symbol, day in keys],
        },
        schema={"symbol": pl.String, "date": pl.Date},
    ).unique()
    quoted = bars.select("symbol", "date", "open", "low").join(
        wanted, on=["symbol", "date"], how="inner"
    )
    return {
        (str(row["symbol"]), row["date"]): (float(row["open"]), float(row["low"]))
        for row in quoted.iter_rows(named=True)
    }


def _store_day_rows(data_dir: Path, d: date) -> pl.DataFrame:
    """The full enriched rows stored for ``d``.

    ``open_only_rows`` copies every enriched column of the day-``d`` row, so the
    template comes from the enriched partition itself (the research loader only
    exposes a subset of the stored columns).
    """
    path = (
        Path(data_dir)
        / "kline_daily_enriched"
        / f"date={d.isoformat()}"
        / "part.parquet"
    )
    if not path.is_file():
        return pl.DataFrame()
    return pl.read_parquet(path)


def _reference_outcomes(
    result: object, bars: pl.DataFrame, start: date, end: date
) -> Reference:
    """Split the reference trades into comparable actions and intraday stops."""
    trades = list(getattr(result, "trades", None) or [])

    exits: list[dict] = []
    for trade in trades:
        symbol = str(_field(trade, "symbol", ""))
        shares = round(float(_field(trade, "shares", 0.0) or 0.0))
        entry_date = _as_date(_field(trade, "entry_date"))
        exit_date = _as_date(_field(trade, "exit_date"))
        reason = str(_field(trade, "exit_reason", "") or "")

        if entry_date is not None and start <= entry_date <= end:
            exits.append(
                {
                    "kind": "buy",
                    "symbol": symbol,
                    "date": entry_date,
                    "shares": shares,
                    "reason": "entry",
                }
            )

        if exit_date is None or not (start <= exit_date <= end):
            continue
        if reason == "end":
            continue
        exits.append(
            {
                "kind": "sell",
                "symbol": symbol,
                "date": exit_date,
                "shares": shares,
                "reason": reason,
                "exit_price": float(_field(trade, "exit_price", 0.0) or 0.0),
                "entry_price": float(_field(trade, "entry_price", 0.0) or 0.0),
            }
        )

    quote_keys = {
        (row["symbol"], row["date"])
        for row in exits
        if row["kind"] == "sell" and row["reason"] == "stop_loss"
    }
    quotes = _quote_lookup(bars, quote_keys)

    actions: Counter[ActionKey] = Counter()
    intraday: list[dict] = []
    for row in exits:
        if row["kind"] == "buy":
            actions[
                (row["symbol"], row["date"].isoformat(), "buy", row["shares"], "entry")
            ] += 1
            continue

        quote = quotes.get((row["symbol"], row["date"]))
        is_intraday = (
            row["reason"] == "stop_loss"
            and quote is not None
            and abs(row["exit_price"] - round(quote[0], 4)) > 1e-9
        )
        if is_intraday:
            stop_price = row["entry_price"] * (1.0 - STOP_LOSS_PCT)
            low = quote[1]
            intraday.append(
                {
                    "symbol": row["symbol"],
                    "date": row["date"],
                    "low": low,
                    "stop_price": stop_price,
                    "low_ok": low <= stop_price,
                }
            )
            continue

        actions[
            (
                row["symbol"],
                row["date"].isoformat(),
                "sell",
                row["shares"],
                row["reason"],
            )
        ] += 1

    return Reference(actions=actions, intraday=intraday, total_trades=len(trades))


# Open exits the full-bar engine replaces with a same-day intraday stop: it runs
# its risk exits before the scheduled exits at the open (engine.py:2040-2101).
_OPEN_EXIT_REASONS = frozenset({"max_hold", "signal"})


def compare_actions(
    collected: Counter,
    expected: Counter,
    intraday: list[dict],
) -> tuple[list[str], list[str]]:
    """Compare rehearsal actions with the reference; return ``(differences, overridden)``.

    ``overridden`` lists open exits (max hold or exit signal) that the morning
    procedure sells at the open while the full-bar engine records an intraday
    stop for the same symbol and day, because the frozen engine checks risk
    exits first. That is the engine's ordering, not a rule mismatch.
    """
    differences: list[str] = []
    overridden: list[str] = []
    stops = {(row["symbol"], row["date"].isoformat()) for row in intraday}

    for row in intraday:
        if not row["low_ok"]:
            differences.append(
                "intraday stop without a low through the stop: "
                f"{row['symbol']} {row['date'].isoformat()} "
                f"low={row['low']:.4f} stop={row['stop_price']:.4f}"
            )

    for key in sorted(set(collected) | set(expected)):
        mine = collected.get(key, 0)
        theirs = expected.get(key, 0)
        if mine == theirs:
            continue
        symbol, day, kind, shares, reason = key
        label = f"{kind} {symbol} {day} shares={shares} reason={reason}"
        if mine < theirs:
            differences.extend([f"missing {label}"] * (theirs - mine))
            continue
        extra = mine - theirs
        if kind == "sell" and reason in _OPEN_EXIT_REASONS and (symbol, day) in stops:
            overridden.append(f"{symbol} {day} open {reason} vs intraday stop_loss")
            extra -= 1
        differences.extend([f"unexpected {label}"] * extra)

    return differences, overridden


def rehearse(
    data_dir: Path,
    variant_id: str,
    start: date,
    end: date,
    capital: float,
    work_dir: Path,
    *,
    limit_days: int | None = None,
) -> dict:
    """Replay the morning procedure day by day and compare it with the research run."""
    data_dir = Path(data_dir)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    bars = load_bars(data_dir)
    if bars.is_empty():
        raise ValueError(f"no research bars under {data_dir}")
    spy = load_bars(data_dir, symbols=["SPY"])
    chan = load_chan_points(data_dir)
    wyk_events = load_wyckoff_events(data_dir)
    wyk_ranges = load_wyckoff_ranges(data_dir)

    elig = eligibility(bars, tradable_symbols(data_dir), UniverseParams())

    full_inputs = build_inputs(bars, spy, chan, wyk_events, wyk_ranges)
    full_table, _log = forward_table(variant_id, full_inputs, elig, full_inputs.bars)

    differences: list[str] = []
    frozen_path = _latest_table(data_dir, variant_id)
    if frozen_path is None:
        differences.append(f"frozen research table missing for {variant_id}")
    elif not _tables_equal(full_table, pl.read_parquet(frozen_path)):
        differences.append(
            f"rebuilt table differs from frozen table {frozen_path.name}"
        )

    reference_symbols = sorted(set(bars["symbol"].to_list()) | {"SPY"})
    # Warm-up history before ``start`` (ATR, 20-day dollar volume, SPY 200-day),
    # as the research run had the whole store; the engine still trades from ``start``.
    store_start = start - timedelta(days=WARMUP_DAYS)
    reference_store = build_engine_store(
        data_dir, work_dir / "reference", reference_symbols, store_start, end, None
    )
    reference_result = run_engine(
        reference_store, full_table, start, end, capital
    )
    reference = _reference_outcomes(reference_result, bars, start, end)

    days = [
        day
        for day in bars["date"].unique().sort().to_list()
        if start <= day <= end
    ]

    collected: Counter[ActionKey] = Counter()
    held: set[str] = set()
    days_run = 0
    last_t1: date | None = None

    for index in range(max(len(days) - 1, 0)):
        if limit_days is not None and days_run >= limit_days:
            break
        d = days[index]
        t1 = days[index + 1]

        bars_t = bars.filter(pl.col("date") <= d)
        spy_t = spy.filter(pl.col("date") <= d)
        inputs = build_inputs(
            bars_t,
            spy_t,
            _through(chan, d),
            _through(wyk_events, d),
            _through(wyk_ranges, d),
        )
        signals = VARIANTS[variant_id](inputs)

        t1_rows = bars.filter(pl.col("date") == t1)
        opens = {
            str(row["symbol"]): float(row["open"])
            for row in t1_rows.iter_rows(named=True)
        }
        pad_day = next_pad_day(t1)
        extra_rows = open_only_rows(
            _store_day_rows(data_dir, d), opens, t1, pad_day
        )
        if extra_rows.is_empty():
            bars_input = inputs.bars
        else:
            extra_atr = extra_rows.with_columns(
                pl.lit(None, dtype=pl.Float64).alias("atr14")
            ).select(inputs.bars.columns)
            bars_input = pl.concat([inputs.bars, extra_atr], how="vertical")

        table, _log = forward_table(variant_id, inputs, elig, bars_input)
        candidates = candidates_on(signals.entries, elig, d)
        symbols = relevant_symbols(table, start, held, candidates)
        store = build_engine_store(
            data_dir,
            work_dir / f"day={d.isoformat()}",
            sorted(set(symbols) | {"SPY"}),
            store_start,
            pad_day,
            extra_rows,
        )
        result = run_engine(store, table, start, pad_day, capital)
        actions = actions_on(result, t1)

        for action in actions:
            collected[
                (
                    action.symbol,
                    t1.isoformat(),
                    action.kind,
                    action.shares,
                    action.reason,
                )
            ] += 1
            if action.kind == "buy":
                held.add(action.symbol)
            else:
                held.discard(action.symbol)

        days_run += 1
        last_t1 = t1

    cutoff = last_t1 if last_t1 is not None else end
    expected = Counter(
        {
            key: count
            for key, count in reference.actions.items()
            if date.fromisoformat(key[1]) <= cutoff
        }
    )
    intraday = [row for row in reference.intraday if row["date"] <= cutoff]

    found, overridden = compare_actions(collected, expected, intraday)
    differences.extend(found)

    return {
        "days": days_run,
        "reference_trades": reference.total_trades,
        "rehearsal_trades": sum(collected.values()),
        "differences": differences,
        "intraday_stops": len(intraday),
        "open_exit_vs_intraday_stop": overridden,
    }


__all__ = ["rehearse"]