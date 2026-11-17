"""End-to-end: synthetic store -> real engines -> compose -> evaluate -> report.

The test never touches the configured data directory or the real research
outputs: everything lives under ``tmp_path`` and ``settings.data_dir`` is
pointed there as well.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.config import settings
from app.indicators.pipeline import run_pipeline
from app.research.bars import load_bars, tradable_symbols
from app.research.chan_replay import load_chan_gone, load_chan_points, run_chan_replay
from app.research.compose import compose_table, with_atr14
from app.research.evaluate import (
    EvalContext,
    dev_period,
    evaluate_variant,
    finalize_trials,
    trading_days,
)
from app.research.hypotheses import VARIANTS, Inputs, variant_path
from app.research.integrity import (
    chan_truncation_check,
    determinism_check,
    phantom_rate,
    wyckoff_truncation_check,
)
from app.research.registry import Registry
from app.research.universe import UniverseParams, eligibility
from app.research.wyckoff.replay import (
    load_wyckoff_events,
    load_wyckoff_ranges,
    run_wyckoff_replay,
)
from scripts.research import evaluate as eval_cli

from .wyckoff.shapes import accumulation

SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE")
SPY = "SPY"
N_DAYS = 300
START = date(2023, 1, 2)
PARAMS = UniverseParams(min_bars=120, min_price=1.0, min_dollar_vol=0.0, top_n=10)
VARIANTS_TO_COMPOSE = ("H1-2B", "H2-sos")

REPORT_HEADINGS = (
    "# Research report:",
    "## Summary",
    "## Baselines",
    "## Cost stress",
    "## Dev folds",
    "## Integrity",
    "## Trials",
    "## Caveats",
    "## Deviations",
)


def _trading_days(n: int) -> list[date]:
    days: list[date] = []
    day = START
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _candles(seed: int) -> tuple[list[float], ...]:
    """The ported Wyckoff accumulation cycle extended to ``N_DAYS`` bars."""
    candles, _marks = accumulation(seed=seed)
    open_ = [float(value) for value in candles.o]
    high = [float(value) for value in candles.h]
    low = [float(value) for value in candles.l]
    close = [float(value) for value in candles.c]
    volume = [float(value) for value in candles.v]

    while len(close) < N_DAYS:
        previous = close[-1]
        open_.append(previous)
        close.append(previous + 0.4)
        high.append(previous + 0.9)
        low.append(previous - 0.4)
        volume.append(120.0)

    return (
        open_[:N_DAYS],
        high[:N_DAYS],
        low[:N_DAYS],
        close[:N_DAYS],
        volume[:N_DAYS],
    )


def _write_store(data_dir: Path) -> None:
    """``kline_daily`` partitions for five stocks plus SPY, and instruments."""
    days = _trading_days(N_DAYS)
    series = {symbol: _candles(seed=index) for index, symbol in enumerate(SYMBOLS)}
    series[SPY] = _candles(seed=99)

    for index, day in enumerate(days):
        rows = []
        for symbol, (open_, high, low, close, volume) in series.items():
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "open": open_[index],
                    "high": high[index],
                    "low": low[index],
                    "close": close[index],
                    "volume": volume[index],
                    "amount": close[index] * volume[index],
                    "quote_ts": 0,
                }
            )
        path = data_dir / "kline_daily" / f"date={day.isoformat()}"
        path.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows).write_parquet(path / "part.parquet")

    instruments = pl.DataFrame(
        [
            {
                "symbol": symbol,
                "name": symbol,
                "asset_type": "stock",
                "instrument_type": "common",
                "is_etf": False,
                "is_preferred": False,
                "is_warrant": False,
                "is_unit": False,
            }
            for symbol in SYMBOLS
        ]
    )
    (data_dir / "instruments").mkdir(parents=True, exist_ok=True)
    instruments.write_parquet(data_dir / "instruments" / "instruments.parquet")


def _write_table(path: Path, table: pl.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table.write_parquet(path, compression="zstd", statistics=False)


@pytest.mark.slow
def test_e2e_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "store"
    monkeypatch.setattr(settings, "data_dir", data_dir)

    # 1. Synthetic broker store -> the ordinary enriched pipeline.
    _write_store(data_dir)
    assert run_pipeline(data_dir=data_dir) > 0

    # 2. Real Chan and Wyckoff replays, in-process.
    assert run_chan_replay(data_dir, list(SYMBOLS), workers=1)["failed"] == []
    assert run_wyckoff_replay(data_dir, list(SYMBOLS), workers=1)["failed"] == []

    # 3. Compose two variants with the e2e universe parameters.
    bars = with_atr14(load_bars(data_dir))
    elig = eligibility(bars, tradable_symbols(data_dir), PARAMS)
    inputs = Inputs(
        chan_points=load_chan_points(data_dir),
        wyk_events=load_wyckoff_events(data_dir),
        wyk_ranges=load_wyckoff_ranges(data_dir),
        bars=bars,
        spy=load_bars(data_dir, symbols=[SPY]),
    )
    tables: dict[str, Path] = {}
    for variant_id in VARIANTS_TO_COMPOSE:
        table, _log = compose_table(VARIANTS[variant_id](inputs), bars, elig)
        path = variant_path(data_dir, variant_id, table)
        _write_table(path, table)
        tables[variant_id] = path

    # 4. Evaluate dev through ZenTrade's ordinary service.
    period = dev_period(elig, top_n=int(PARAMS.top_n))
    registry = Registry(data_dir / "research" / "registry.json")
    ctx = EvalContext(
        svc=eval_cli._build_service(data_dir),
        registry=registry,
        data_dir=data_dir,
        tables=tables,
        bars=bars,
        elig=elig,
        spy=load_bars(data_dir, symbols=[SPY]),
        work_dir=data_dir / "research" / "tmp",
        n_trials=registry.n_trials(),
        var_sr_trials=registry.var_sr_trials(),
        trading_days=trading_days(bars, period),
        random_runner=None,
    )
    results = [
        evaluate_variant(ctx, variant_id, period, seeds=3)
        for variant_id in VARIANTS_TO_COMPOSE
    ]
    finalize_trials(results, registry.n_trials(), registry.var_sr_trials())

    # 5. Integrity checks over the real stored parts.
    integrity = {
        "chan_truncation": chan_truncation_check(
            data_dir, list(SYMBOLS), cuts_per_symbol=2, seed=0
        ),
        "wyckoff_truncation": wyckoff_truncation_check(
            data_dir, list(SYMBOLS), cuts_per_symbol=2, seed=0
        ),
        "phantom_rate": phantom_rate(
            load_chan_points(data_dir), load_chan_gone(data_dir)
        ).to_dicts(),
        "determinism": determinism_check(data_dir, list(SYMBOLS)),
    }

    # 6. Write the report through the CLI's own call site.
    monkeypatch.setattr(eval_cli, "REPO", tmp_path)
    deviations = tmp_path / "DEVIATIONS.md"
    deviations.write_text("# Deviations\n\n- e2e synthetic store\n", encoding="utf-8")
    monkeypatch.setattr(eval_cli, "DEVIATIONS_PATH", deviations)
    report_path = eval_cli._write_report(
        data_dir, period, results, integrity, registry, None, elig, bars, ctx.spy
    )

    # Assertions.
    assert report_path is not None
    assert report_path.exists()
    text = report_path.read_text(encoding="utf-8")
    for heading in REPORT_HEADINGS:
        assert heading in text, heading
    for result in results:
        assert result["main"]["trades"] >= 0
    dev_rows = [row for row in registry.rows() if row["period"] == "dev"]
    assert len(dev_rows) == 2
    assert integrity["chan_truncation"]["mismatches"] == []
    assert integrity["wyckoff_truncation"]["mismatches"] == []
    assert integrity["determinism"]["different"] == []