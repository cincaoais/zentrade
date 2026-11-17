# ZenTrade strategy + backtest interface

All paths relative to `D:/Desktop/GitHub/zentrade`. Read-only recon; no code was run.

## Strategy contract
- Modules are Python files in `backend/app/strategy/builtin/*.py` or custom/AI dirs; dirs are registered in `backend/app/main.py:255-262` (builtin + `data/strategies/{custom,ai,composite}`).
- Loader reads module-level names: `META`, `EXECUTION_BACKEND` (`backend/app/strategy/engine.py:503-508`), `MATRIX_STRATEGY` (`:512,517-523`), `ENTRY_SIGNALS`/`EXIT_SIGNALS`, `STOP_LOSS`, `TRAILING_STOP`, `TRAILING_TAKE_PROFIT_ACTIVATE`, `TRAILING_TAKE_PROFIT_DRAWDOWN`, `MAX_HOLD_DAYS`, `TAKE_PROFIT`, `REQUIRED_FEATURES`, `LOOKBACK_DAYS` (`:587-593`). Builtin `META` must declare `asset_types` (`:469-471`).
- `EXECUTION_BACKEND = "matrix_native"` requires `MATRIX_STRATEGY` to implement the `MatrixStrategy` protocol and forbids `filter`/`filter_history` (`engine.py:517-523`).
- Protocol (`backend/app/backtest/matrix.py:3269-3278`): `required_fields() -> frozenset[str]`, `required_warmup_bars(params) -> int`, `compute_signals(market: MarketDataMatrix, params: dict) -> SignalMatrix`.
- It receives `MarketDataMatrix` (`matrix.py:416-446`): time x asset arrays `open/high/low/close/volume/tradable`, plus `fields: Mapping[str, np.ndarray]` (indicator/feature columns, e.g. `ma20`, `vol_ratio_5d`), `symbols`, `timestamps`, `timestamp_labels`, `session_ids`. Sizing is `market.shape == (time, asset)` (`:444-446`). Prices are forward-adjusted enriched parquet (`backend/app/indicators/pipeline.py:6-12,90-115`); unadjusted variants are separate fields `raw_close/raw_high/raw_low` (`pipeline.py:92,113-115`).
- Must return `SignalMatrix` (`matrix.py:489-499`) with arrays of shape `market.shape`, canonical dtypes enforced read-only: `entry/exit uint8`, `score float32` (finite), `entry_signal_code/exit_signal_code int16` (`matrix.py:2057-2093, 2098-2119`). Use `make_signal_matrix(...)` (`matrix.py:2057`) or you will fail validation.
- Example: `backend/app/strategy/builtin/macd_golden.py:18-48` (META/ENTRY_SIGNALS/STOP_LOSS), `:51-95` (required_fields = close+volume, warmup 60, compute_signals -> make_signal_matrix, signal ids `signal_macd_golden`/`signal_macd_dead`).
- Framework post-processes strategy output: basic-filter mask AND entry, and framework scoring recomputes `score` from `META["scoring"]` (`matrix.py:3292-3362`). For matrix_native, entry/exit signal *column* overrides are rejected (`backend/app/backtest/strategy.py:1442-1443`) — the only signal source is `compute_signals`.

## Exits
- Matcher config fields (`backend/app/backtest/engine.py:57-75`): `stop_loss_pct`, `take_profit_pct`, `trailing_stop_pct`, `trailing_take_profit_activate_pct`, `trailing_take_profit_drawdown_pct`, `max_hold_days`.
- Priority: pending exit > risk (stop/trailing SL/TP) > signal exit > `max_hold` > end (`engine.py:162-163`; code `engine.py:816-831, 1568-1577`).
- Strategies declare exits as module constants (`STOP_LOSS=-0.07`, `MAX_HOLD_DAYS=20` in `macd_golden.py:47-48`); loader maps them into `StrategyDef` (`engine.py:588-593`). Per-run overrides take precedence (`strategy.py:1136-1160`).
- Signal exit via `exit` bits in the returned `SignalMatrix`; risk checks use intraday high/low and open gaps (`engine.py:1012-1033, 1416-1447`).
- `exit_fill="signal_next_minute"` is a special minute-level exit mode requiring declared exit signals (`strategy.py:1113-1122`).

## Sizing, costs, fills
- Per-run `StrategyBacktestConfig` (`strategy.py:553-588`): `max_positions=10`, `max_exposure_pct=1.0`, `initial_capital=1_000_000`, `position_sizing="equal"|"score_weight"`, `matching`, `entry_fill`, `exit_fill`, `commission_per_share`, `commission_min`, `slippage_bps`, `cost_model`, `mode` ("position"|"full"), `holding_days`, `minute_fill`, `regime_filter`.
- Defaults: `open_t+1` matching for strategy runs (`strategy.py:561`), `commission_per_share=0.005`, `commission_min=1.0`, `slippage_bps=5.0`, `cost_model="us_per_share_v1"` (`engine.py:61-69,79-90`); legacy `fees_pct/commission_pct/stamp_tax_pct` auto-switch to `legacy_pct` (`engine.py:85-89`).
- Sizing: equal = `initial_capital / max_positions` (legacy simulator `engine.py:1080,1465`); portfolio path uses `equity_before * max_exposure_pct / max_positions` and whole-share `affordable_shares` including costs (`engine.py:132-144, 2137-2165, 2684-2713`); `score_weight` splits by score (`engine.py:2145, 2695`).
- Fill timing: `entry_fill`/`exit_fill` in {close_t, open_t+1} map to `entry_delay_bars`/`exit_delay_bars` 0/1 when building `MarketMatrix` from signals (`strategy.py:1547-1555`; `matrix.py:2133-2149`); exit price = open for `open_t+1` else close (`engine.py:1789`).
- Config is copied into `MatcherConfig` per run (`strategy.py:1316-1341, 1908-1934`), then `BacktestEngine.simulate_market_matrix` matches (`engine.py:1770-1793`).

## External data / sandbox
- `compute_signals` is ordinary in-process Python with full filesystem access *in principle* (no data-only argument sandbox at call time, `matrix.py:3274-3278`).
- But every strategy file — including builtin — is AST-validated before `exec_module` (`engine.py:278-288, 391-406, 424-446`): import whitelist is `polars, numpy, app.backtest.matrix, app.backtest.tv_kernels, app.strategy.builtin.factor_rank_research, app.strategy.market_data, datetime, __future__` (`backend/app/strategy/ai_generator.py:449-456`), and bare calls `open/exec/eval/compile/__import__/globals/locals/vars/dir/getattr/setattr/delattr/type/input/breakpoint` are banned (`:459-462`), plus dunder-attribute/subscript escapes (`:496-523`).
- Consequence: `open()`/pandas/pyarrow are unavailable, but `pl.read_parquet(path)` is an attribute call and is NOT blocked by these rules — so a precomputed parquet keyed by symbol/date is technically readable inside `compute_signals` if the path arrives via `params` (META params are provided per run). NOT VERIFIED end-to-end; no builtin strategy does this. Official read-only helpers exist for index/ETF daily bars via `app.strategy.market_data` (`get_index_daily`, `get_etf_daily`, `get_daily`: `backend/app/strategy/market_data.py:72-120`).
- The AST check self-describes as "not a real sandbox"; true isolation is future work (`ai_generator.py:471-474`). Loading is in-process, not a subprocess (`engine.py:424-446`).
- Practical alternative for an external signal table: write a builtin-style `matrix_native` strategy whose `compute_signals` loads the parquet with polars and emits `entry/exit` arrays aligned to `market.symbols`/`market.timestamp_labels`.

## Running a backtest from Python + result fields
- Wiring (mirrors `backend/app/backtest/worker.py:196-219`):
  `store = DataStore(data_dir); repo = KlineRepository(store)`
  `engine = StrategyEngine(strategy_dirs=[builtin_dir, ...])`
  `svc = StrategyBacktestService(BacktestEngine(repo), engine)`
  `res = svc.run(StrategyBacktestConfig(strategy_id=..., symbols=None, start=..., end=..., params=..., overrides=...))`
- `StrategyBacktestService.__init__(engine, strategy_engine)` (`strategy.py:736-740`); `run(config, progress_cb=None, cancel_event=None, prepared=None, result_policy=None)` (`strategy.py:1063-1069`).
- Returns `StrategyBacktestResult` (`strategy.py:591-604`): `run_id, config, stats, equity_curve, drawdown_curve, benchmark_curve, trades, per_symbol_stats, strategy_info, factor_attribution, elapsed_ms, error`.
- `trades` items are dicts from `_trade_to_dict` (`strategy.py:2322-2352`): `symbol, name, entry_date, exit_date, entry_price, exit_price, pnl_pct, duration, exit_reason, shares, lots, entry/exit_commission, entry/exit_slippage, entry_adv20_usd, selection_as_of, position_pct, entry_value, exit_value, pnl_amount, entry_score, entry_signal_date, exit_signal_date, blocked_exit_days, entry_signal_id, exit_signal_id`.
- `exit_reason` ∈ {signal, stop_loss, take_profit, trailing_stop, trailing_take_profit, max_hold, end} (`engine.py:162`).
- Data source: enriched parquet under `data/kline_daily_enriched` etc., loaded as matrices (`engine.py:563-600`; `matrix.py:647-662`).

## Walk-forward / OOS
- Strategy walk-forward: calendar-day rolling folds `generate_folds(start, end, train_days, test_days, step_days)` where `test_start = train_end + 1 day` and the window stops when `test_end > end` (`backend/app/backtest/walkforward.py:29-63`); `WalkForwardService.run(cfg)` optimizes params on train and backtests OOS (`walkforward.py:169-172, 218-270`).
- Mining/OOS: `NestedValidationConfig` defaults 504/126/63 outer (train/test/step), 252/63/63 inner, `purge_bars=30`, `embargo_bars=5` (`backend/app/backtest/mining.py:91-122`); profiles exploratory/balanced/strict override these (`:123-152`), `required_outer_folds` = 1 exploratory else 3 (`:161-163`).
- `generate_nested_folds(trading_labels, config)` rolls outer folds by `outer_step_bars` over ordered trading bars, inner folds strictly inside outer-train, with purge+embargo (`mining.py:752-825`).
- Per-fold OOS metrics recorded by `_fold_row`: `total_return, sharpe, max_drawdown, n_trades, skipped, reason`, plus train/test boundaries (`backend/app/backtest/mining_runtime.py:1309-1344`).
- Candidate-level `oos_*` aggregates = mean over successful folds of return/sharpe, worst max_drawdown, ratio of positive folds, summed trades (`mining_runtime.py:1174-1219`).

## Open questions
- Whether `pl.read_parquet` of an arbitrary path is acceptable operationally (cache invalidation, asset_type routing, generation checks) — no builtin precedent. NOT VERIFIED.
- How strategy `score` should be supplied for an external signal table: framework recomputes from `META["scoring"]` (`matrix.py:3346-3353`), so a custom `score` in `SignalMatrix` is only a fallback.
- Line-precise stats dict keys of `SimResult.stats` were not enumerated here; only its container type is confirmed (`engine.py:185-193`). NOT VERIFIED.