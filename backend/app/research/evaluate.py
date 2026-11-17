"""Evaluate composed research tables through the ordinary ZenTrade backtest path.

The module has no knowledge of where the tables came from: ``run_table`` hands a
``research_signal_table`` strategy config to ``StrategyBacktestService`` and the
result is scored with the metrics module. Random-entry seeds and the fold split
use exactly the same path, so every comparison shares costs and execution.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl

from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestResult
from app.research import DEV_END, HOLDOUT_END, HOLDOUT_START
from app.research.baselines import b0_bh, random_signals
from app.research.compose import compose_table
from app.research.metrics import curve_stats, dsr, summarize
from app.research.registry import Registry
from app.research.versions import file_hash

logger = logging.getLogger(__name__)

STRATEGY_ID = "research_signal_table"
GATE_DSR = 0.9
GATE_MIN_TRADES = 100
MAIN_SLIPPAGE_BPS = 5.0
STRESS_SLIPPAGE_BPS = 15.0

PeriodName = Literal["dev", "holdout"]


@dataclass(frozen=True)
class Period:
    """A named evaluation window."""

    name: PeriodName
    start: date
    end: date


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


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


def dev_period(elig: pl.DataFrame, top_n: int = 1500) -> Period:
    """Development window starting at the first full-universe date.

    "Full universe" is ``min(top_n, max daily eligible count)``: a handful of
    names with older history are eligible before the bulk of the store reaches
    250 bars, and starting there would evaluate a different market.
    """
    if elig.is_empty() or "eligible" not in elig.columns:
        raise ValueError("eligibility frame is empty")
    counts = elig.filter(pl.col("eligible")).group_by("date").len().sort("date")
    if counts.is_empty():
        raise ValueError("no eligible symbol on any date")
    full = min(int(top_n), int(counts["len"].max()))
    start = counts.filter(pl.col("len") >= full)["date"].min()
    return Period("dev", start, DEV_END)


def holdout_period() -> Period:
    """The locked holdout window."""
    return Period("holdout", HOLDOUT_START, HOLDOUT_END)


def folds(
    period: Period,
    trading_days: list[date],
    test_len: int = 126,
    embargo: int = 5,
) -> list[Period]:
    """Consecutive test windows inside ``period``, each after a 5-bar embargo."""
    days = sorted({d for d in trading_days if period.start <= d <= period.end})
    result: list[Period] = []
    start = 0
    while start + test_len <= len(days):
        window = days[start : start + test_len]
        result.append(Period(period.name, window[0], window[-1]))
        start += test_len + embargo
    return result


# ---------------------------------------------------------------------------
# ZenTrade runs
# ---------------------------------------------------------------------------


def run_table(
    svc: Any,
    table_path: Path,
    period: Period,
    slippage_bps: float = MAIN_SLIPPAGE_BPS,
) -> StrategyBacktestResult:
    """Run one composed table over ``period`` through the normal backtest service."""
    config = StrategyBacktestConfig(
        strategy_id=STRATEGY_ID,
        symbols=None,
        start=period.start,
        end=period.end,
        params={"table_path": str(table_path)},
        max_positions=10,
        position_sizing="equal",
        entry_fill="open_t+1",
        exit_fill="open_t+1",
        slippage_bps=slippage_bps,
        mode="position",
    )
    return svc.run(config)


def equity_frame(result: object) -> pl.DataFrame:
    """Adapt the engine's ``[{date, value}]`` curve to ``(date, equity)``.

    ``summarize`` works on ``(date, equity)``; the engine emits ``value`` and a
    string date, and some paths already emit ``equity``. This is the only place
    that translation happens.
    """
    if isinstance(result, dict):
        curve = result.get("equity_curve") or []
    else:
        curve = getattr(result, "equity_curve", None) or []

    dates: list[date] = []
    values: list[float] = []
    for row in curve:
        raw_date = row.get("date") if isinstance(row, dict) else getattr(row, "date", None)
        raw_value = (
            row.get("equity", row.get("value"))
            if isinstance(row, dict)
            else getattr(row, "equity", getattr(row, "value", None))
        )
        parsed = _as_date(raw_date)
        if parsed is None or raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        dates.append(parsed)
        values.append(value)

    return pl.DataFrame(
        {"date": dates, "equity": values},
        schema={"date": pl.Date, "equity": pl.Float64},
    ).sort("date")


# ---------------------------------------------------------------------------
# Metrics and gate
# ---------------------------------------------------------------------------


def _metrics(result: object, table: pl.DataFrame, ctx: EvalContext) -> tuple[dict, pl.DataFrame]:
    equity = equity_frame(result)
    trades = getattr(result, "trades", None) or []
    metrics = summarize(trades, equity, table, ctx.n_trials, ctx.var_sr_trials)
    # Keep the return moments so the DSR can be recomputed once the final trial
    # count is known (finalize_trials); ctx.n_trials is only provisional.
    stats = curve_stats(equity)
    metrics["curve"] = {
        key: stats[key] for key in ("sharpe_daily", "skew", "kurtosis", "n_days")
    }
    return metrics, equity


def _recompute_dsr(metrics: dict, n_trials: int, var_sr_trials: float) -> None:
    curve = metrics.get("curve") or {}
    n_days = int(curve.get("n_days") or 0)
    if n_days < 2:
        metrics["dsr"] = None
        return
    metrics["dsr"] = dsr(
        float(curve["sharpe_daily"]),
        n_days - 1,
        float(curve["skew"]),
        float(curve["kurtosis"]),
        var_sr_trials,
        n_trials,
    )


def finalize_trials(results: list[dict], n_trials: int, var_sr_trials: float) -> None:
    """Recompute DSR (and the holdout gate) with the final trial count.

    The registry only knows every variant's Sharpe after the whole pass has run,
    so DSR computed during the pass would use too small an N (on a first dev run,
    N=1) and overstate significance. Call this after all variants are recorded.
    """
    for result in results:
        for key in ("main", "stress"):
            if result.get(key):
                _recompute_dsr(result[key], n_trials, var_sr_trials)
        result["n_trials"] = n_trials
        result["var_sr_trials"] = var_sr_trials
        if result.get("period") == "holdout":
            result["gate"] = gate_result(
                result["main"],
                result.get("stress"),
                result.get("bh_cagr"),
                result.get("random_cagrs") or [],
            )


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q))


def gate_checks(main: dict, bh_cagr: float | None, random_cagrs: list[float]) -> dict:
    """The five holdout gate conditions for one set of metrics."""
    rand95 = _percentile(random_cagrs, 95)
    cagr = main.get("cagr")
    avg_r = main.get("avg_r")
    dsr = main.get("dsr")
    return {
        "cagr_vs_bh": (bh_cagr is not None and cagr is not None and float(cagr) > float(bh_cagr)),
        "cagr_vs_rand95": (rand95 is not None and cagr is not None and float(cagr) > rand95),
        "trades_ge_100": int(main.get("trades") or 0) >= GATE_MIN_TRADES,
        "avg_r_pos": avg_r is not None and float(avg_r) > 0.0,
        "dsr_ge_0_9": dsr is not None and float(dsr) >= GATE_DSR,
    }


def gate_result(
    main: dict,
    stress: dict | None,
    bh_cagr: float | None,
    random_cagrs: list[float],
) -> dict:
    """Holdout gate: ``passed`` from 5 bps, ``fragile_to_costs`` from 15 bps."""
    checks = gate_checks(main, bh_cagr, random_cagrs)
    passed = all(checks.values())
    fragile = False
    if passed and stress is not None:
        stress_checks = gate_checks(stress, bh_cagr, random_cagrs)
        fragile = not all(stress_checks.values())
    return {"passed": bool(passed), "checks": checks, "fragile_to_costs": fragile}


def _bh_cagr(ctx: EvalContext, period: Period) -> float | None:
    if ctx.bh_cagr is not None:
        return float(ctx.bh_cagr)
    if ctx.bars.is_empty() or ctx.elig.is_empty():
        return None
    curve = b0_bh(ctx.bars, ctx.elig, period.start, period.end)
    if curve.is_empty():
        return None
    return float(curve_stats(curve)["cagr"])


# ---------------------------------------------------------------------------
# Random baseline and evaluation
# ---------------------------------------------------------------------------


@dataclass
class EvalContext:
    """Everything one evaluation pass needs, loaded once by the CLI.

    ``tables`` maps variant id to the composed parquet. ``bars`` is expected to
    carry the precomputed ``atr14`` column so random seeds do not recompute it.
    """

    svc: Any
    registry: Registry
    data_dir: Path
    tables: dict[str, Path]
    bars: pl.DataFrame = field(default_factory=pl.DataFrame)
    elig: pl.DataFrame = field(default_factory=pl.DataFrame)
    spy: pl.DataFrame = field(default_factory=pl.DataFrame)
    work_dir: Path | None = None
    n_trials: int = 1
    var_sr_trials: float = 0.0
    trading_days: list[date] | None = None
    bh_cagr: float | None = None
    # Called with (random table paths, period) and returns CAGRs. The CLI wires
    # a process pool here; tests leave it None and use the monkeypatched run_table.
    random_runner: Callable[[list[Path], Period], list[float]] | None = None


def _write_table(path: Path, table: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table.write_parquet(path, compression="zstd", statistics=False)


def _random_cagrs(
    ctx: EvalContext,
    variant_id: str,
    table: pl.DataFrame,
    period: Period,
    seeds: int,
) -> tuple[list[float], str | None]:
    """Random-entry CAGRs, or a note when the variant has no entries."""
    if seeds <= 0:
        return [], None
    if ctx.bars.is_empty() or ctx.elig.is_empty():
        return [], "no inputs"

    base = ctx.work_dir or (Path(ctx.data_dir) / "research" / "tmp")
    paths: list[Path] = []
    for seed in range(seeds):
        signals = random_signals(table, ctx.elig, ctx.bars, seed)
        if signals is None:
            return [], "no entries"
        random_table, _log = compose_table(signals, ctx.bars, ctx.elig)
        path = base / f"{variant_id}-{period.name}-random-{seed}.parquet"
        _write_table(path, random_table)
        paths.append(path)

    if ctx.random_runner is not None:
        return [float(value) for value in ctx.random_runner(paths, period)], None

    cagrs: list[float] = []
    for path in paths:
        result = run_table(ctx.svc, path, period)
        cagrs.append(float(curve_stats(equity_frame(result))["cagr"]))
    return cagrs, None


def evaluate_variant(
    ctx: EvalContext,
    variant_id: str,
    period: Period,
    seeds: int = 200,
) -> dict:
    """Score one variant in ``period``, stress it at 15 bps and log the trial."""
    if variant_id not in ctx.tables:
        raise KeyError(f"no composed table for variant {variant_id!r}")
    table_path = Path(ctx.tables[variant_id])
    table = pl.read_parquet(table_path)

    main_result = run_table(ctx.svc, table_path, period, MAIN_SLIPPAGE_BPS)
    main_metrics, main_equity = _metrics(main_result, table, ctx)

    stress_result = run_table(ctx.svc, table_path, period, STRESS_SLIPPAGE_BPS)
    stress_metrics, _stress_equity = _metrics(stress_result, table, ctx)

    fold_results: list[dict] = []
    if period.name == "dev":
        days = ctx.trading_days
        if days is None:
            days = table["date"].unique().sort().to_list()
        for fold in folds(period, list(days)):
            result = run_table(ctx.svc, table_path, fold)
            metrics, _equity = _metrics(result, table, ctx)
            fold_results.append(
                {
                    "start": fold.start.isoformat(),
                    "end": fold.end.isoformat(),
                    **metrics,
                }
            )

    random_cagrs, random_note = _random_cagrs(ctx, variant_id, table, period, seeds)
    random_percentile = None
    if random_cagrs:
        cagr = float(main_metrics["cagr"])
        below = sum(1 for value in random_cagrs if value < cagr)
        random_percentile = below / len(random_cagrs) * 100.0

    result: dict = {
        "variant_id": variant_id,
        "period": period.name,
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
        "table_path": str(table_path),
        "main": main_metrics,
        "main_metrics": main_metrics,
        "stress": stress_metrics,
        "stress_metrics": stress_metrics,
        "folds": fold_results,
        "random_cagrs": random_cagrs,
        "random_percentile": random_percentile,
        "random_note": random_note,
        "gate": None,
    }

    if period.name == "holdout":
        result["bh_cagr"] = _bh_cagr(ctx, period)
        result["gate"] = gate_result(
            main_metrics,
            stress_metrics,
            result["bh_cagr"],
            random_cagrs,
        )

    ctx.registry.record(
        variant_id=variant_id,
        period=period.name,
        table_hash=table_path.stem,
        code_hash=file_hash(Path(__file__)),
        sharpe_daily=float(curve_stats(main_equity)["sharpe_daily"]),
        n_obs=int(main_equity.height),
    )
    return result


def trading_days(bars: pl.DataFrame, period: Period) -> list[date]:
    """Sorted stored trading dates inside ``period``."""
    if bars.is_empty() or "date" not in bars.columns:
        return []
    return [d for d in bars["date"].unique().sort().to_list() if period.start <= d <= period.end]


__all__ = [
    "EvalContext",
    "Period",
    "dev_period",
    "equity_frame",
    "evaluate_variant",
    "finalize_trials",
    "folds",
    "gate_checks",
    "gate_result",
    "holdout_period",
    "run_table",
    "trading_days",
]
