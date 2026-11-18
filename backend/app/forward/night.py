"""Night preparation (N1-N4): data, points, tables, relevant symbols, parity.

The night step never sends orders. It freezes what the next morning may decide
from: the universe snapshot, a readiness record, the extended signals, each
strategy's composed table, the relevant symbol list, the input hashes and the
night parity result.

Readiness is the only gate here: when a required symbol has no bar on ``t`` the
night record says ``ready: false`` with the reason and the morning records
``no_plan``. Parity differences are recorded on their decision dates; a
``logic_mismatch`` stops new entries in the morning through ``days.statuses_on``.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.forward.book import LiveBook
from app.forward.config import LEADS, forward_dir
from app.forward.data import (
    check_readiness,
    forward_tradable,
    history_hashes,
    load_forward_bars,
    preserve_bars,
    snapshot_universe,
)
from app.forward.days import record, statuses_on
from app.forward.engine_run import build_engine_store, relevant_symbols, run_engine
from app.forward.orders import Journal
from app.forward.parity import compare
from app.forward.registration import load_registration, require_pins
from app.forward.signals import extend_points, load_points_through, verify_sample
from app.forward.tables import (
    build_inputs,
    candidates_on,
    forward_eligibility,
    forward_table,
)
from app.market_clock import us_clock
from app.research.hypotheses import VARIANTS

WARMUP_DAYS = 400
SAMPLE_SIZE = 20

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
    """Stable sha256 of a composed table (the common night/morning recipe)."""
    if table.is_empty():
        payload = ""
    elif "date" in table.columns and "symbol" in table.columns:
        payload = table.sort(["date", "symbol"]).write_csv()
    else:
        payload = table.write_csv()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _night_path(data_dir: Path, short: str, t: date) -> Path:
    return forward_dir(data_dir) / short / "night" / f"{t.isoformat()}.json"

def _parity_path(data_dir: Path, short: str, t: date) -> Path:
    return forward_dir(data_dir) / short / "parity" / f"{t.isoformat()}.json"

def _eligibility_path(data_dir: Path, t: date) -> Path:
    return forward_dir(data_dir) / "night" / f"{t.isoformat()}-elig.parquet"

def _atomic_parquet(frame: pl.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        frame.write_parquet(tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()

def _save_eligibility(data_dir: Path, t: date, elig: pl.DataFrame) -> Path:
    """Write the one full-universe eligibility frame shared by both strategies."""
    target = _eligibility_path(data_dir, t)
    _atomic_parquet(
        elig.select(["symbol", "date", "eligible", "rank"]), target
    )
    return target

def _capital(reg: dict, lead: str) -> float:
    return float(reg["strategies"][lead]["backtest"]["initial_capital"])

def _held_books(data_dir: Path, reg: dict) -> dict[str, LiveBook]:
    books: dict[str, LiveBook] = {}
    for lead, short in LEADS.items():
        journal = Journal(forward_dir(data_dir) / short / "orders.jsonl")
        books[short] = LiveBook.from_journal(short, _capital(reg, lead), journal.events())
    return books

def _eligible_symbols(elig: pl.DataFrame, t: date) -> list[str]:
    if elig.is_empty():
        return []
    rows = elig.filter((pl.col("date") == t) & pl.col("eligible"))
    return sorted(set(rows["symbol"].to_list()))

def _load_decisions(data_dir: Path, short: str, through: date) -> dict[date, dict]:
    directory = forward_dir(data_dir) / short / "decision"
    decisions: dict[date, dict] = {}
    if not directory.is_dir():
        return decisions
    for path in sorted(directory.glob("*.json")):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day > through:
            continue
        decisions[day] = json.loads(path.read_text(encoding="utf-8"))
    return decisions

def _current_hash_map(
    data_dir: Path,
    short: str,
    decisions: dict[date, dict],
    bars: pl.DataFrame,
) -> dict[date, str]:
    """Current input hash per decision date, without recomposing every table.

    A decision's saved hash is the night table hash of its previous trading day.
    The current hash is the same value when the stored night record's per-symbol
    history hashes still match today's bars. Any changed history yields a
    different value, which ``parity.compare`` classifies as ``data_revision``.
    """
    hashes: dict[date, str] = {}
    for day, saved in decisions.items():
        try:
            night_day = us_clock.previous_trading_day(day)
        except ValueError:
            continue
        path = _night_path(data_dir, short, night_day)
        if not path.is_file():
            continue
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        old_hashes = old.get("history_hashes") or {}
        saved_hash = saved.get("input_hash")
        if not saved_hash:
            continue
        symbols = sorted(old_hashes)
        current = history_hashes(bars, night_day + timedelta(days=1))
        if all(current.get(symbol) == old_hashes.get(symbol) for symbol in symbols):
            hashes[day] = str(saved_hash)
        else:
            payload = json.dumps(
                {symbol: current.get(symbol) for symbol in symbols}, sort_keys=True
            )
            hashes[day] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return hashes

def _record_mismatches(
    data_dir: Path, short: str, outcome: dict[str, Any]
) -> None:
    for status in ("logic_mismatch", "data_revision"):
        grouped: dict[date, list[str]] = defaultdict(list)
        for item in outcome.get(status, []):
            try:
                day = date.fromisoformat(str(item.get("date"))[:10])
            except (TypeError, ValueError):
                continue
            symbol = item.get("symbol") or item.get("saved_hash") or ""
            field = item.get("field") or "input"
            grouped[day].append(f"{symbol}:{field}")
        for day, reasons in sorted(grouped.items()):
            record(data_dir, short, day, status, "; ".join(sorted(set(reasons))))

def _degraded_dates(
    data_dir: Path, short: str, decisions: dict[date, dict]
) -> set[date]:
    """Decision dates whose morning was not a full plan."""
    degraded: set[date] = set()
    for day in decisions:
        statuses = statuses_on(data_dir, short, day)
        if any(
            status in statuses for status in ("no_plan", "missed_open", "no_open")
        ):
            degraded.add(day)
    return degraded

def _run_parity(
    data_dir: Path,
    short: str,
    *,
    table: pl.DataFrame,
    symbols: list[str],
    start: date,
    t: date,
    capital: float,
    bars: pl.DataFrame,
) -> dict[str, Any]:
    decisions = _load_decisions(data_dir, short, t)
    if symbols:
        with tempfile.TemporaryDirectory(prefix=f"forward-parity-{short}-") as work:
            store = build_engine_store(
                data_dir,
                Path(work) / "store",
                symbols,
                start - timedelta(days=WARMUP_DAYS),
                t,
                None,
            )
            result = run_engine(store, table, start, t, capital)
            trades = list(getattr(result, "trades", None) or [])
    else:
        trades = []

    hashes = _current_hash_map(data_dir, short, decisions, bars)
    outcome = compare(
        trades,
        decisions,
        hashes,
        degraded_dates=_degraded_dates(data_dir, short, decisions),
    )
    payload = {
        "date": t.isoformat(),
        "short": short,
        "result": outcome,
        "engine_trades": len(trades),
        "decisions": len(decisions),
    }
    _atomic_json(_parity_path(data_dir, short, t), payload)
    _record_mismatches(data_dir, short, outcome)
    return payload

def _is_last_trading_day_of_week(t: date) -> bool:
    try:
        following = us_clock.next_trading_day(t)
    except ValueError:
        return False
    return following.isocalendar()[:2] != t.isocalendar()[:2]

def _not_ready_payload(
    t: date,
    short: str,
    lead: str,
    *,
    reason: str,
    pins: dict,
    held: list[str],
    candidates: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "date": t.isoformat(),
        "short": short,
        "variant_id": lead,
        "ready": False,
        "reason": reason,
        "held": held,
        "candidates": candidates or [],
        "relevant_symbols": sorted(set(held) | set(candidates or [])),
        "history_hashes": {},
        "table_hash": "",
        "pins": pins,
    }

def run_night(
    data_dir: Path,
    t: date,
    *,
    repo_root: Path,
    snapshot_fn=None,
    workers: int = 1,
) -> dict[str, Any]:
    """Prepare the forward record for the morning of the next trading day."""
    data_dir = Path(data_dir)
    reg = load_registration(data_dir)
    require_pins(reg, repo_root, data_dir)
    pins = dict(reg.get("pins") or {})
    start = date.fromisoformat(str(reg["start_date"])[:10])

    snapshot_universe(data_dir, t)
    books = _held_books(data_dir, reg)
    held_by_short = {short: sorted(book.lots) for short, book in books.items()}
    all_held = sorted({symbol for lots in held_by_short.values() for symbol in lots})
    preserve_bars(data_dir, all_held, t)

    bars = load_forward_bars(data_dir, end=t)
    elig = forward_eligibility(data_dir, bars, t)
    _save_eligibility(data_dir, t, elig)
    eligible = _eligible_symbols(elig, t)
    readiness = check_readiness(
        data_dir,
        t,
        sorted(set(all_held) | {"SPY"}),
        eligible,
    )

    if not readiness.ready:
        reason = (
            f"missing bars: {', '.join(readiness.missing)}; "
            f"coverage {readiness.coverage:.2%}"
        )
        records: dict[str, Any] = {}
        for lead, short in LEADS.items():
            payload = _not_ready_payload(
                t,
                short,
                lead,
                reason=reason,
                pins=pins,
                held=held_by_short.get(short, []),
            )
            payload.update(
                {
                    "missing": list(readiness.missing),
                    "coverage": readiness.coverage,
                    "checked_at": readiness.checked_at,
                }
            )
            _atomic_json(_night_path(data_dir, short, t), payload)
            records[short] = payload
        return {
            "date": t.isoformat(),
            "ready": False,
            "reason": reason,
            "strategies": records,
        }

    extension = extend_points(
        data_dir,
        sorted(set(eligible) | set(all_held)),
        t,
        snapshot_fn=snapshot_fn,
        workers=workers,
    )
    if extension.get("failed"):
        reason = "point extension failed: " + ", ".join(extension["failed"])
        records = {}
        for lead, short in LEADS.items():
            payload = _not_ready_payload(
                t,
                short,
                lead,
                reason=reason,
                pins=pins,
                held=held_by_short.get(short, []),
            )
            _atomic_json(_night_path(data_dir, short, t), payload)
            records[short] = payload
        return {
            "date": t.isoformat(),
            "ready": False,
            "reason": reason,
            "strategies": records,
        }

    chan, events, ranges = load_points_through(data_dir, t)
    all_symbols = sorted(set(forward_tradable(data_dir, t)) | set(all_held) | {"SPY"})
    bars = load_forward_bars(data_dir, symbols=all_symbols, end=t)
    spy = bars.filter(pl.col("symbol") == "SPY")

    records = {}
    for lead, short in LEADS.items():
        held = set(held_by_short.get(short, []))
        inputs = build_inputs(bars, spy, chan, events, ranges)
        signals = VARIANTS[lead](inputs)
        candidates = candidates_on(signals.entries, elig, t)
        table, _log = forward_table(lead, inputs, elig, bars)
        relevant = relevant_symbols(table, start, held, candidates)

        relevant_bars = (
            bars.filter(pl.col("symbol").is_in(relevant)) if relevant else bars.head(0)
        )
        hashes = (
            history_hashes(relevant_bars, t + timedelta(days=1)) if relevant else {}
        )
        payload = {
            "date": t.isoformat(),
            "short": short,
            "variant_id": lead,
            "ready": True,
            "reason": "",
            "held": sorted(held),
            "candidates": candidates,
            "relevant_symbols": relevant,
            "history_hashes": hashes,
            "table_hash": _table_hash(table),
            "pins": pins,
            "readiness": {
                "coverage": readiness.coverage,
                "checked_at": readiness.checked_at,
            },
        }
        _atomic_json(_night_path(data_dir, short, t), payload)

        parity_symbols = relevant or sorted(set(table["symbol"].to_list()))
        _run_parity(
            data_dir,
            short,
            table=table,
            symbols=parity_symbols,
            start=start,
            t=t,
            capital=_capital(reg, lead),
            bars=bars,
        )
        records[short] = payload

    if _is_last_trading_day_of_week(t):
        pool = sorted(set(eligible))
        if pool:
            picker = random.Random(t.toordinal())
            sample = picker.sample(pool, min(SAMPLE_SIZE, len(pool)))
        else:
            sample = []
        sample = sorted(set(sample) | set(all_held))
        if sample:
            differences = verify_sample(
                data_dir, sample, t, snapshot_fn=snapshot_fn
            )
            if differences:
                for short in records:
                    record(
                        data_dir,
                        short,
                        t,
                        "replay_mismatch",
                        ", ".join(differences),
                    )

    return {
        "date": t.isoformat(),
        "ready": True,
        "reason": "",
        "strategies": records,
    }

__all__ = ["run_night"]