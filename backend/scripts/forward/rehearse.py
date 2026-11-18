"""Replay the forward morning procedure over the research holdout.

Usage::

    cd backend
    uv run python scripts/forward/rehearse.py --variant H2-spring
    uv run python scripts/forward/rehearse.py --variant H6-H4-3B --work-dir /tmp/rehearse

The script only reads ``--data-dir`` and writes engine stores and decision
tables under ``--work-dir`` (a temporary directory by default). It never writes
the research registry or store and never talks to a broker.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings
from app.forward.rehearse import rehearse
from app.research.evaluate import holdout_period
from app.research.hypotheses import VARIANTS

DEFAULT_CAPITAL = 1_000_000.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rehearse the forward morning procedure over the holdout"
    )
    parser.add_argument("--variant", required=True, help="research variant id")
    parser.add_argument(
        "--data-dir",
        default=str(settings.data_dir),
        help="ZenTrade data directory (default: configured data dir)",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="where to build engine stores (default: a new temporary directory)",
    )
    parser.add_argument(
        "--limit-days",
        type=int,
        default=None,
        help="rehearse only the first N trading days (quick partial run)",
    )
    args = parser.parse_args(argv)

    if args.variant not in VARIANTS:
        parser.error(f"unknown variant id: {args.variant}")

    data_dir = Path(args.data_dir).resolve()
    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="forward-rehearse-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    period = holdout_period()
    result = rehearse(
        data_dir,
        args.variant,
        period.start,
        period.end,
        DEFAULT_CAPITAL,
        work_dir,
        limit_days=args.limit_days,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())