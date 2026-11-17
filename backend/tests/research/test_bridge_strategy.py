from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.backtest.engine import BacktestEngine, SimResult
from app.backtest.matrix import build_market_data_matrix
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.ai_generator import AIStrategyGenerator
from app.strategy.engine import StrategyEngine

STRATEGY_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "strategy"
    / "builtin"
    / "research_signal_table.py"
)


class _RepoStub:
    def __init__(self) -> None:
        self.store = SimpleNamespace(data_dir=None)

    def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
        return pl.DataFrame()


class _StrategyEngineStub:
    def __init__(self, strategy) -> None:
        self.strategy = strategy

    def get(self, strategy_id: str):
        return self.strategy


class _EngineStub:
    """Reuses the panel/matrix stub pattern from tests/backtest."""

    def __init__(self, panel: pl.DataFrame) -> None:
        self.panel = panel
        self.repo = _RepoStub()
        self.sim_matrix = None

    def load_panel_for_backtest(
        self,
        symbols,
        start,
        end,
        feature_plan,
        asset_type="stock",
    ) -> pl.DataFrame:
        del symbols, start, end, feature_plan, asset_type
        return self.panel

    def load_market_data_matrix_for_backtest(
        self,
        symbols,
        start,
        end,
        feature_plan,
        asset_type="stock",
        **kwargs,
    ):
        del kwargs
        panel = self.load_panel_for_backtest(
            symbols,
            start,
            end,
            feature_plan,
            asset_type=asset_type,
        )
        field_columns = (
            set(feature_plan.base_columns)
            | set(feature_plan.instrument_columns)
            | set(feature_plan.matrix_columns)
        )
        return build_market_data_matrix(panel, field_columns=field_columns)

    def simulate_market_matrix(self, matrix, config, progress_cb=None, cancel_event=None, options=None) -> SimResult:
        del progress_cb, cancel_event, options
        self.sim_matrix = matrix
        return SimResult(
            equity_curve=[{"date": "2024-01-01", "value": config.initial_capital}],
            drawdown_curve=[{"date": "2024-01-01", "value": 0.0}],
            trades=[],
            per_symbol_stats=[],
            stats={"total_return": 0.0, "n_trades": 0},
        )


def _panel(dates: list[date], symbols: tuple[str, ...] = ("A",)) -> pl.DataFrame:
    rows = []
    for symbol in symbols:
        for index, day in enumerate(dates):
            price = 10.0 + index
            rows.append({
                "symbol": symbol,
                "name": symbol,
                "date": day,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1_000_000.0,
                "amount": 10_000_000.0,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _write_table(path: Path, rows: list[dict]) -> str:
    pl.DataFrame(rows, schema_overrides={"date": pl.Date}).write_parquet(path)
    return str(path)


def _stub_service(panel: pl.DataFrame, table_path: str):
    strategy = StrategyEngine._load_file(STRATEGY_PATH)
    engine = _EngineStub(panel)
    service = StrategyBacktestService(
        engine=engine,  # type: ignore[arg-type]
        strategy_engine=_StrategyEngineStub(strategy),  # type: ignore[arg-type]
    )
    return service, engine, {"table_path": table_path}


def test_bridge_loads_through_strategy_engine():
    service = StrategyEngine(strategy_dirs=[STRATEGY_PATH.parent])
    assert service.has("research_signal_table")
    loaded = service.get("research_signal_table")

    assert loaded.execution_backend == "matrix_native"
    assert loaded.meta["id"] == "research_signal_table"
    assert loaded.meta["scoring"] == {}
    assert loaded.meta["order_by"] == "score"
    assert loaded.meta["descending"] is True
    assert loaded.meta["limit"] == 100000
    assert loaded.basic_filter.get("enabled") is False
    assert loaded.matrix_strategy is not None
    assert loaded.matrix_strategy.required_fields() == frozenset({"close"})
    assert loaded.matrix_strategy.required_warmup_bars({}) == 1

    # The source must survive the AI-strategy AST import whitelist unchanged.
    AIStrategyGenerator._validate_safety(STRATEGY_PATH.read_text(encoding="utf-8"))


def test_bridge_maps_table_to_matrix(tmp_path):
    strategy = StrategyEngine._load_file(STRATEGY_PATH)
    table_path = _write_table(
        tmp_path / "table.parquet",
        [
            {"symbol": "A", "date": date(2024, 1, 2), "entry": True, "exit": False, "score": 7.5},
            {"symbol": "B", "date": date(2024, 1, 3), "entry": False, "exit": True, "score": 0.0},
            {"symbol": "ZZZ", "date": date(2024, 1, 2), "entry": True, "exit": False, "score": 9.0},
            {"symbol": "A", "date": date(2024, 2, 1), "entry": True, "exit": False, "score": 3.0},
        ],
    )
    market = SimpleNamespace(
        shape=(2, 2),
        symbols=("A", "B"),
        timestamp_labels=("2024-01-02", "2024-01-03T00:00:00"),
    )

    signals = strategy.matrix_strategy.compute_signals(market, {"table_path": table_path})

    assert signals.entry.tolist() == [[1, 0], [0, 0]]
    assert signals.exit.tolist() == [[0, 0], [0, 1]]
    assert signals.score.tolist() == [[7.5, 0.0], [0.0, 0.0]]
    assert signals.entry_signal_code.tolist() == [[0, -1], [-1, -1]]
    assert signals.exit_signal_code.tolist() == [[-1, -1], [-1, 0]]
    assert strategy.matrix_strategy.ignored_rows == 2


def test_bridge_requires_table_path(tmp_path):
    strategy = StrategyEngine._load_file(STRATEGY_PATH)
    market = SimpleNamespace(shape=(1, 1), symbols=("A",), timestamp_labels=("2024-01-02",))

    try:
        strategy.matrix_strategy.compute_signals(market, {"table_path": ""})
    except ValueError as exc:
        assert str(exc) == "table_path required"
    else:  # pragma: no cover - explicit failure message
        raise AssertionError("empty table_path must raise ValueError")


def test_bridge_passthrough_in_service(tmp_path):
    dates = [date(2024, 1, 2), date(2024, 1, 3)]
    panel = _panel(dates, symbols=("A", "B"))
    table_path = _write_table(
        tmp_path / "table.parquet",
        [
            {"symbol": "A", "date": dates[0], "entry": True, "exit": False, "score": 1.0},
            {"symbol": "B", "date": dates[0], "entry": True, "exit": False, "score": 2.0},
            {"symbol": "ZZZ", "date": dates[0], "entry": True, "exit": False, "score": 3.0},
        ],
    )
    service, engine, params = _stub_service(panel, table_path)

    result = service.run(StrategyBacktestConfig(
        strategy_id="research_signal_table",
        symbols=None,
        start=dates[0],
        end=dates[-1],
        params=params,
        matching="open_t+1",
        mode="position",
    ))

    assert result.error is None
    assert engine.sim_matrix is not None
    assert engine.sim_matrix.entry[:, :].sum() == 2
    assert result.stats["execution_backend"] == "matrix_native"
    assert result.stats["selection"]["entry_candidates"] == 2
    assert result.stats["selection"]["entry_trigger_filtered"] == 0
    assert result.stats["selection"]["entry_trigger_enabled"] is False


def test_bridge_no_reentry_while_held(tmp_path):
    dates = [date(2024, 1, 2) + timedelta(days=i) for i in range(5)]
    panel = _panel(dates)
    table_path = _write_table(
        tmp_path / "table.parquet",
        [
            {"symbol": "A", "date": dates[0], "entry": True, "exit": False, "score": 1.0},
            {"symbol": "A", "date": dates[1], "entry": True, "exit": False, "score": 2.0},
            {"symbol": "A", "date": dates[2], "entry": True, "exit": False, "score": 3.0},
        ],
    )
    strategy = StrategyEngine._load_file(STRATEGY_PATH)
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]

    def _load(symbols, start, end, feature_plan, asset_type="stock", **kwargs):
        del symbols, start, end, asset_type, kwargs
        field_columns = (
            set(feature_plan.base_columns)
            | set(feature_plan.instrument_columns)
            | set(feature_plan.matrix_columns)
        )
        return build_market_data_matrix(panel, field_columns=field_columns)

    engine.load_market_data_matrix_for_backtest = _load  # type: ignore[method-assign]
    service = StrategyBacktestService(
        engine=engine,
        strategy_engine=_StrategyEngineStub(strategy),  # type: ignore[arg-type]
    )

    result = service.run(StrategyBacktestConfig(
        strategy_id="research_signal_table",
        symbols=None,
        start=dates[0],
        end=dates[-1],
        params={"table_path": table_path},
        matching="open_t+1",
        mode="position",
        max_positions=10,
    ))

    assert result.error is None
    assert result.stats["n_trades"] == 1
    assert len(result.trades) == 1