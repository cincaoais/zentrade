"""Run the research variants through ZenTrade and write a report.

Usage::

    cd backend
    uv run python scripts/research/evaluate.py --period dev [--variants H1-2B] \
        [--seeds 200] [--workers N]

The dev run records one registry row per variant; the holdout run first checks
the freeze file per variant and raises :class:`HoldoutLocked` before running
anything. Integrity checks abort the run with exit code 1 on a mismatch.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import random
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import polars as pl

from app.config import settings
from app.research import RESEARCH_LAST_DATE
from app.research.bars import load_bars, tradable_symbols
from app.research.baselines import b0_bh, b0_spy
from app.research.compose import with_atr14
from app.research.evaluate import (
    EvalContext,
    Period,
    dev_period,
    equity_frame,
    evaluate_variant,
    finalize_trials,
    folds,
    holdout_period,
    run_table,
    trading_days,
)
from app.research.metrics import curve_stats
from app.research.registry import HoldoutLocked, Pins, Registry, check_holdout_allowed
from app.research.universe import UniverseParams, eligibility
from app.research.versions import chan_engine_version, file_hash, wyckoff_port_version
from app.storage.repository import DataStore, KlineRepository

logger = logging.getLogger("evaluate")

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
BRIDGE_PATH = BACKEND / "app" / "strategy" / "builtin" / "research_signal_table.py"
DEVIATIONS_PATH = BACKEND / "app" / "research" / "DEVIATIONS.md"

# Set once per worker process; StrategyBacktestService holds the matrix cache.
_WORKER_SERVICE = None


def _default_workers() -> int:
    return max(1, min(12, (os.cpu_count() or 1) - 2))


def _strategy_dirs(data_dir: Path) -> list[Path]:
    return [
        BACKEND / "app" / "strategy" / "builtin",
        data_dir / "strategies" / "custom",
        data_dir / "strategies" / "ai",
        data_dir / "strategies" / "composite",
    ]


def _build_service(data_dir: Path):
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.factors.store import load_into_registry
    from app.strategy import config as strategy_config
    from app.strategy.engine import StrategyEngine

    load_into_registry(data_dir)
    strategy_engine = StrategyEngine(
        strategy_dirs=_strategy_dirs(data_dir),
        override_loader=lambda sid: strategy_config.load_override(data_dir, sid),
    )
    return StrategyBacktestService(
        BacktestEngine(KlineRepository(DataStore(data_dir))), strategy_engine
    )


def _latest_table(data_dir: Path, variant_id: str) -> Path | None:
    directory = Path(data_dir) / "research" / "variants" / variant_id
    files = list(directory.glob("*.parquet")) if directory.is_dir() else []
    if not files:
        return None
    return max(files, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _current_pins(data_dir: Path, variant_ids: list[str]) -> Pins:
    table_hashes: dict[str, str] = {}
    for variant_id in variant_ids:
        path = _latest_table(data_dir, variant_id)
        if path is not None:
            table_hashes[variant_id] = path.stem
    repo = KlineRepository(DataStore(data_dir))
    return Pins(
        data_generation=repo.get_matrix_data_generation("stock"),
        last_date=RESEARCH_LAST_DATE.isoformat(),
        universe_hash=UniverseParams().hash(),
        chan_version=chan_engine_version(),
        wyckoff_version=wyckoff_port_version(),
        table_hashes=table_hashes,
        bridge_hash=file_hash(BRIDGE_PATH),
    )


# ---------------------------------------------------------------------------
# Random-seed process pool: each worker owns one StrategyBacktestService
# ---------------------------------------------------------------------------


def _init_worker(data_dir: str) -> None:
    global _WORKER_SERVICE
    _WORKER_SERVICE = _build_service(Path(data_dir))


def _run_worker(task: tuple[str, str, date, date]) -> float:
    path, name, start, end = task
    result = run_table(
        _WORKER_SERVICE,
        Path(path),
        Period(name, start, end),
    )
    return float(curve_stats(equity_frame(result))["cagr"])


def _pool_runner(data_dir: Path, workers: int):
    def runner(paths: list[Path], period: Period) -> list[float]:
        tasks = [(str(path), period.name, period.start, period.end) for path in paths]
        with multiprocessing.Pool(
            processes=workers,
            initializer=_init_worker,
            initargs=(str(data_dir),),
        ) as pool:
            chunksize = max(1, len(tasks) // max(workers * 4, 1))
            return list(pool.map(_run_worker, tasks, chunksize=chunksize))

    return runner


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def _chan_truncation_one(task: tuple[str, str, int]) -> dict:
    """One symbol's Chan truncation check (real engine), for the process pool."""
    from app.research.integrity import chan_truncation_check

    data_dir, symbol, seed = task
    return chan_truncation_check(Path(data_dir), [symbol], cuts_per_symbol=5, seed=seed)


def _chan_truncation_parallel(data_dir: Path, symbols: list[str], workers: int) -> dict:
    """The real-engine truncation check costs ~20 s per cut (250 cuts): spread
    symbols over a pool. Each symbol gets its own fixed seed, so it is
    reproducible regardless of worker scheduling."""
    tasks = [(str(data_dir), symbol, index) for index, symbol in enumerate(symbols)]
    if workers <= 1:
        parts = [_chan_truncation_one(task) for task in tasks]
    else:
        with multiprocessing.get_context("spawn").Pool(processes=workers) as pool:
            parts = pool.map(_chan_truncation_one, tasks, chunksize=1)
    return {
        "checked": sum(part["checked"] for part in parts),
        "mismatches": [item for part in parts for item in part["mismatches"]],
    }


def _integrity(data_dir: Path, symbols: list[str], workers: int = 1) -> dict:
    """Run the Task 13 checks; imported lazily so the runner never owns them."""
    from app.research.chan_replay import load_chan_gone, load_chan_points
    from app.research.integrity import (
        determinism_check,
        phantom_rate,
        wyckoff_truncation_check,
    )

    rng = random.Random(0)
    picks = sorted(symbols)
    rng.shuffle(picks)
    chan_symbols = picks[:50]
    wyk_symbols = picks[:50]
    determinism_symbols = picks[:10]

    chan = _chan_truncation_parallel(data_dir, chan_symbols, workers)
    wyk = wyckoff_truncation_check(data_dir, wyk_symbols, cuts_per_symbol=5, seed=0)
    phantom = phantom_rate(load_chan_points(data_dir), load_chan_gone(data_dir))
    determinism = determinism_check(data_dir, determinism_symbols)
    return {
        "chan_truncation": chan,
        "wyckoff_truncation": wyk,
        "phantom_rate": phantom.to_dicts(),
        "determinism": determinism,
    }


def _integrity_failed(integrity: dict) -> bool:
    return bool(
        integrity["chan_truncation"].get("mismatches")
        or integrity["wyckoff_truncation"].get("mismatches")
        or integrity["determinism"].get("different")
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_report(
    data_dir: Path,
    period: Period,
    results: list[dict],
    integrity: dict,
    registry: Registry,
    frozen: dict | None,
    eligible: pl.DataFrame,
    bars: pl.DataFrame,
    spy: pl.DataFrame,
) -> Path | None:
    from app.research.report import write_report

    baselines = {
        "b0_bh": curve_stats(b0_bh(bars, eligible, period.start, period.end)),
        "b0_spy": curve_stats(b0_spy(spy, period.start, period.end)),
    }
    today = date.today().isoformat()
    run_name = f"{today}-{period.name}"
    out = REPO / "docs" / "research" / f"{run_name}.md"
    deviations = (
        DEVIATIONS_PATH.read_text(encoding="utf-8")
        if DEVIATIONS_PATH.exists()
        else "(no deviations file)"
    )
    return write_report(
        out=out,
        run_name=run_name,
        results=results,
        baselines=baselines,
        integrity=integrity,
        registry_rows=registry.rows(),
        frozen=frozen,
        deviations_md=deviations,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the research variants")
    parser.add_argument("--period", choices=("dev", "holdout"), required=True)
    parser.add_argument(
        "--variants",
        default=None,
        help="comma separated variant ids (default: all 18)",
    )
    parser.add_argument("--seeds", type=int, default=200, help="random baseline seeds")
    parser.add_argument(
        "--workers",
        type=int,
        default=_default_workers(),
        help="random-baseline worker processes (each holds a full backtest service, ~2.3 GB)",
    )
    parser.add_argument(
        "--integrity-workers",
        type=int,
        default=None,
        help="Chan truncation-check processes (~0.25 GB each; default: --workers)",
    )
    parser.add_argument(
        "--data-dir",
        default=str(settings.data_dir),
        help="ZenTrade data directory (default: configured data dir)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(args.data_dir).resolve()
    research_dir = data_dir / "research"
    frozen_path = research_dir / "registry.frozen.json"

    from app.research.hypotheses import VARIANTS

    if args.variants:
        variant_ids = [item.strip() for item in args.variants.split(",") if item.strip()]
        unknown = [item for item in variant_ids if item not in VARIANTS]
        if unknown:
            parser.error(f"unknown variant ids: {', '.join(unknown)}")
    else:
        variant_ids = list(VARIANTS)

    # Lock before touching the store: a holdout run without a freeze is a no-op
    # that must not even build inputs, let alone call run_table.
    if args.period == "holdout" and not frozen_path.exists():
        raise HoldoutLocked(f"holdout locked: no freeze file at {frozen_path}")

    # ATR is computed once and reused by every composed table and random seed.
    bars = with_atr14(load_bars(data_dir))
    elig = eligibility(bars, tradable_symbols(data_dir), UniverseParams())
    spy = load_bars(data_dir, symbols=["SPY"])

    period = holdout_period() if args.period == "holdout" else dev_period(elig)

    tables: dict[str, Path] = {}
    missing: list[str] = []
    for variant_id in variant_ids:
        path = _latest_table(data_dir, variant_id)
        if path is None:
            missing.append(variant_id)
        else:
            tables[variant_id] = path
    if missing:
        print(
            "evaluate: no composed table for " + ", ".join(missing) + "; run compose first",
            file=sys.stderr,
        )
        return 1

    registry = Registry(research_dir / "registry.json")
    if args.period == "holdout":
        pins = _current_pins(data_dir, variant_ids)
        for variant_id in variant_ids:
            check_holdout_allowed(frozen_path, pins, variant_id)

    # Integrity first: a leak found after hours of backtests would waste them all.
    eligible_symbols = elig.filter(pl.col("eligible"))["symbol"].unique().to_list()
    logger.info("integrity checks (Chan truncation replays the real engine; ~30 min)")
    integrity_workers = args.integrity_workers or args.workers
    integrity = _integrity(data_dir, eligible_symbols, integrity_workers)
    integrity_path = research_dir / f"integrity-{args.period}.json"
    integrity_path.parent.mkdir(parents=True, exist_ok=True)
    integrity_path.write_text(json.dumps(integrity, indent=1, default=str), encoding="utf-8")
    logger.info(
        "integrity: chan %d cuts / %d mismatches, wyckoff %d cuts / %d mismatches, "
        "determinism %d identical / %d different -> %s",
        integrity["chan_truncation"].get("checked", 0),
        len(integrity["chan_truncation"].get("mismatches") or []),
        integrity["wyckoff_truncation"].get("checked", 0),
        len(integrity["wyckoff_truncation"].get("mismatches") or []),
        integrity["determinism"].get("identical", 0),
        len(integrity["determinism"].get("different") or []),
        integrity_path,
    )
    if _integrity_failed(integrity):
        print(
            f"evaluate: integrity check failed; aborting (details: {integrity_path})",
            file=sys.stderr,
        )
        return 1

    svc = _build_service(data_dir)
    ctx = EvalContext(
        svc=svc,
        registry=registry,
        data_dir=data_dir,
        tables=tables,
        bars=bars,
        elig=elig,
        spy=spy,
        work_dir=research_dir / "tmp",
        n_trials=registry.n_trials(),
        var_sr_trials=registry.var_sr_trials(),
        trading_days=trading_days(bars, period),
        random_runner=_pool_runner(data_dir, args.workers) if args.workers > 1 else None,
    )

    fold_count = len(folds(period, ctx.trading_days, test_len=126, embargo=5))
    starts = time.perf_counter()
    run_table(svc, tables[variant_ids[0]], period)
    seconds = time.perf_counter() - starts
    runs = len(variant_ids) * (2 + (fold_count if args.period == "dev" else 0) + args.seeds)
    eta_minutes = runs * seconds / max(args.workers, 1) / 60.0
    print(f"ETA: {runs} runs x {seconds:.1f}s / {args.workers} workers = {eta_minutes:.1f} min")

    results: list[dict] = []
    for variant_id in variant_ids:
        logger.info("evaluating %s (%s)", variant_id, period.name)
        result = evaluate_variant(ctx, variant_id, period, seeds=args.seeds)
        results.append(result)
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "variant_id",
                        "period",
                        "main",
                        "random_percentile",
                        "random_note",
                        "gate",
                    )
                },
                default=str,
            )
        )

    # DSR during the pass used a provisional N; recompute with the final count.
    finalize_trials(results, registry.n_trials(), registry.var_sr_trials())
    for result in results:
        print(json.dumps({"variant_id": result["variant_id"], "dsr": result["main"].get("dsr"),
                          "n_trials": result["n_trials"], "gate": result["gate"]}, default=str))

    frozen = json.loads(frozen_path.read_text(encoding="utf-8")) if frozen_path.exists() else None
    try:
        out = _write_report(data_dir, period, results, integrity, registry, frozen, elig, bars, spy)
    except ImportError as exc:  # report writer arrives in Task 15
        logger.warning("report writer unavailable: %s", exc)
        out = None
    if out is not None:
        print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
