"""Export one frozen research variant as the strategy replay page's data file.

Usage::

    cd backend
    uv run python scripts/research/export_viz.py --period dev \
        [--variant H1-2B~retr] [--data-dir ...] [--out ...]

The exporter runs the composed table once through the ordinary backtest service
at the main slippage, then writes ``<out>/<variant>-<period>.js`` and
``<out>/current.js`` (identical content).  A holdout export is refused unless
the frozen registry allows the variant with the current pins.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402


def _load_evaluate_module():
    """Load ``scripts/research/evaluate.py`` without hand-copying its pin logic."""
    path = Path(__file__).with_name("evaluate.py")
    spec = importlib.util.spec_from_file_location("zt_research_evaluate_export", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _period(evaluate_module, args, data_dir: Path):
    if args.period == "holdout":
        return evaluate_module.holdout_period()

    from app.research.bars import load_bars, tradable_symbols
    from app.research.compose import with_atr14
    from app.research.universe import UniverseParams, eligibility

    bars = with_atr14(load_bars(data_dir))
    elig = eligibility(bars, tradable_symbols(data_dir), UniverseParams())
    return evaluate_module.dev_period(elig)


def _traded_symbols(trades: list) -> list[str]:
    from app.research.metrics import _field

    symbols: list[str] = []
    for trade in trades:
        symbol = _field(trade, "symbol")
        if symbol is not None and str(symbol) not in symbols:
            symbols.append(str(symbol))
    return symbols


def _load_points(data_dir: Path, symbols: list[str], retr_spec) -> object | None:
    import polars as pl

    from app.research.versions import chan_engine_version

    if retr_spec is None or not symbols:
        return None
    points_dir = data_dir / "research" / "chan" / chan_engine_version() / "points"
    frames = []
    for symbol in symbols:
        path = points_dir / f"{symbol}.parquet"
        if path.exists():
            frames.append(pl.read_parquet(path))
    if not frames:
        return None
    return pl.concat(frames, how="diagonal_relaxed")


def _bars_window_start(period, trades: list, points, retr_spec):
    """``min(period.start - 180 days, earliest leg date of a traded point)``."""
    from app.research.metrics import _as_iso, _field
    from app.research.viz_export import _point_candidates, _point_leg_dates

    window_start = period.start - timedelta(days=180)
    if points is None or retr_spec is None:
        return window_start

    by_symbol: dict[str, set[str]] = {}
    for trade in trades:
        symbol = _field(trade, "symbol")
        signal_date = _as_iso(_field(trade, "entry_signal_date"))
        if symbol is None or signal_date is None:
            continue
        by_symbol.setdefault(str(symbol), set()).add(signal_date)

    for symbol, signal_dates in by_symbol.items():
        for signal_date in signal_dates:
            for point in _point_candidates(points, symbol, signal_date, retr_spec):
                for leg_date in _point_leg_dates(point):
                    if leg_date < window_start:
                        window_start = leg_date
    return window_start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export one research variant as data/current.js for the replay page"
    )
    parser.add_argument("--variant", default="H1-2B~retr", help="variant id")
    parser.add_argument("--period", required=True, choices=("dev", "holdout"))
    parser.add_argument(
        "--data-dir",
        default=str(settings.data_dir),
        help="ZenTrade data directory (default: configured data dir)",
    )
    parser.add_argument(
        "--out",
        default=str(REPO / "docs" / "research" / "viz" / "data"),
        help="output directory (default: <repo>/docs/research/viz/data)",
    )
    args = parser.parse_args(argv)

    import polars as pl

    from app.research.evaluate import equity_frame, run_table
    from app.research.hypotheses import _RETR_SPECS, VARIANTS, variant_config
    from app.research.metrics import summarize
    from app.research.registry import HoldoutLocked, check_holdout_allowed
    from app.research.viz_export import build_payload, write_data_js

    try:
        config = asdict(variant_config(args.variant))
    except KeyError:
        parser.error(f"unknown variant id: {args.variant}")

    slots = int(config["slots"])
    retr_spec = _RETR_SPECS.get(config["base"])
    data_dir = Path(args.data_dir).resolve()
    frozen_path = data_dir / "research" / "registry.frozen.json"
    evaluate_module = _load_evaluate_module()

    # Lock before touching the store, exactly like the evaluate script.
    if args.period == "holdout":
        if not frozen_path.exists():
            print(f"export_viz: holdout locked: no freeze file at {frozen_path}", file=sys.stderr)
            return 1
        # Pins cover every frozen variant (table_hashes, variant_configs).
        pins = evaluate_module._current_pins(data_dir, list(VARIANTS))
        try:
            check_holdout_allowed(frozen_path, pins, args.variant)
        except HoldoutLocked as exc:
            print(f"export_viz: {exc}", file=sys.stderr)
            return 1

    table_path = evaluate_module._latest_table(data_dir, args.variant)
    if table_path is None:
        print(
            f"export_viz: no composed table for {args.variant}; run compose first",
            file=sys.stderr,
        )
        return 1

    period = _period(evaluate_module, args, data_dir)
    service = evaluate_module._build_service(data_dir)
    result = run_table(service, table_path, period, max_positions=slots)
    if isinstance(result, dict):
        trades = list(result.get("trades") or [])
    else:
        trades = list(getattr(result, "trades", None) or [])
    equity = equity_frame(result)
    table = pl.read_parquet(table_path)

    symbols = _traded_symbols(trades)
    points = _load_points(data_dir, symbols, retr_spec)
    window_start = _bars_window_start(period, trades, points, retr_spec)

    from app.research.bars import load_bars

    bars = load_bars(
        data_dir,
        symbols=sorted({*symbols, "SPY"}),
        start=window_start,
        end=period.end,
    )

    stats = summarize(
        trades,
        equity,
        table,
        n_trials=1,
        var_sr_trials=0.0,
        slots=slots,
    )
    payload = build_payload(
        variant_id=args.variant,
        config=config,
        period=period,
        trades=trades,
        equity=equity,
        table=table,
        bars=bars,
        points=points,
        retr_spec=retr_spec,
        stats=stats,
        holdout=args.period == "holdout",
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )

    out_dir = Path(args.out).resolve()
    period_path = out_dir / f"{args.variant}-{period.name}.js"
    current_path = out_dir / "current.js"
    write_data_js(payload, period_path)
    write_data_js(payload, current_path)

    signal_count = sum(len(day["items"]) for day in payload["signals"])
    print(f"export_viz: wrote {period_path}")
    print(f"export_viz: wrote {current_path}")
    print(
        "export_viz: "
        f"{len(payload['days'])} days, {len(payload['trades'])} trades, "
        f"{signal_count} signals, {len(payload['bars'])} bar series"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())