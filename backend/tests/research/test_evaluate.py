"""Evaluation runner: folds, gate, stress and registry wiring."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.research import DEV_END, HOLDOUT_END, HOLDOUT_START
from app.research import evaluate as ev
from app.research.registry import HoldoutLocked, Registry


def _elig(rows: list[tuple[date, int]]) -> pl.DataFrame:
    """Eligibility frame with ``n`` eligible symbols on each listed date."""
    data: list[dict] = []
    for d, count in rows:
        for index in range(count):
            data.append({"symbol": f"S{index:02d}", "date": d, "eligible": True})
    return pl.DataFrame(
        data,
        schema={"symbol": pl.String, "date": pl.Date, "eligible": pl.Boolean},
    )


def _entry_table(path, entries: list[tuple[str, date]]) -> pl.DataFrame:
    frame = pl.DataFrame(
        {
            "symbol": [symbol for symbol, _d in entries],
            "date": [d for _symbol, d in entries],
            "entry": [True for _ in entries],
            "exit": [False for _ in entries],
            "score": [1.0 for _ in entries],
            "invalidation": [99.0 for _ in entries],
            "atr14": [2.0 for _ in entries],
            "close": [100.0 for _ in entries],
        },
        schema_overrides={
            "date": pl.Date,
            "entry": pl.Boolean,
            "exit": pl.Boolean,
        },
    )
    frame.write_parquet(path)
    return frame


def _random_bars(days: list[date]) -> pl.DataFrame:
    rows: list[dict] = []
    for d in days:
        for symbol in ("AAA", "BBB", "CCC"):
            rows.append(
                {
                    "symbol": symbol,
                    "date": d,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "atr14": 2.0,
                }
            )
    return pl.DataFrame(rows, schema_overrides={"date": pl.Date})


def _table(path) -> pl.DataFrame:
    frame = pl.DataFrame(
        {
            "symbol": ["AAA"],
            "date": [date(2024, 1, 2)],
            "entry": [True],
            "exit": [False],
            "score": [0.5],
            "invalidation": [99.0],
            "atr14": [2.0],
            "close": [100.0],
        },
        schema_overrides={
            "date": pl.Date,
            "entry": pl.Boolean,
            "exit": pl.Boolean,
        },
    )
    frame.write_parquet(path)
    return frame


def _fake_result(start: date, end: date, value: float = 100.0):
    return SimpleNamespace(
        trades=[],
        equity_curve=[
            {"date": start.isoformat(), "value": value},
            {"date": end.isoformat(), "value": value},
        ],
    )


# ---------------------------------------------------------------------------
# Periods and folds
# ---------------------------------------------------------------------------


def test_dev_period_starts_at_full_universe() -> None:
    elig = _elig(
        [
            (date(2023, 9, 22), 2),
            (date(2023, 11, 16), 5),
            (date(2023, 11, 17), 7),
            (date(2023, 11, 20), 7),
        ]
    )
    period = ev.dev_period(elig)
    assert period.name == "dev"
    assert period.start == date(2023, 11, 17)
    assert period.end == DEV_END


def test_holdout_period_bounds() -> None:
    period = ev.holdout_period()
    assert period.name == "holdout"
    assert period.start == HOLDOUT_START
    assert period.end == HOLDOUT_END


def test_folds_embargo() -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(400)]
    period = ev.Period("dev", days[0], days[-1])

    result = ev.folds(period, days, test_len=126, embargo=5)

    assert len(result) == 3
    assert [fold.start for fold in result] == [days[0], days[131], days[262]]
    assert [fold.end for fold in result] == [days[125], days[256], days[387]]
    for fold in result:
        assert (fold.end - fold.start).days == 125
        assert fold.start >= period.start
        assert fold.end <= period.end


def test_folds_tail_window() -> None:
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(499)]
    period = ev.Period("dev", days[0], days[-1])

    result = ev.folds(period, days, test_len=126, embargo=5)

    assert len(result) == 4
    assert result[3].start == days[393]
    assert result[3].end == days[498]

    short = days[:433]
    short_period = ev.Period("dev", short[0], short[-1])
    assert len(ev.folds(short_period, short, test_len=126, embargo=5)) == 3


# ---------------------------------------------------------------------------
# Engine result adaptation and run_table wiring
# ---------------------------------------------------------------------------


def test_equity_frame_adapts_engine_curve() -> None:
    result = SimpleNamespace(
        equity_curve=[
            {"date": datetime(2024, 1, 3, 15, 30), "value": 101.0},
            {"date": "2024-01-02", "value": 100.0},
            {"date": "2024-01-04", "equity": 102.0},
        ]
    )
    frame = ev.equity_frame(result)

    assert frame.columns == ["date", "equity"]
    assert frame["date"].to_list() == [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    ]
    assert frame["equity"].to_list() == [100.0, 101.0, 102.0]


def test_run_table_config(tmp_path) -> None:
    seen: list = []

    class _Svc:
        def run(self, config):
            seen.append(config)
            return "result"

    path = tmp_path / "table.parquet"
    period = ev.Period("dev", date(2024, 1, 2), date(2024, 3, 1))

    assert ev.run_table(_Svc(), path, period, slippage_bps=15.0) == "result"
    config = seen[0]
    assert config.strategy_id == "research_signal_table"
    assert config.symbols is None
    assert config.start == period.start
    assert config.end == period.end
    assert config.params == {"table_path": str(path)}
    assert config.max_positions == 10
    assert config.position_sizing == "equal"
    assert config.entry_fill == "open_t+1"
    assert config.exit_fill == "open_t+1"
    assert config.slippage_bps == 15.0
    assert config.mode == "position"


def test_run_table_passes_max_positions(tmp_path) -> None:
    seen: list = []

    class _Svc:
        def run(self, config):
            seen.append(config)
            return "result"

    path = tmp_path / "table.parquet"
    period = ev.Period("dev", date(2024, 1, 2), date(2024, 3, 1))

    ev.run_table(_Svc(), path, period, max_positions=5)
    ev.run_table(_Svc(), path, period)

    assert seen[0].max_positions == 5
    assert seen[1].max_positions == 10


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _passing_main() -> dict:
    return {"cagr": 0.20, "trades": 150, "avg_r": 0.10, "dsr": 0.95}


def test_gate_all_checks() -> None:
    main = _passing_main()
    bh = 0.10
    randoms = [0.05, 0.10, 0.15]

    gate = ev.gate_result(main, main, bh, randoms)
    assert gate["checks"] == {
        "cagr_vs_bh": True,
        "cagr_vs_rand95": True,
        "trades_ge_100": True,
        "avg_r_pos": True,
        "dsr_ge_0_9": True,
    }
    assert gate["passed"] is True
    assert gate["fragile_to_costs"] is False

    flips = {
        "cagr_vs_bh": ({"cagr": 0.05}, bh, randoms),
        "cagr_vs_rand95": (main, bh, [10.0]),
        "trades_ge_100": ({"trades": 99}, bh, randoms),
        "avg_r_pos": ({"avg_r": -0.01}, bh, randoms),
        "dsr_ge_0_9": ({"dsr": 0.89}, bh, randoms),
    }
    for check, (override, bh_value, random_values) in flips.items():
        flipped = {**main, **override}
        result = ev.gate_result(flipped, flipped, bh_value, random_values)
        assert result["checks"][check] is False, check
        assert result["passed"] is False, check


def test_fragile_to_costs_flag() -> None:
    main = _passing_main()
    stress = {**main, "cagr": 0.05}

    gate = ev.gate_result(main, stress, 0.10, [0.05, 0.10, 0.15])

    assert gate["passed"] is True
    assert gate["fragile_to_costs"] is True


# ---------------------------------------------------------------------------
# Evaluation and registry
# ---------------------------------------------------------------------------


def test_evaluate_records_registry_row(tmp_path, monkeypatch) -> None:
    table_path = tmp_path / "table.parquet"
    _table(table_path)
    registry = Registry(tmp_path / "registry.json")
    period = ev.Period("dev", date(2024, 1, 2), date(2024, 3, 1))

    def fake_run_table(svc, path, period, slippage_bps=5.0, max_positions=10):
        return _fake_result(period.start, period.end)

    monkeypatch.setattr(ev, "run_table", fake_run_table)

    ctx = ev.EvalContext(
        svc=None,
        registry=registry,
        data_dir=tmp_path,
        tables={"H1-2B": table_path},
        trading_days=[],
        work_dir=tmp_path,
    )
    result = ev.evaluate_variant(ctx, "H1-2B", period, seeds=0)

    assert result["variant_id"] == "H1-2B"
    assert result["period"] == "dev"
    assert result["main"]["trades"] == 0
    rows = registry.rows()
    assert len(rows) == 1
    assert rows[0]["variant_id"] == "H1-2B"
    assert rows[0]["period"] == "dev"
    assert rows[0]["table_hash"] == table_path.stem


def test_slots_reach_every_run(tmp_path) -> None:
    table_path = tmp_path / "V.parquet"
    other_path = tmp_path / "W.parquet"
    _table(table_path)
    _table(other_path)
    registry = Registry(tmp_path / "registry.json")
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(300)]
    period = ev.Period("dev", days[0], days[-1])
    seen: list = []

    class _Svc:
        def run(self, config):
            seen.append(config)
            return _fake_result(config.start, config.end)

    ctx = ev.EvalContext(
        svc=_Svc(),
        registry=registry,
        data_dir=tmp_path,
        tables={"V": table_path, "W": other_path},
        bars=_random_bars([days[0], days[1]]),
        elig=_elig([(days[0], 3), (days[1], 3)]),
        work_dir=tmp_path / "tmp",
        trading_days=days,
        configs={"V": {"base": "V", "rank": "rs", "slots": 20}},
    )

    result = ev.evaluate_variant(ctx, "V", period, seeds=2)

    assert result["config"] == {"base": "V", "rank": "rs", "slots": 20}
    assert len(seen) == 2 + len(ev.folds(period, days)) + 2
    assert all(config.max_positions == 20 for config in seen)

    seen.clear()
    result = ev.evaluate_variant(ctx, "W", period, seeds=2)

    assert result["config"] == {"base": "W", "rank": "hash", "slots": 10}
    assert all(config.max_positions == 10 for config in seen)


def test_random_cache_hit_same_entries(tmp_path) -> None:
    day = date(2024, 1, 2)
    first_path = tmp_path / "A.parquet"
    frame = _entry_table(first_path, [("AAA", day)])
    second_path = tmp_path / "B.parquet"
    frame.with_columns(pl.lit(0.9).alias("score")).write_parquet(second_path)
    registry = Registry(tmp_path / "registry.json")
    period = ev.Period("dev", day, date(2024, 3, 1))
    cache_dir = tmp_path / "random-cache"

    class _Svc:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, config):
            self.calls += 1
            return _fake_result(config.start, config.end)

    svc = _Svc()

    def make_ctx() -> ev.EvalContext:
        return ev.EvalContext(
            svc=svc,
            registry=registry,
            data_dir=tmp_path,
            tables={"A": first_path, "B": second_path},
            bars=_random_bars([day, date(2024, 1, 3)]),
            elig=_elig([(day, 3), (date(2024, 1, 3), 3)]),
            work_dir=tmp_path / "tmp",
            trading_days=[],
            random_cache_dir=cache_dir,
        )

    first = ev.evaluate_variant(make_ctx(), "A", period, seeds=2)
    calls_after_first = svc.calls
    second = ev.evaluate_variant(make_ctx(), "B", period, seeds=2)

    assert first["random_cache_hit"] is False
    assert second["random_cache_hit"] is True
    assert svc.calls - calls_after_first == 2
    assert second["random_cagrs"] == first["random_cagrs"]
    assert list(cache_dir.glob("*.json"))

    fresh = ev.evaluate_variant(make_ctx(), "A", period, seeds=2)
    assert fresh["random_cache_hit"] is True
    assert fresh["random_cagrs"] == first["random_cagrs"]


def test_random_cache_misses_on_slots_or_counts(tmp_path) -> None:
    day = date(2024, 1, 2)
    base_path = tmp_path / "base.parquet"
    extra_path = tmp_path / "extra.parquet"
    _entry_table(base_path, [("AAA", day)])
    _entry_table(extra_path, [("AAA", day), ("BBB", day)])
    period = ev.Period("dev", day, date(2024, 3, 1))
    cache_dir = tmp_path / "random-cache"

    class _Svc:
        def run(self, config):
            return _fake_result(config.start, config.end)

    def make_ctx(table_path, slots: int, salt: str = "") -> ev.EvalContext:
        return ev.EvalContext(
            svc=_Svc(),
            registry=Registry(tmp_path / f"registry-{slots}-{salt}.json"),
            data_dir=tmp_path,
            tables={"A": table_path},
            bars=_random_bars([day, date(2024, 1, 3)]),
            elig=_elig([(day, 3), (date(2024, 1, 3), 3)]),
            work_dir=tmp_path / "tmp",
            trading_days=[],
            configs={"A": {"base": "A", "rank": "hash", "slots": slots}},
            random_cache_dir=cache_dir,
            cache_salt=salt,
        )

    first = ev.evaluate_variant(make_ctx(base_path, 5), "A", period, seeds=2)
    assert first["random_cache_hit"] is False
    assert first["random_cagrs"]

    slots_miss = ev.evaluate_variant(make_ctx(base_path, 10), "A", period, seeds=2)
    assert slots_miss["random_cache_hit"] is False

    counts_miss = ev.evaluate_variant(make_ctx(extra_path, 10), "A", period, seeds=2)
    assert counts_miss["random_cache_hit"] is False

    salt_miss = ev.evaluate_variant(make_ctx(base_path, 10, salt="x"), "A", period, seeds=2)
    assert salt_miss["random_cache_hit"] is False


def test_random_runner_receives_slots(tmp_path) -> None:
    day = date(2024, 1, 2)
    table_path = tmp_path / "V.parquet"
    _table(table_path)
    period = ev.Period("dev", day, date(2024, 3, 1))
    calls: list[int] = []

    def runner(paths, period, max_positions):
        calls.append(max_positions)
        return [0.1 for _ in paths]

    class _Svc:
        def run(self, config):
            return _fake_result(config.start, config.end)

    ctx = ev.EvalContext(
        svc=_Svc(),
        registry=Registry(tmp_path / "registry.json"),
        data_dir=tmp_path,
        tables={"V": table_path},
        bars=_random_bars([day, date(2024, 1, 3)]),
        elig=_elig([(day, 3), (date(2024, 1, 3), 3)]),
        work_dir=tmp_path / "tmp",
        trading_days=[],
        configs={"V": {"base": "V", "rank": "rs", "slots": 7}},
        random_runner=runner,
    )

    result = ev.evaluate_variant(ctx, "V", period, seeds=2)

    assert calls == [7]
    assert result["random_cagrs"] == [0.1, 0.1]


# ---------------------------------------------------------------------------
# CLI holdout lock
# ---------------------------------------------------------------------------


def test_holdout_requires_freeze(tmp_path, monkeypatch) -> None:
    from scripts.research import evaluate as cli

    called: list = []
    monkeypatch.setattr(ev, "run_table", lambda *a, **k: called.append(1))

    with pytest.raises(HoldoutLocked):
        cli.main(["--period", "holdout", "--data-dir", str(tmp_path)])

    assert called == []


def test_finalize_trials_uses_final_count_and_regates() -> None:
    """DSR computed mid-pass (N=1) must be replaced by the final-N value."""
    from app.research.evaluate import finalize_trials
    from app.research.metrics import dsr

    curve = {"sharpe_daily": 0.08, "skew": 0.0, "kurtosis": 3.0, "n_days": 252}
    main = {"trades": 150, "avg_r": 0.3, "cagr": 0.5, "dsr": 0.99, "curve": dict(curve)}
    stress = {"trades": 150, "avg_r": 0.3, "cagr": 0.5, "dsr": 0.99, "curve": dict(curve)}
    result = {"period": "holdout", "main": main, "stress": stress, "bh_cagr": 0.1,
              "random_cagrs": [0.0] * 200, "gate": None}

    finalize_trials([result], n_trials=18, var_sr_trials=0.002)

    expected = dsr(0.08, 251, 0.0, 3.0, 0.002, 18)
    assert result["main"]["dsr"] == pytest.approx(expected)
    assert result["main"]["dsr"] < 0.99
    assert result["n_trials"] == 18
    assert result["gate"]["checks"]["dsr_ge_0_9"] is (expected >= 0.9)
