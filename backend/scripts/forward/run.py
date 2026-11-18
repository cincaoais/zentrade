"""Forward-test command line (``uv run python scripts/forward/run.py ...``).

Subcommands mirror spec section 6.10. The daemon is meant to be started by the
user in an Orca terminal; nothing here is wired into the web app's scheduler.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings
from app.forward import cli_support, daemon, registration
from app.forward.config import LEADS
from app.market_clock import us_clock

REPO_ROOT = Path(__file__).resolve().parents[3]


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forward",
        description="Moomoo SIMULATE forward test (research leads H2-spring, H6-H4-3B)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=help_text)
        child.add_argument(
            "--data-dir",
            default=str(settings.data_dir),
            help="ZenTrade data directory (default: configured data dir)",
        )
        return child

    rehearse = add("rehearse", "replay the morning procedure over the holdout")
    rehearse.add_argument("--variant", required=True, choices=sorted(LEADS))
    rehearse.add_argument("--work-dir", default=None)
    rehearse.add_argument("--limit-days", type=int, default=None)

    register = add("register", "freeze the forward registration")
    register.add_argument("--capital", type=float, required=True)
    register.add_argument("--start-date", required=True)
    register.add_argument("--account-id", type=int, required=True)
    register.add_argument("--supersede", default=None, metavar="REASON")

    status = add("status", "show records; optionally read the account or a test order")
    status.add_argument("--check-account", action="store_true")
    status.add_argument("--test-order", default=None, metavar="SYMBOL")
    status.add_argument(
        "--account-id", type=int, default=None,
        help="account to read before registration (default: the registered one)",
    )
    status.add_argument(
        "--list-accounts", action="store_true",
        help="print every OpenD account (id, environment, markets); read only",
    )

    flatten = add("flatten", "list holdings; --confirm sells everything at market")
    flatten.add_argument("--confirm", action="store_true")
    flatten.add_argument(
        "--account-id", type=int, default=None,
        help="account to flatten before registration (default: the registered one)",
    )

    add("dry-run", "run the daemon without sending orders")
    add("daemon", "run the forward daemon")

    night = add("night", "prepare the forward record for one night")
    night.add_argument("--date", required=True)
    night.add_argument(
        "--workers",
        type=int,
        default=None,
        help="parallel seeding workers (default: min(8, cpu_count-1))",
    )

    add("morning", "run the morning decision and orders for today")

    add("reconcile", "reconcile the broker against both live books now")

    parity = add("parity", "recompute one night's parity record")
    parity.add_argument("--date", required=True)

    stop = add("stop", "write the forward/STOP kill flag")
    stop.add_argument("--reason", default="")

    report = add("report", "write the monthly forward report")
    report.add_argument("--month", required=True, metavar="YYYY-MM")
    report.add_argument("--as-of", default=None)

    return parser


def _run_parity(data_dir: Path, day: date) -> dict:
    """Recompute and save both strategies' parity records for ``day``."""
    from app.forward import night
    from app.forward.data import forward_tradable, load_forward_bars
    from app.forward.signals import load_points_through
    from app.forward.tables import build_inputs, forward_eligibility, forward_table

    reg = registration.load_registration(data_dir)
    start = date.fromisoformat(str(reg["start_date"])[:10])
    books = night._held_books(data_dir, reg)
    held = sorted({symbol for book in books.values() for symbol in book.lots})
    symbols = sorted(set(forward_tradable(data_dir, day)) | set(held) | {"SPY"})
    bars = load_forward_bars(data_dir, symbols=symbols, end=day)
    elig = forward_eligibility(data_dir, bars, day)
    chan, events, ranges = load_points_through(data_dir, day)
    spy = bars.filter(bars["symbol"] == "SPY")

    records: dict[str, dict] = {}
    for lead, short in LEADS.items():
        inputs = build_inputs(bars, spy, chan, events, ranges)
        table, _log = forward_table(lead, inputs, elig, bars)
        path = data_dir / "forward" / short / "night" / f"{day.isoformat()}.json"
        relevant: list[str] = []
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            relevant = list(saved.get("relevant_symbols") or [])
        if not relevant:
            relevant = sorted(set(table["symbol"].to_list()))
        records[short] = night._run_parity(
            data_dir,
            short,
            table=table,
            symbols=relevant,
            start=start,
            t=day,
            capital=night._capital(reg, lead),
            bars=bars,
        )
    return records


def _morning(data_dir: Path) -> dict:
    from app.forward.morning import run_morning
    from app.forward.orders import Executor

    reg = registration.load_registration(data_dir)
    broker = cli_support.make_broker(int(reg["account_id"]))
    quotes = cli_support.make_quotes()
    clock = cli_support.SystemClock()

    def executor_factory(broker_arg, journal_arg, *, clock):
        return Executor(broker_arg, journal_arg, clock=clock)

    try:
        return run_morning(
            data_dir,
            us_clock.today(),
            broker=broker,
            quotes=quotes,
            executor_factory=executor_factory,
            clock=clock,
            dry_run=False,
            repo_root=REPO_ROOT,
        )
    finally:
        for resource in (quotes, broker):
            close = getattr(resource, "close", None)
            if callable(close):
                close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()

    if args.command == "rehearse":
        _print(
            cli_support.run_rehearse(
                data_dir,
                variant=args.variant,
                work_dir=Path(args.work_dir) if args.work_dir else None,
                limit_days=args.limit_days,
            )
        )
    elif args.command == "register":
        _print(
            str(
                cli_support.run_register(
                    data_dir,
                    capital=args.capital,
                    start_date=date.fromisoformat(args.start_date),
                    account_id=args.account_id,
                    repo_root=REPO_ROOT,
                    supersede=args.supersede,
                )
            )
        )
    elif args.command == "status":
        if args.list_accounts:
            _print(cli_support.list_accounts())
        else:
            _print(
                cli_support.run_status(
                    data_dir,
                    check_account=args.check_account,
                    test_order=args.test_order,
                    account_id=args.account_id,
                )
            )
    elif args.command == "flatten":
        _print(
            cli_support.run_flatten(
                data_dir, confirm=args.confirm, account_id=args.account_id
            )
        )
    elif args.command == "dry-run":
        _print(daemon.run(data_dir, dry_run=True, repo_root=REPO_ROOT))
    elif args.command == "daemon":
        _print(daemon.run(data_dir, dry_run=False, repo_root=REPO_ROOT))
    elif args.command == "night":
        from app.forward.night import run_night

        workers = args.workers if args.workers is not None else cli_support.default_workers()
        _print(
            run_night(
                data_dir,
                date.fromisoformat(args.date),
                repo_root=REPO_ROOT,
                workers=workers,
            )
        )
    elif args.command == "morning":
        _print(_morning(data_dir))
    elif args.command == "reconcile":
        reg = registration.load_registration(data_dir)
        broker = cli_support.make_broker(int(reg["account_id"]))
        try:
            _print(
                daemon.reconcile_books(
                    data_dir, us_clock.today(), broker=broker, reg=reg
                )
            )
        finally:
            close = getattr(broker, "close", None)
            if callable(close):
                close()
    elif args.command == "parity":
        _print(_run_parity(data_dir, date.fromisoformat(args.date)))
    elif args.command == "stop":
        _print(str(cli_support.run_stop(data_dir, reason=args.reason)))
    elif args.command == "report":
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        _print(str(cli_support.run_report(data_dir, month=args.month, as_of=as_of)))
    else:  # argparse enforces the choice
        parser.error(f"unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())