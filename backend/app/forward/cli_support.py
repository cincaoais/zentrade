"""Helpers behind ``scripts/forward/run.py``.

The CLI is intentionally thin: every operation that can be reasoned about in a
test lives here and takes its broker, quote reader and clock as arguments. The
default factories are the only places that create a real SIMULATE context, so
tests can inject the fakes from ``tests/forward`` and never touch OpenD.
"""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import polars as pl

from app.forward import registration
from app.forward.book import LiveBook
from app.forward.broker import BrokerError, OrderType, Quotes, SimBroker, TrdSide
from app.forward.config import LEADS, forward_dir
from app.forward.orders import Executor, Journal
from app.market_clock import us_clock
from app.plugins.moomoo.provider import to_moomoo
from app.research.registry import Registry

__all__ = [
    "SystemClock",
    "append_audit",
    "audit_path",
    "default_workers",
    "engine_book",
    "is_regular_hours",
    "list_accounts",
    "make_broker",
    "make_quotes",
    "place_test_limit",
    "reference_block",
    "report_engine_book_fn",
    "run_flatten",
    "run_register",
    "run_rehearse",
    "run_report",
    "run_status",
    "run_stop",
    "var_sr_trials",
]


# ---------------------------------------------------------------------------
# Clocks and factories
# ---------------------------------------------------------------------------


class SystemClock:
    """The real ET wall clock plus the monotonic face ``Executor`` needs."""

    def now(self) -> datetime:
        return us_clock.now()

    def today(self) -> date:
        return self.now().date()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))


def make_broker(account_id: int) -> SimBroker:
    return SimBroker(int(account_id))


def make_quotes() -> Quotes:
    return Quotes()


def default_workers() -> int:
    """Night seeding workers: at most 8 and leave one CPU for the user."""
    cpus = os.cpu_count() or 1
    return max(1, min(8, cpus - 1))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def audit_path(data_dir: Path) -> Path:
    return forward_dir(data_dir) / "audit.jsonl"


def append_audit(data_dir: Path, event: dict[str, Any]) -> None:
    """Append one JSONL audit line, flushed and fsynced."""
    path = audit_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": datetime.now(UTC).isoformat(), **event}
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def is_regular_hours(now: datetime, calendar: Any | None = None) -> bool:
    """True inside 09:30-16:00 ET on a trading day."""
    if now.weekday() >= 5:
        return False
    calendar = calendar or us_clock
    try:
        if not calendar.is_trading_day(now.date()):
            return False
    except Exception:  # a missing calendar must not turn into a trade
        return False
    clock_time = now.timetz().replace(tzinfo=None)
    return dt_time(9, 30) <= clock_time < dt_time(16, 0)


def _resolve_broker(
    data_dir: Path,
    broker: Any | None,
    account_id: int | None = None,
    broker_factory: Any | None = None,
) -> Any:
    """The broker for an explicit ``account_id`` (before registration) or the registered one."""
    if broker is not None:
        return broker
    factory = broker_factory or make_broker
    if account_id is not None:
        return factory(int(account_id))
    reg = registration.load_registration(data_dir)
    return factory(int(reg["account_id"]))


def list_accounts(ctx_factory: Any | None = None) -> list[dict[str, Any]]:
    """Every account OpenD reports (read only: only ``get_acc_list`` is called).

    Used before registration to find the SIMULATE account id; nothing here can
    place, modify or cancel an order.
    """
    from app.forward.broker import _default_trade_context, _rows

    ctx = (ctx_factory or _default_trade_context)()
    try:
        ret, data = ctx.get_acc_list()
        if ret != 0:
            raise BrokerError(f"get_acc_list 失败: ret={ret!r} data={data!r}")
        return _rows(data)
    finally:
        close = getattr(ctx, "close", None)
        if callable(close):
            close()


def _is_open_order(status: Any) -> bool:
    return str(status or "").strip().upper() in {
        "SUBMITTED",
        "SUBMITTING",
        "FILLED_PART",
        "PARTIAL",
        "NEW",
        "WAITING_SUBMIT",
        "WAITING",
    }


def place_test_limit(broker: Any, symbol: str, price: float, remark: str) -> str:
    """One 1-share LIMIT buy through the broker's SIMULATE plumbing.

    ``SimBroker`` is frozen and has no limit-order method, so this helper
    reuses its private ``_call``/``_context`` path: that inherits the forced
    ``trd_env=SIMULATE`` and the registered ``acc_id``.
    """
    rows = broker._call(
        broker._context().place_order,
        price=float(price),
        qty=1,
        code=to_moomoo(symbol),
        trd_side=TrdSide.BUY,
        order_type=OrderType.NORMAL,
        remark=remark,
        time_in_force="DAY",
        fill_outside_rth=False,
    )
    if not rows:
        raise BrokerError("place_order 没有返回委托")
    order_id = rows[0].get("order_id")
    if order_id is None:
        raise BrokerError(f"place_order 没有 order_id: {rows[0]!r}")
    return str(order_id)


# ---------------------------------------------------------------------------
# status / test order
# ---------------------------------------------------------------------------


def run_status(
    data_dir: Path,
    *,
    check_account: bool = False,
    test_order: str | None = None,
    broker: Any | None = None,
    quotes: Any | None = None,
    clock: Any | None = None,
    calendar: Any | None = None,
    account_id: int | None = None,
    broker_factory: Any | None = None,
) -> dict[str, Any]:
    """Read-only status; ``--check-account`` never sends, ``--test-order`` cancels."""
    data_dir = Path(data_dir)
    clock = clock or SystemClock()
    now = clock.now()
    broker = _resolve_broker(data_dir, broker, account_id, broker_factory)
    result: dict[str, Any] = {"date": now.date().isoformat()}

    if check_account:
        account = broker.check_account()
        cash = broker.cash()
        positions = broker.positions()
        orders = broker.orders_today()
        result.update(
            {
                "account": account,
                "cash": cash,
                "positions": positions,
                "orders": orders,
            }
        )
        print(
            json.dumps(
                {
                    "account": account,
                    "cash": cash,
                    "positions": positions,
                    "orders": orders,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )

    if test_order:
        symbol = str(test_order).upper()
        if not is_regular_hours(now, calendar):
            raise RuntimeError("--test-order only runs during regular US hours")
        quotes = quotes or make_quotes()
        snapshot = quotes.snapshot([symbol]).get(symbol) or {}
        last = snapshot.get("last_price")
        if last is None or float(last) <= 0:
            raise BrokerError(f"{symbol} has no usable last price")
        price = round(float(last) * 0.8, 2)
        remark = f"fw-test-{now:%Y%m%d}-{symbol}"
        order_id = place_test_limit(broker, symbol, price, remark)

        orders = broker.orders_today()
        placed = next(
            (
                row
                for row in orders
                if str(row.get("remark")) == remark
                or str(row.get("order_id")) == order_id
            ),
            None,
        )
        print("placed order:")
        print(
            json.dumps(
                placed or {"order_id": order_id, "remark": remark},
                indent=2,
                sort_keys=True,
                default=str,
            )
        )

        cancel_id = str((placed or {}).get("order_id") or order_id)
        broker.cancel(cancel_id)
        final = next(
            (row for row in broker.orders_today() if str(row.get("remark")) == remark),
            None,
        )
        result["test_order"] = {
            **(placed or {"order_id": order_id, "remark": remark}),
            "cancelled_status": (final or {}).get("status"),
        }
        print("final order:")
        print(
            json.dumps(
                final or {"order_id": cancel_id, "remark": remark},
                indent=2,
                sort_keys=True,
                default=str,
            )
        )

    return result


# ---------------------------------------------------------------------------
# flatten
# ---------------------------------------------------------------------------


def run_flatten(
    data_dir: Path,
    *,
    confirm: bool = False,
    broker: Any | None = None,
    clock: Any | None = None,
    calendar: Any | None = None,
    account_id: int | None = None,
    broker_factory: Any | None = None,
) -> dict[str, Any]:
    """List holdings; with ``--confirm`` cancel and sell everything at market."""
    data_dir = Path(data_dir)
    clock = clock or SystemClock()
    broker = _resolve_broker(data_dir, broker, account_id, broker_factory)
    positions = broker.positions()
    orders = broker.orders_today()
    print("positions:")
    print(json.dumps(positions, indent=2, sort_keys=True, default=str))
    print("open orders:")
    print(json.dumps(orders, indent=2, sort_keys=True, default=str))

    result: dict[str, Any] = {
        "confirmed": bool(confirm),
        "positions": positions,
        "orders": orders,
    }
    if not confirm:
        return result

    if not is_regular_hours(clock.now(), calendar):
        raise RuntimeError("flatten --confirm only runs during regular US hours")

    for order in orders:
        if _is_open_order(order.get("status")):
            broker.cancel(str(order.get("order_id")))

    journal = Journal(forward_dir(data_dir) / "flatten" / "orders.jsonl")
    executor = Executor(broker, journal, clock=clock)
    day = clock.now().date()
    keys: list[str] = []
    errors: list[str] = []
    for symbol, qty in sorted(positions.items()):
        shares = int(qty)
        if shares <= 0:
            continue
        key = f"fw-flatten-{day:%Y%m%d}-{symbol}"
        try:
            executor.submit(key, symbol, "S", shares)
        except Exception as exc:  # one symbol must not hide the others
            errors.append(f"{symbol}: {exc}")
            continue
        keys.append(key)

    if keys:
        executor.poll_until(clock.monotonic() + 600.0)

    final_positions = broker.positions()
    print("positions after flatten:")
    print(json.dumps(final_positions, indent=2, sort_keys=True, default=str))
    result.update(
        {
            "keys": keys,
            "errors": errors,
            "final_positions": final_positions,
        }
    )
    return result


# ---------------------------------------------------------------------------
# Kill flag
# ---------------------------------------------------------------------------


def run_stop(data_dir: Path, *, reason: str = "", clock: Any | None = None) -> Path:
    """Create ``forward/STOP`` and record who asked for it."""
    data_dir = Path(data_dir)
    function_dir = forward_dir(data_dir)
    function_dir.mkdir(parents=True, exist_ok=True)
    path = function_dir / "STOP"
    now = (clock.now() if clock is not None else us_clock.now())
    path.write_text(
        f"forward runner stopped at {now.isoformat()}\nreason: {reason or 'manual'}\n",
        encoding="utf-8",
    )
    append_audit(
        data_dir,
        {"event": "stop_flag", "reason": reason or "manual", "path": str(path)},
    )
    return path


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def var_sr_trials(data_dir: Path) -> float:
    """The trial Sharpe variance the research script passes to ``finalize_trials``.

    ``scripts/research/evaluate.py`` calls ``Registry.var_sr_trials()`` and hands
    the value to ``evaluate.finalize_trials``; that method is the variance of
    the latest dev ``sharpe_daily`` per variant.
    """
    path = Path(data_dir) / "research" / "registry.json"
    return float(Registry(path).var_sr_trials())


_REFERENCE_FIELDS = ("cagr", "sharpe", "max_drawdown", "trades", "win_rate", "avg_r")


def reference_block(data_dir: Path, variant_id: str) -> dict[str, Any] | None:
    """The research holdout row's headline numbers, when the registry has them."""
    rows = Registry(Path(data_dir) / "research" / "registry.json").rows()
    candidates = [
        row
        for row in rows
        if row.get("period") == "holdout" and row.get("variant_id") == variant_id
    ]
    if not candidates:
        return None
    row = candidates[-1]
    nested = row.get("main")
    source = nested if isinstance(nested, dict) else {}
    block: dict[str, Any] = {}
    for key in _REFERENCE_FIELDS:
        value = source.get(key, row.get(key))
        if value is not None:
            block[key] = value
    return block or None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def run_register(
    data_dir: Path,
    *,
    capital: float,
    start_date: date,
    account_id: int,
    repo_root: Path,
    supersede: str | None = None,
) -> Path:
    """Write the registration and attach the research reference block."""
    data_dir = Path(data_dir)
    path = registration.write_registration(
        data_dir,
        capital_per_strategy=float(capital),
        start_date=start_date,
        account_id=int(account_id),
        var_sr_trials=var_sr_trials(data_dir),
        repo_root=Path(repo_root),
        supersede_reason=supersede,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    for lead, entry in (payload.get("strategies") or {}).items():
        block = reference_block(data_dir, lead)
        if block:
            entry["reference"] = block
    _atomic_json(path, payload)
    return path


# ---------------------------------------------------------------------------
# Rehearse / report
# ---------------------------------------------------------------------------


def run_rehearse(
    data_dir: Path,
    *,
    variant: str,
    work_dir: Path | None = None,
    limit_days: int | None = None,
) -> dict[str, Any]:
    """Same call the standalone ``scripts/forward/rehearse.py`` makes."""
    from app.forward.rehearse import rehearse
    from app.research.evaluate import holdout_period

    period = holdout_period()
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="forward-rehearse-"))
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    return rehearse(
        Path(data_dir),
        variant,
        period.start,
        period.end,
        1_000_000.0,
        work_dir,
        limit_days=limit_days,
    )


def engine_book(
    data_dir: Path,
    short: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Run the real research engine over ``[start, end]`` on real forward bars."""
    from app.forward.data import load_forward_bars
    from app.forward.engine_run import build_engine_store, run_engine
    from app.forward.signals import load_points_through
    from app.forward.tables import (
        build_inputs,
        forward_eligibility,
        forward_table,
    )
    from app.research.evaluate import equity_frame

    data_dir = Path(data_dir)
    reg = registration.load_registration(data_dir)
    lead = next((name for name, value in LEADS.items() if value == short), short)
    entry = (reg.get("strategies") or {}).get(lead) or {}
    capital = float(
        (entry.get("backtest") or {}).get("initial_capital")
        or reg.get("capital_per_strategy")
        or 1_000_000.0
    )

    empty = pl.DataFrame(schema={"date": pl.Date, "equity": pl.Float64})
    bars = load_forward_bars(data_dir, end=end)
    if bars.is_empty():
        return {"trades": [], "equity": empty, "table": pl.DataFrame()}

    chan, events, ranges = load_points_through(data_dir, end)
    spy = bars.filter(pl.col("symbol") == "SPY")
    inputs = build_inputs(bars, spy, chan, events, ranges)
    elig = forward_eligibility(data_dir, bars, end)
    table, _log = forward_table(lead, inputs, elig, bars)

    book = LiveBook.from_journal(
        short,
        capital,
        Journal(forward_dir(data_dir) / short / "orders.jsonl").events(),
    )
    symbols = sorted(set(table["symbol"].to_list()) | set(book.lots) | {"SPY"})
    if not symbols:
        return {"trades": [], "equity": empty, "table": table}

    with tempfile.TemporaryDirectory(prefix=f"forward-report-{short}-") as work:
        store = build_engine_store(
            data_dir,
            Path(work) / "store",
            symbols,
            start - timedelta(days=400),
            end,
            None,
        )
        result = run_engine(store, table, start, end, capital)

    return {
        "trades": list(getattr(result, "trades", None) or []),
        "equity": equity_frame(result),
        "table": table,
    }


def report_engine_book_fn(data_dir: Path):
    data_dir = Path(data_dir)
    return lambda short, start, end: engine_book(data_dir, short, start, end)


def run_report(
    data_dir: Path,
    *,
    month: str,
    as_of: date | None = None,
    report_dir: Path | None = None,
) -> Path:
    from app.forward.report import write_report

    end = as_of or us_clock.today()
    return write_report(
        Path(data_dir),
        month,
        as_of=end,
        engine_book_fn=report_engine_book_fn(data_dir),
        report_dir=report_dir,
    )


# Kept for callers that want the same statistic without importing the registry.
def variance(values: list[float]) -> float:
    return float(statistics.variance(values)) if len(values) >= 2 else 0.0