# 缠论 + Wyckoff Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> In this repo, execution runs through **Orca with pi workers**: one pi worker per task, and Claude Code coordinates and reviews each task before the next.

**Goal:** A point-in-time research harness that replays the user's 缠论 engine and a ported `wyk` Wyckoff detector set day by day over moomoo-backed US daily bars. It composes 18 hypothesis variants into dated entry/exit tables and trades them through ZenTrade's own backtester. It then judges them against baselines with a deflated Sharpe and a locked holdout.

**Architecture:** A new `backend/app/research/` package (not imported by the live app) produces parquet tables under `<data_dir>/research/`:
- replay (Chan, Wyckoff) → per-symbol dated events;
- composer → one table per variant;
- a `matrix_native` bridge strategy → ZenTrade fills, costs and exits;
- evaluation → metrics, gate and report.

Integrity checks and a registry/freeze lock guard against hindsight and against peeking at the holdout.

**Tech Stack:**
- Python 3 (backend `uv` env), polars, numpy, pandas (inside `research/` only);
- `czsc==1.0.2`;
- the vendored DSA `engine.py`;
- the ported `wyk` detectors;
- ZenTrade `StrategyBacktestService`;
- stdlib `multiprocessing` and `statistics.NormalDist`.

**Spec:** `docs/superpowers/specs/2026-11-17-chan-wyckoff-harness-design.md` (approved by the user 2026-11-17). Where the spec's "Builder-check resolutions" section and the body disagree, the resolutions win.

## Global Constraints

- Branch: `feat/research-harness`, created from `feat/moomoo-data`. Commit per task. Never push.
- New dependency: exactly `czsc==1.0.2` (Apache-2.0). No other new dependency. Use `statistics.NormalDist` for the normal CDF and inverse, not scipy.
- `app.research` must never be imported by any module under `backend/app/` outside `backend/app/research/` (Task 1 test).
- **No data after the decision date.** Never use `still_valid`, later `state`, `gone_date`, `end_idx`/`end_reason`, today's liquidity, or split-adjusted `volume` for liquidity in any entry, filter or universe rule.
- Universe: ≥ 250 stored bars up to t; raw close at t ≥ $5; 20-day median of the unadjusted `amount` (days ≤ t) ≥ $10,000,000; top 1,500 by that median. SPY is reference only.
- Replay starts at each symbol's 120th bar (index 119). Chan point identity = `(type, point_dt)`.
- Research data window: first stored date → **2026-11-13** (`RESEARCH_LAST_DATE`). The stored 2026-11-16 bar was written intraday and is partial; it must never be read.
- Development ends **2025-11-13**; holdout = **2025-11-14 → 2026-11-13**.
- Run config:
  - `max_positions=10`, equal sizing;
  - `entry_fill = exit_fill = "open_t+1"`;
  - default costs (0.005 $/share, $1 min, 5 bps);
  - a stress run at `slippage_bps=15`.
- Exits:
  - close < active invalidation → next open;
  - sell event while holding;
  - `MAX_HOLD_DAYS = 20`;
  - `STOP_LOSS = -0.15`.
- Variant budget 24. v1 registers exactly the 18 variant ids defined in Task 8. Baselines and random seeds are not variants.
- `wyk` thresholds unchanged. Any deviation from either engine is listed in `app/research/DEVIATIONS.md` (Task 5 creates it) and copied into every report.
- Worker pool default `min(12, os.cpu_count() - 2)`, overridable by `--workers`.
- Never call a moomoo trade API. The harness reads only ZenTrade's local parquet store.
- Tests live in `backend/tests/research/` and run with `uv run pytest tests/research -q` from `backend/`. They need no OpenD and no network.

## Review Focus

1. **Zero-range bars and halts.** `wyk` drops bars with `high == low` and treats data gaps as `post_gap`. Every event's `known_date` must still map to the right calendar date (Task 5 test `test_zero_range_bar_keeps_dates`).
2. **Symbols the engine can't analyse** (< 120 bars, `status != "available"`, engine error) must give zero rows and a logged count, never a crash or a half-written part (Task 4 test `test_engine_error_symbol_skipped_and_counted`).
3. **A variant with zero entries or zero trades in a period** gives 0 trades and gate FAIL with a reason. There is no division by zero, and the random baseline is skipped with the note "no entries" (Task 11 test `test_metrics_zero_trades`, Task 10 test `test_random_baseline_no_entries`).
4. **The partial 2026-11-16 bar** is excluded everywhere because every loader clips at `RESEARCH_LAST_DATE` (Task 2 test `test_load_bars_clips_partial_day`).
5. **Table rows the backtest panel doesn't contain**: a symbol outside `market.symbols` or a date outside the labels is ignored and counted in the strategy's log, never an IndexError. Labels can be `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS` (Task 9 tests).

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/research/__init__.py` | package marker, `RESEARCH_LAST_DATE`, `DEV_END`, `HOLDOUT_START`, `HOLDOUT_END` |
| `backend/app/research/vendor/dsa_chan/engine.py` | verbatim DSA engine plus provenance header |
| `backend/app/research/versions.py` | `chan_engine_version()`, `wyckoff_port_version()`, `file_hash()` |
| `backend/app/research/bars.py` | read enriched bars and the instrument filter from the store |
| `backend/app/research/features.py` | `atr14`, `rel_volume` (causal) |
| `backend/app/research/universe.py` | point-in-time eligibility |
| `backend/app/research/chan_replay.py` | per-symbol replay core, pool runner, parts and resume |
| `backend/app/research/wyckoff/` | ported `wyk` code (`base.py`, `pivots.py`, `ranges.py`, `accumulation.py`, `distribution.py`, `effort.py`, `quality.py`, `volume.py`), `us_bars.py`, `replay.py` |
| `backend/app/research/compose.py` | common entry/exit mechanics |
| `backend/app/research/hypotheses.py` | the 18 variant definitions |
| `backend/app/research/baselines.py` | B0-rand tables, B0-bh and B0-spy curves |
| `backend/app/research/metrics.py` | trade R, CAGR, Sharpe, MDD, exposure, PSR/DSR |
| `backend/app/research/registry.py` | run registry, freeze, holdout lock |
| `backend/app/research/integrity.py` | truncation, phantom, no-future-columns, determinism |
| `backend/app/research/evaluate.py` | ZenTrade runs, folds, stress, gate |
| `backend/app/research/report.py` | markdown report |
| `backend/app/research/DEVIATIONS.md` | engine deviations list |
| `backend/app/strategy/builtin/research_signal_table.py` | the bridge strategy |
| `backend/scripts/research/{replay_chan,replay_wyckoff,compose,evaluate,freeze}.py` | CLIs |
| `backend/tests/research/…` | tests per task |

Output layout under `<data_dir>/research/`:
- `chan/<engine_version>/points/<SYMBOL>.parquet` and `chan/<engine_version>/gone/<SYMBOL>.parquet`;
- `wyckoff/<port_version>/events/<SYMBOL>.parquet` and `…/ranges/<SYMBOL>.parquet`;
- `universe/<params_hash>.parquet`;
- `variants/<variant_id>/<table_hash>.parquet`;
- `registry.json`, `registry.frozen.json`.

Reports go to `docs/research/YYYY-MM-DD-<run>.md`.

---

### Task 1: Package scaffold, czsc dependency, vendored Chan engine

**Files:**
- Create: `backend/app/research/__init__.py`, `backend/app/research/vendor/__init__.py`, `backend/app/research/vendor/dsa_chan/__init__.py`, `backend/app/research/vendor/dsa_chan/engine.py`, `backend/app/research/versions.py`
- Modify: `backend/pyproject.toml` (add `"czsc==1.0.2"` to main dependencies), `backend/uv.lock`
- Test: `backend/tests/research/__init__.py`, `backend/tests/research/test_scaffold.py`

**Interfaces:**
- Produces:
  - `app.research.RESEARCH_LAST_DATE = date(2026, 11, 13)`, `DEV_END = date(2025, 11, 13)`, `HOLDOUT_START = date(2025, 11, 14)`, `HOLDOUT_END = date(2026, 11, 13)`.
  - `app.research.vendor.dsa_chan.engine.build_level_snapshot(df: pd.DataFrame, timeframe: str, symbol: str = "") -> dict` (unchanged DSA API).
  - `app.research.versions.file_hash(path: Path) -> str`: first 16 hex characters of the sha256 of the file bytes.
  - `chan_engine_version() -> str` = `f"{file_hash(engine.py)}-czsc{czsc.__version__}"`.
  - `wyckoff_port_version() -> str`: a hash over the sorted `app/research/wyckoff/*.py` bytes. It returns `"none"` until Task 5 adds files.

- [ ] **Step 1: Copy the engine.** Copy `D:/Desktop/GitHub/daily_stock_analysis/src/chan/engine.py` byte for byte. Prepend this header only:
  ```python
  # Vendored verbatim from daily_stock_analysis/src/chan/engine.py at commit e9b0d57127ed107df6730cc48ce2338e5ca1546e (MIT).
  # Do not edit below this header; deviations go in app/research/DEVIATIONS.md.
  ```
  If the source has relative imports, report it to the coordinator before changing anything; engine.py imports only pandas and stdlib per the recon notes.
- [ ] **Step 2: Add the dependency.** Add `czsc==1.0.2` and run `uv lock` then `uv sync --extra dev`. Verify with `uv run python -c "import czsc; print(czsc.__version__)"`, which must print `1.0.2`. If no wheel exists for this Python on win_amd64, stop and report.
- [ ] **Step 3: Write the failing tests** in `test_scaffold.py`:
  - `test_engine_snapshot_available_on_synthetic_bars`: 300 bars of a deterministic zig-zag (`close = 100 + 10*sin(i/7) + i*0.05`, high = close+1, low = close−1, open = previous close, volume = 1e6). Assert `snap["status"] == "available"` and that `snap` has `"buy_sell_points"` (reach into the level dict the way engine.py returns it).
  - `test_chan_engine_version_stable`: two calls are equal; the value matches `r"^[0-9a-f]{16}-czsc1\.0\.2$"`.
  - `test_live_app_never_imports_research`: walk `backend/app/**/*.py` excluding `app/research/**`. No file contains `app.research` or `from app import research` (text search).
- [ ] **Step 4: Run** `uv run pytest tests/research/test_scaffold.py -q`. Expect a FAIL before the files exist, then a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): scaffold, czsc 1.0.2, vendored DSA chan engine`.

---

### Task 2: Bars loader, instrument filter, universe

**Files:**
- Create: `backend/app/research/bars.py`, `backend/app/research/features.py`, `backend/app/research/universe.py`
- Test: `backend/tests/research/test_bars_universe.py`

**Interfaces:**
- Produces:
  - `load_bars(data_dir: Path, symbols: list[str] | None = None, start: date | None = None, end: date = RESEARCH_LAST_DATE) -> pl.DataFrame`.
    - Columns: `symbol, date, open, high, low, close, volume, amount, raw_close`, sorted by `(symbol, date)`.
    - Reads `<data_dir>/kline_daily_enriched/date=*/part.parquet` with `pl.scan_parquet(glob, hive_partitioning=False)`; the files carry their own `date` column, the same layout as `kline_daily`.
    - `end` is clipped to `min(end, RESEARCH_LAST_DATE)`.
  - `tradable_symbols(data_dir: Path) -> set[str]`: rows of `instruments/instruments.parquet` with `asset_type == "stock"` and not `is_etf`/`is_preferred`/`is_warrant`/`is_unit`, and `instrument_type != "right"`. Nulls count as False. SPY/QQQ/IWM/DIA are not in instruments, so they are excluded automatically. Symbols in `EXCLUDED_SYMBOLS = frozenset({"DPM","GLDE","SMX","STAFQ","SVA","VLPIX"})` are also excluded. moomoo reported a non-zero `forward_adj_factorB` for these, so their adjustment events were skipped and their prices are unadjusted (backfill resume log, 2026-11-17). Test: `test_excluded_symbols_never_tradable`.
  - `features.atr14(high, low, close) -> np.ndarray`: Wilder ATR(14) including bar t. NaN for the first 13 values; the first value is the mean of 14 TRs.
  - `features.rel_volume(volume) -> np.ndarray`: `volume[t] / median(volume[t-20:t])`, previous 20 bars only. NaN when fewer than 20 exist or the median is 0.
  - `UniverseParams` (frozen dataclass): `min_bars=250, min_price=5.0, min_dollar_vol=10_000_000.0, top_n=1500`; `.hash() -> str` (16 hex characters of the sha256 of sorted-key JSON).
  - `eligibility(bars: pl.DataFrame, tradable: set[str], params: UniverseParams) -> pl.DataFrame`, with columns `symbol, date, n_bars, dv_med20, eligible: bool, rank: int|null`. `rank` is the 1-based rank of `dv_med20` among the symbols that pass the first three rules that day, ties broken by symbol ascending. `eligible` = passes all three and `rank <= top_n` and symbol in tradable.
  - `universe_path(data_dir, params) -> Path` = `<data_dir>/research/universe/<params.hash()>.parquet`.

- [ ] **Step 1: Write the failing tests.**
  - `test_load_bars_clips_partial_day`: in a temporary data dir, write partitions 2026-11-12, 11-13 and 11-16. `load_bars(tmp, end=date(2026,11,20))["date"].max() == date(2026,11,13)`.
  - `test_universe_uses_unadjusted_amount_not_volume`: symbol X has `volume` ×10 after a fake split (adjusted history) but an unchanged `amount`. Its `dv_med20` equals the median of `amount`, and its eligibility before the split date is unchanged when the split rows are removed from the frame (no future dependence).
  - `test_universe_rules`: on synthetic bars:
    - 249 bars gives ineligible on the 249th day and eligible on the 250th, given price and dollar volume;
    - raw_close 4.99 gives ineligible;
    - a dv_med20 of 9.9 M gives ineligible;
    - with `top_n=2` and 3 qualifying names, the lowest dv is excluded;
    - a non-tradable symbol is never eligible.
  - `test_atr14_and_rel_volume_causal`: changing bar t+1 never changes `atr14[: t+1]` or `rel_volume[: t+1]`; `rel_volume` at t ignores `volume[t]` in its median.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** the three modules, polars-first. `n_bars` = cumulative count of stored bars per symbol. `dv_med20` = rolling median of `amount` over the last 20 stored bars including t, null when fewer than 20.
- [ ] **Step 4: Run** `uv run pytest tests/research/test_bars_universe.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): bars loader, causal features, point-in-time universe`.

---

### Task 3: Chan replay core (pure, injectable engine)

**Files:**
- Create: `backend/app/research/chan_replay.py`
- Test: `backend/tests/research/test_chan_replay_core.py`

**Interfaces:**
- Consumes: `build_level_snapshot` (Task 1).
- Produces:
  - `POINT_COLUMNS = ("symbol","known_date","type","side","point_dt","price","invalidation_price","basis","state_at_known","confirmed_date","leg_prev_start","leg_prev_end","leg_last_start","leg_last_end","engine_version")`.
  - `GONE_COLUMNS = ("symbol","type","point_dt","first_known_date","gone_date","window_start")`.
  - `replay_symbol(symbol: str, bars: pd.DataFrame, snapshot_fn: Callable[[pd.DataFrame], dict], engine_version: str, start_index: int = 119) -> tuple[pl.DataFrame, pl.DataFrame, dict]`, which returns `(points, gone, stats)`. `stats` has the keys `days, available_days, error_days`.

Replay rules:
- For t in `range(start_index, len(bars))`, call `snapshot_fn(bars.iloc[: t + 1])`. Read the daily level's `buy_sell_points` and `recent_bis`. Skip a day whose status is not `"available"`; it counts in `error_days` for status `"error"`.
- **First-seen:** an identity `(type, dt)` not seen before gets a row with:
  - `known_date = date[t]`;
  - `state_at_known = point["state"]`;
  - `confirmed_date = null`.
- **Confirmed:** the first later day where the identity is present with `state == "confirmed"` sets `confirmed_date`. If it is confirmed on the first-seen day, then `confirmed_date = known_date`.
- **Legs:** from that day's `recent_bis`, the stroke whose end equals `point_dt` gives `leg_last_*`, and the stroke before it gives `leg_prev_*`. These are null when not found. Read the stroke start/end keys exactly as engine.py names them.
- **Gone:** an identity seen on day t−1's snapshot and absent on day t's gets a gone row with `gone_date = date[t]` and `window_start` = the earliest stroke start in day t's `recent_bis`. If it reappears later, it keeps its first-seen row; a later disappearance gets a new gone row.
- **Never stored:** `still_valid`, the later `state`, `bars_since`.

- [ ] **Step 1: Write the failing tests** with a scripted fake `snapshot_fn` (a dict from `len(frame)` to a canned snapshot):
  - `test_first_seen_row_dated_at_first_appearance`: a point appears at length 130 → `known_date == date[129]`, one row only, even though it is present on later days.
  - `test_confirmed_date_recorded_later`: candidate at 130 and confirmed at 133 → `confirmed_date == date[132]`, `state_at_known == "candidate"`.
  - `test_gone_row_with_window_start`: present on 130–131 and absent on 132 → one gone row, `gone_date == date[131]`, `window_start` = the earliest `recent_bis` start in the length-132 snapshot.
  - `test_reappear_keeps_first_seen`: present, gone, present → 1 points row, 1 gone row.
  - `test_no_future_fields_stored`: the column set equals `POINT_COLUMNS`, and no column is named `still_valid`/`bars_since`.
  - `test_unavailable_days_skipped`: status `"partial"` on some days → no rows from those days; the `stats` counts match.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement `replay_symbol`.** Keep a `dict[(type, dt)] -> row` for seen identities and a `set` for those present on the previous day.
- [ ] **Step 4: Run** `uv run pytest tests/research/test_chan_replay_core.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): chan point-in-time replay core`.

---

### Task 4: Chan replay runner and CLI (pool, parts, resume)

**Files:**
- Modify: `backend/app/research/chan_replay.py`
- Create: `backend/scripts/research/__init__.py` (empty), `backend/scripts/research/replay_chan.py`
- Test: `backend/tests/research/test_chan_replay_runner.py`

**Interfaces:**
- Consumes: `load_bars`, `eligibility`, `universe_path` (Task 2), `replay_symbol` (Task 3), `chan_engine_version` (Task 1).
- Produces:
  - `chan_dir(data_dir) -> Path` = `<data_dir>/research/chan/<chan_engine_version()>`.
  - `run_chan_replay(data_dir: Path, symbols: list[str], workers: int, snapshot_fn=None) -> dict` returns `{"done": int, "skipped_existing": int, "failed": list[str], "error_days": int}`. `snapshot_fn=None` means the real engine with timeframe `"1d"`.
  - `load_chan_points(data_dir) -> pl.DataFrame` and `load_chan_gone(data_dir) -> pl.DataFrame` concatenate the parts.
  - CLI: `uv run python scripts/research/replay_chan.py [--workers N] [--symbols A,B] [--limit N]`. Without `--symbols` it builds or loads the universe parquet and replays the union of symbols eligible on any date.

Runner rules:
- One task per symbol. The child process loads only its own symbol's bars, converted to pandas with columns `date, open, high, low, close, volume, amount`.
- Each part is written to `<name>.parquet.tmp`, then `os.replace`d to its final name, so a crash never leaves a half part.
- A symbol whose points part exists is skipped (resume).
- A child exception is caught per symbol, logged, and added to `failed`; no part is written for it.
- `snapshot_fn` must be picklable (a module-level function).

- [ ] **Step 1: Write the failing tests** on a temporary data dir with 3 synthetic symbols × 200 bars, `workers=2`, and a module-level fake `snapshot_fn`:
  - `test_runner_writes_parts_and_resumes`: the first run gives `done=3`; the second gives `skipped_existing=3, done=0`.
  - `test_engine_error_symbol_skipped_and_counted`: a fake that raises for symbol `"BAD"` gives `failed == ["BAD"]`, no `BAD.parquet`, and no `.tmp` left.
  - `test_replay_deterministic_bytes`: run twice into two directories; the parts are byte-identical. Write with fixed settings: `compression="zstd"`, `statistics=False`.
  - `test_real_engine_smoke`: the real engine on 1 synthetic symbol × 160 bars finishes and returns `done=1`. Mark it `@pytest.mark.slow` only if it takes more than 20 s.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** the runner (`multiprocessing.get_context("spawn").Pool`, `imap_unordered`, chunksize 1) and the CLI. The CLI logs progress every 50 symbols with an ETA.
- [ ] **Step 4: Run** `uv run pytest tests/research/test_chan_replay_runner.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): parallel resumable chan replay runner + CLI`.

---

### Task 5: Port `wyk` detectors to US daily bars

**Files:**
- Create in `backend/app/research/wyckoff/`: `__init__.py`, `base.py`, `volume.py`, `pivots.py`, `ranges.py`, `accumulation.py`, `distribution.py`, `effort.py`, `quality.py`, `us_bars.py`
- Create: `backend/app/research/DEVIATIONS.md`
- Test: `backend/tests/research/wyckoff/__init__.py`, the ported tests listed below, and `backend/tests/research/wyckoff/test_us_bars.py`

**Interfaces:**
- Produces:
  - Every ported module keeps its `wyk` public names and signatures (`Candles`, `Event`, `wilder_atr`, `candles_from_arrays`, `pivots`, `trend_context`, `TradingRange`, `generic_ranges`, `event_ranges`, `springs`, `spring_tests`, `sos_events`, `lps_events`, `accumulation_events`, `upthrusts`, `utad_confirmations`, `sow_events`, `lpsy_events`, `distribution_events`, `effort_events`, `choch_events`, `qualification_map`, `apply_s0`, `ACC_KINDS`, `DIST_KINDS`).
  - `us_bars.us_candles(df: pd.DataFrame) -> tuple[Candles, np.ndarray]`.
    - Input columns: `date, open, high, low, close, volume`.
    - Returns `(candles, kept_dates)`, where `kept_dates[i]` is the `datetime.date` of candle i.
    - Bars with `high <= low` are dropped (as `wyk` does).
    - `holiday`, `holiday_lowvol` and `news` are all-False arrays.
    - `post_gap[i]` is True when bar i follows a stored-bar gap of more than 5 calendar days (a trading halt). A normal weekend or a single holiday gives False.
    - Volume features take the D1 path: `rv` = V / trailing 20-bar median, and `ae` as in `wyk`. Use `v` = real share volume and `v2` = NaN.
    - No TICK rounding.

Port rules:
- Copy from `D:/Desktop/GitHub/wyckoff/src/wyk` at commit `fc142999e4fbfb617333878c72c5b3d3a03ea255`. Add this header to each file: `# Ported from wyk/<path> @ fc142999 (user's own code).`
- Rewrite only:
  - the imports (`wyk.` → `app.research.wyckoff.`);
  - the XAUUSD-only parts: `prepare()` and `slot_of`/session paths may be deleted; `TICK` rounding is not applied in `us_candles`; there is no `wyk.data.*` import.
- Thresholds and logic are untouched.
- `wyk.timeutil.to_ms` is inlined if needed.
- Each change goes in `DEVIATIONS.md` as one line: file, what changed, why.

- [ ] **Step 1: Port the tests first.** Copy `wyk` `tests/test_foundation.py` (causality parts only), `test_ranges.py`, `test_accumulation.py`, `test_distribution.py`, `test_effort.py` and `test_quality.py` into `backend/tests/research/wyckoff/`, with imports rewritten. Drop only the tests of deleted XAUUSD code (calendar, `prepare`, bars, holidays, news, stage0/1) and list each dropped test name in `DEVIATIONS.md`. Run them: FAIL (module missing).
- [ ] **Step 2: Copy and adapt the detector modules.** Run the ported tests: PASS. Do not touch a threshold to make a test pass. Report any failure to the coordinator.
- [ ] **Step 3: Write the failing `test_us_bars.py` tests:**
  - `test_zero_range_bar_keeps_dates`: 30 bars with bar 10 flat (`high == low`). `len(candles) == 29`, `kept_dates` skips date[10], and `kept_dates[10] == date[11]`.
  - `test_post_gap_only_for_halts`: a Friday→Monday gap gives False, a 3-day weekend gives False, and a 9-day gap gives True.
  - `test_rv_uses_real_volume`: `candles.rv[25] == vol[25] / median(vol[5:25])` (prior 20 bars, `wyk` definition).
- [ ] **Step 4: Implement `us_candles`** by building the arrays and calling the same feature helpers `candles_from_arrays` uses, with the real `t`/`tc` from the dates (UTC midnight ms, `tc = t + 1 day`). Run `uv run pytest tests/research/wyckoff -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): port wyk detectors to US daily bars`.

---

### Task 6: Wyckoff replay (events and ranges tables, CLI)

**Files:**
- Create: `backend/app/research/wyckoff/replay.py`, `backend/scripts/research/replay_wyckoff.py`
- Test: `backend/tests/research/wyckoff/test_replay.py`

**Interfaces:**
- Consumes: `us_candles`, the detectors (Task 5), `load_bars` (Task 2), `wyckoff_port_version` (Task 1).
- Produces:
  - `EVENT_KINDS = ("entry_steady", "sos", "sos_failed", "lps", "sow", "utad_confirmed")`.
  - `EVENT_COLUMNS = ("symbol","known_date","kind","idx_date","price","stop_ref","range_kind","range_lo","range_hi","atr_known","port_version")`.
  - `RANGE_COLUMNS = ("symbol","range_kind","known_date","end_date","lo","hi")`.
  - `wyckoff_symbol(symbol: str, bars: pd.DataFrame, port_version: str) -> tuple[pl.DataFrame, pl.DataFrame]` returns `(events, ranges)`.
  - `run_wyckoff_replay(data_dir, symbols, workers) -> dict`, with the same part, resume and failure rules as Task 4, under `<data_dir>/research/wyckoff/<port_version>/{events,ranges}/`.
  - `load_wyckoff_events(data_dir)` and `load_wyckoff_ranges(data_dir)`.
  - CLI: `scripts/research/replay_wyckoff.py [--workers N] [--symbols ...]`, which defaults to the same union universe as the Chan CLI.

Replay rules:
- Run the detectors once on the full candle series; they are causal by `known_at`, which Step 1's truncation test proves.
- Use the `wyk` default pipeline:
  - ranges = `event_ranges(..., "accumulation")` + `event_ranges(..., "distribution")` + `generic_ranges`;
  - `accumulation_events`, `distribution_events`;
  - `spring_tests` for `entry_steady`.
- Keep only `EVENT_KINDS`.
- Columns:
  - `known_date = kept_dates[e.known_at]`, `idx_date = kept_dates[e.idx]`;
  - `stop_ref = e.data.get("stop_ref")`;
  - range fields from `e.data` (`range_kind`, `range_lo`, `range_hi`);
  - `atr_known = candles.atrp[e.known_at]`.
- Ranges: `known_date = kept_dates[r.known_at]`. `end_date = kept_dates[r.end_idx]` if the range closed, else null. It is stored only for the "active at t" test: active ⇔ `known_date <= t < end_date` (or `end_date` null). This is causal because a range closes at the close of `end_idx`.

- [ ] **Step 1: Write the failing tests** on synthetic accumulation-shaped bars. Reuse a generator from the ported `wyk` tests, then convert it to a dated frame.
  - `test_truncation_events_equal`: for cuts at 60%, 75% and 90% of the length, events from `bars[:cut]` equal the full-run events with `known_date <= date[cut-1]`.
  - `test_truncation_ranges_active_equal`: for each cut and each day t ≤ cut−1, the set of active ranges at t is the same in the truncated and the full run.
  - `test_columns_and_kinds`: the columns equal `EVENT_COLUMNS`; every kind is in `EVENT_KINDS`; there is no `end_idx` column in events.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** the replay and the CLI.
- [ ] **Step 4: Run** `uv run pytest tests/research/wyckoff/test_replay.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): wyckoff event/range replay + CLI`.

---

### Task 7: Composer core (common entry/exit mechanics)

**Files:**
- Create: `backend/app/research/compose.py`
- Test: `backend/tests/research/test_compose_core.py`

**Interfaces:**
- Consumes: `atr14` (Task 2), the eligibility frame (Task 2).
- Produces:
  - `FORBIDDEN_INPUT_COLUMNS = frozenset({"still_valid", "state", "gone_date", "end_idx", "end_reason", "bars_since"})`.
  - `assert_no_future_columns(df: pl.DataFrame) -> None`: raises `ValueError` naming the column.
  - `Signals` (dataclass):
    - `entries: pl.DataFrame` with `symbol, date, invalidation, source`;
    - `sells: pl.DataFrame` with `symbol, date`.
    `date` is the decision date t.
  - `TABLE_COLUMNS = ("symbol","date","entry","exit","score","invalidation","atr14","close")`.
  - `compose_table(signals: Signals, bars: pl.DataFrame, elig: pl.DataFrame) -> tuple[pl.DataFrame, dict]` returns `(table, log)`. `log` has the counts `emitted, ineligible, gapped_through, kept, exits`.
  - `entry_score(symbol: str, d: date) -> float`: deterministic in (0, 1), the first 8 bytes of `sha256(f"{symbol}|{d}")` / 2^64.

Mechanics, applied in this order:
1. Call `assert_no_future_columns` on every input frame.
2. Several entry rows for the same `(symbol, date)` merge into one, using the **highest** invalidation (the tighter stop).
3. An entry is dropped (`ineligible`) unless `elig.eligible` is true at `(symbol, t)`.
4. An entry is dropped (`gapped_through`) when `open[t+1] <= invalidation`, or when t+1 doesn't exist. This is a fill-time check on the opening print, as in the spec.
5. **Active invalidation**, per symbol: the invalidation of the most recent kept entry with date ≤ t. `exit[t] = True` when `close[t] < active_invalidation`, or there is a sell row at `(symbol, t)`. Exit rows are kept regardless of eligibility.
6. `score = entry_score(symbol, t)` on entry rows and 0 otherwise; ties are neutral (my call, it replaces the spec's `score=1`, see below). `atr14` and `close` come from bars at t.
7. The table holds only rows where `entry` or `exit` is True. It is sorted by `(date, symbol)`.

The score deviates from the spec, which said `score=1`. With equal scores, ZenTrade fills the 10 slots in panel order, which is alphabetical: a selection bias. A neutral hash order removes it and is still deterministic. Record this in `DEVIATIONS.md`.

- [ ] **Step 1: Write the failing tests** on hand-made bars (2 symbols × 10 days):
  - `test_forbidden_column_rejected`: an input with `still_valid` raises `ValueError` naming it.
  - `test_ineligible_entry_dropped_exit_kept`: the counts in `log` match.
  - `test_gapped_through_skipped`: `open[t+1] = invalidation − 0.01` → dropped and counted.
  - `test_same_day_merge_uses_higher_invalidation`.
  - `test_newest_signal_invalidation_governs`: entry A on day 2 with inv 90 and entry B on day 5 with inv 95; a close of 93 on day 6 gives `exit[6] = True`, but the same close on day 4 gives False.
  - `test_sell_row_sets_exit`.
  - `test_score_deterministic_and_neutral`: same inputs give the same score; scores lie in (0, 1); the order is not alphabetical for `["AAA","BBB","CCC"]` on a fixed date (pin the computed order).
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement `compose_table`.** Use polars joins plus a per-symbol forward-fill of the invalidation (`forward_fill().over("symbol")`).
- [ ] **Step 4: Run** `uv run pytest tests/research/test_compose_core.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): composer core (eligibility, gap skip, active invalidation, neutral score)`.

---

### Task 8: Hypothesis definitions (18 variants) and the compose CLI

**Files:**
- Create: `backend/app/research/hypotheses.py`, `backend/scripts/research/compose.py`
- Test: `backend/tests/research/test_hypotheses.py`

**Interfaces:**
- Consumes: `Signals`, `compose_table` (Task 7); `load_chan_points` (Task 4); `load_wyckoff_events` and `load_wyckoff_ranges` (Task 6); `rel_volume` (Task 2); SPY bars via `load_bars(symbols=["SPY"])`.
- Produces:
  - `VARIANTS: dict[str, Callable[[Inputs], Signals]]` with exactly these 18 keys:
    `H1-2B, H1-3B, H1-1B, H2-spring, H2-sos, H2-lps, H3-2B, H3-3B, H4-3B, H5-w5, H5-w10, H6-H3-2B, H6-H3-3B, H6-H4-3B, H6-H5-w5, H6-H5-w10, H1c-2B, H1c-3B`.
  - `Inputs` (dataclass): `chan_points, wyk_events, wyk_ranges, bars, spy` (all `pl.DataFrame`).
  - `variant_path(data_dir, variant_id, table) -> Path` = `<data_dir>/research/variants/<id>/<sha256 of the table's parquet bytes, 16 hex>.parquet`.
  - CLI: `scripts/research/compose.py [--variants id,...]` writes each table and prints its path and log counts.

Rules (t = decision date; "known at t" = `known_date == t` unless a window is given):

| Id | Entries | Invalidation | Sells |
|---|---|---|---|
| H1-{2B,3B,1B} | Chan `type` == X, `known_date` == t | `invalidation_price` | Chan 1S/2S/3S `known_date` == t |
| H1c-{2B,3B} | Chan `type` == X, `confirmed_date` == t (non-null) | `invalidation_price` | as H1 |
| H2-spring | wyk `entry_steady` known at t | `stop_ref` | wyk `sow`, `utad_confirmed`, `sos_failed` |
| H2-sos | wyk `sos` known at t | `range_hi − 0.25·atr_known` | as H2 |
| H2-lps | wyk `lps` known at t | `stop_ref` | as H2 |
| H3-{2B,3B} | H1-X entry at t, and ≥ 1 range with `range_kind in ACC_KINDS` active at t, and a wyk `entry_steady`/`sos`/`lps` with `known_date` in the last 20 bars of the symbol up to and including t | max(Chan inv, that wyk event's inv; the most recent event if several) | Chan sells ∪ wyk sells |
| H4-3B | H1-3B entry at t, mean `rel_volume` over the bars of `[leg_prev_start, leg_prev_end]` ≥ 1.5 and over `(leg_last_start, leg_last_end]` ≤ 0.8; skipped if the legs are null | Chan inv | Chan sells |
| H5-w{5,10} | a Chan buy (1B/2B/3B) and a wyk buy (`entry_steady`/`sos`/`lps`) for the same symbol with known dates ≤ W bars apart; entry at the later date | max of the two invalidations | Chan sells ∪ wyk sells |
| H6-* | the named variant's entries, kept only if SPY close at t > SPY 200-day simple MA at t (null MA → dropped) | as the named variant | as the named variant |

"Bars" means the symbol's stored trading bars, not calendar days.

- [ ] **Step 1: Write the failing tests**, one per rule family, on hand-made input frames:
  - `test_h1_type_filter_and_sells`;
  - `test_h1c_uses_confirmed_date_only` (a null `confirmed_date` gives no entry);
  - `test_h2_sos_invalidation_formula` (`range_hi=50, atr_known=2` gives 49.5);
  - `test_h3_requires_active_acc_range_and_recent_event` (range ended at t gives none; event 21 bars ago gives none; 20 bars ago gives an entry);
  - `test_h4_volume_gates` (1.49 gives none, 1.5 gives an entry; pullback 0.81 gives none);
  - `test_h5_window_and_later_date` (5 bars apart → entry at the later date for w5; 6 apart → none for w5, an entry for w10);
  - `test_h6_spy_regime` (199 SPY bars → none; above MA → kept);
  - `test_variant_ids_exact` (`len(VARIANTS) == 18` and the key set equals the list above).
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** the variants as functions returning `Signals`, and the CLI. The CLI loads the inputs once and composes each variant with `compose_table`.
- [ ] **Step 4: Run** `uv run pytest tests/research/test_hypotheses.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): 18 hypothesis variants + compose CLI`.

---

### Task 9: Bridge strategy `research_signal_table`

**Files:**
- Create: `backend/app/strategy/builtin/research_signal_table.py`
- Test: `backend/tests/research/test_bridge_strategy.py`

**Interfaces:**
- Consumes: a variant table parquet (Task 8 columns `symbol, date, entry, exit, score`).
- Produces: the module constants
  - `META` (`id="research_signal_table"`, `asset_types=["stock"]`, `timeframes=["1d"]`, `params=[{"id":"table_path","label":"Signal table","type":"str","default":""}]`, `scoring={}`, `order_by="score"`, `descending=True`, `limit=100000`);
  - `EXECUTION_BACKEND="matrix_native"`, `STOP_LOSS=-0.15`, `MAX_HOLD_DAYS=20`;
  - `BASIC_FILTER={"enabled": False}`;
  - `ENTRY_SIGNALS=["signal_research_entry"]`, `EXIT_SIGNALS=["signal_research_exit"]`;
  - `MATRIX_STRATEGY`.

  `required_fields() == frozenset({"close"})` and `required_warmup_bars(params) == 1`.

Behaviour:
- `compute_signals` reads `pl.read_parquet(params["table_path"])`; an empty path raises `ValueError("table_path required")`.
- It maps the symbol to its column via `market.symbols`, and the date to its row via `label[:10]` of `market.timestamp_labels` (ISO date).
- It sets `entry`/`exit` (uint8), `score` (float32, 0 where no entry), and the codes 0/−1.
- Rows not in the panel are ignored. Their count goes to the module logger at INFO.
- Imports are limited to the AST whitelist: `polars`, `numpy`, `app.backtest.matrix`. Use no `logging` import if the whitelist forbids it; check `app/strategy/ai_generator.py:449-456` and drop the log if so.

- [ ] **Step 1: Write the failing tests:**
  - `test_bridge_loads_through_strategy_engine`: `StrategyEngine(strategy_dirs=[builtin_dir]).get("research_signal_table")` (or the equivalent loader call used in `tests/backtest`) loads without an AST rejection, and `execution_backend == "matrix_native"`.
  - `test_bridge_maps_table_to_matrix`: build a `MarketDataMatrix` stub (follow `tests/backtest/test_strategy_backtest_correctness.py:266-316`) with symbols `("A","B")` and labels `("2024-01-02","2024-01-03T00:00:00")`. A table with A entry on 01-02, B exit on 01-03, and a row for `"ZZZ"` gives exactly `entry[0,0] == 1` and `exit[1,1] == 1`, with nothing else set and no error.
  - `test_bridge_passthrough_in_service`: through `StrategyBacktestService` with the stub engine as in the correctness test, with `matching="open_t+1"`, the number of entry candidates equals the table's entry rows in the panel. This proves that no basic filter or scoring dropped or re-ordered anything.
  - `test_bridge_no_reentry_while_held`: entries on 3 consecutive days for one symbol with `max_positions=10` give exactly 1 trade. This verifies `engine.py:2107`.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** the strategy.
- [ ] **Step 4: Run** `uv run pytest tests/research/test_bridge_strategy.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(strategy): research_signal_table bridge (matrix_native)`.

---

### Task 10: Baselines (random entry tables, buy-and-hold curves)

**Files:**
- Create: `backend/app/research/baselines.py`
- Test: `backend/tests/research/test_baselines.py`

**Interfaces:**
- Consumes: `compose_table`, `Signals` (Task 7); the eligibility frame (Task 2); `atr14`.
- Produces:
  - `invalidation_k(table: pl.DataFrame) -> float | None`: median of `(close − invalidation) / atr14` over entry rows; None when there are no entries.
  - `random_signals(variant_table: pl.DataFrame, elig: pl.DataFrame, bars: pl.DataFrame, seed: int) -> Signals | None`.
    - For each date t with n_t entry rows in the variant table, draw n_t distinct symbols uniformly from the eligible set at t with `numpy.random.default_rng(seed)`; dates are processed in ascending order.
    - Invalidation = `close_t − k·atr14_t`.
    - No sells.
    - Returns None when `invalidation_k` is None.
  - `buy_and_hold_curve(bars: pl.DataFrame, symbols: list[str], start: date, end: date, capital: float = 1_000_000.0, commission_per_share=0.005, commission_min=1.0, slippage_bps=5.0) -> pl.DataFrame` with columns `date, equity`.
    - Equal capital per symbol.
    - Buy at the first day's open plus slippage; whole shares; cash is kept.
    - Mark at the close each day, forward-filling a missing close.
    - Sell at the last day's close minus slippage, with a commission on both legs.
  - `b0_bh(bars, elig, start, end)` = `buy_and_hold_curve` over the symbols eligible on `start` (the first trading date ≥ start).
  - `b0_spy(bars_spy, start, end)` = the same for `["SPY"]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_random_matches_daily_counts_and_universe`: the counts per day equal the variant's, and every drawn symbol is eligible that day.
  - `test_random_seed_reproducible`: same seed, same table; different seeds, different tables.
  - `test_random_baseline_no_entries` returns None.
  - `test_invalidation_k_median`.
  - `test_buy_and_hold_costs`: 1 symbol, open 10, close 11 every day, 3 days; equity at the end = the hand-computed value including both commissions and both slippages.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/research/test_baselines.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): random-entry and buy-and-hold baselines`.

---

### Task 11: Metrics and the deflated Sharpe ratio

**Files:**
- Create: `backend/app/research/metrics.py`
- Test: `backend/tests/research/test_metrics.py`

**Interfaces:**
- Produces:
  - `trade_r(trades: list[dict], table: pl.DataFrame) -> list[float]`.
    - Join each trade on `(symbol, entry_signal_date)` to the table's entry row.
    - R = `(exit_price − entry_price) / max(entry_price − invalidation, 0.5 · atr14)`.
    - Unmatched trades are skipped and counted in the logger.
  - `curve_stats(equity: pl.DataFrame) -> dict` with keys `cagr, sharpe_ann, sharpe_daily, max_drawdown, n_days, skew, kurtosis`, computed from daily simple returns. `kurtosis` is non-excess (normal = 3). Annualise with 252.
  - `exposure(trades, n_days) -> float`: the average of the number of positions held per day / max_positions.
  - `psr(sr: float, sr_ref: float, n: int, skew: float, kurt: float) -> float` = `Φ(((sr − sr_ref)·√(n−1)) / √(1 − skew·sr + (kurt−1)/4·sr²))`.
  - `expected_max_sr(var_sr: float, n_trials: int) -> float` = `√var_sr · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))` with γ = 0.5772156649.
  - `dsr(sr, n, skew, kurt, var_sr_trials, n_trials) -> float` = `psr(sr, expected_max_sr(var_sr_trials, n_trials), n, skew, kurt)`. When `n_trials == 1` it returns `psr(sr, 0, …)`.

  All Sharpe inputs to `psr`/`dsr` are per-period (daily), not annualised.
- `summarize(trades, equity, table, n_trials, var_sr_trials) -> dict` returns:
  `trades, win_rate, avg_r, median_r, cagr, sharpe_ann, max_drawdown, exposure, dsr` plus `reason` when there are no trades.

- [ ] **Step 1: Write the failing tests:**
  - `test_psr_formula_hand_value`: `psr(0.1, 0.0, 251, 0.0, 3.0)` equals `NormalDist().cdf(0.1*sqrt(250)/sqrt(1 + 0.5*0.01))` to 1e-12.
  - `test_expected_max_sr_monotonic_in_trials`.
  - `test_dsr_published_example`: the numerical example in Bailey & López de Prado (2014), "The Deflated Sharpe Ratio", section 6. The test author must open the paper and copy its inputs and stated DSR into the test with a tolerance of 0.005. The coordinator verifies that the constants match the paper before approving the task. If the paper is unavailable, stop and report; do not invent values.
  - `test_metrics_zero_trades`: `summarize([], flat_equity, empty_table, 18, 0.01)` gives `trades == 0`, `avg_r is None`, `reason == "no trades"`, and no exception.
  - `test_trade_r_floor`: entry 100, inv 99.9, atr 2 → denominator 1.0.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement** with `statistics.NormalDist`.
- [ ] **Step 4: Run** `uv run pytest tests/research/test_metrics.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): trade R, curve stats, PSR/DSR`.

---

### Task 12: Registry, freeze, holdout lock

**Files:**
- Create: `backend/app/research/registry.py`, `backend/scripts/research/freeze.py`
- Test: `backend/tests/research/test_registry.py`

**Interfaces:**
- Produces:
  - `Registry(path: Path)` with these methods:
    - `.record(variant_id: str, period: Literal["dev","holdout"], table_hash: str, code_hash: str, sharpe_daily: float | None, n_obs: int) -> None` appends `{…, "run_at": iso}` to `registry.json`, written atomically.
    - `.rows() -> list[dict]`.
    - `.n_trials() -> int` = the number of distinct `variant_id`s with a dev row, plus the number of holdout rows beyond the first per variant.
    - `.var_sr_trials() -> float` = the variance of the latest dev `sharpe_daily` per variant; 0.0 if fewer than 2.
  - `Pins` (dataclass): `data_generation: str, last_date: str, universe_hash: str, chan_version: str, wyckoff_version: str, table_hashes: dict[str,str], bridge_hash: str`.
  - `freeze(registry: Registry, pins: Pins, variant_ids: list[str], out: Path) -> dict` writes `registry.frozen.json` = `{"variants", "n_trials", "pins", "frozen_at", "sha256"}`. `sha256` is computed over the canonical JSON of the other keys.
  - `check_holdout_allowed(frozen_path: Path, current: Pins, variant_id: str) -> None` raises `HoldoutLocked(reason)` when:
    - the file is missing;
    - the sha256 doesn't match;
    - any pin differs (the reason names it);
    - the variant is not in `variants`.
  - CLI `scripts/research/freeze.py`:
    - computes the current pins: `KlineRepository(DataStore(data_dir)).get_matrix_data_generation("stock")`, `RESEARCH_LAST_DATE`, the universe hash, both engine versions, the latest table hash per variant, and `file_hash(bridge)`;
    - refuses if any of the 18 variants lacks a dev row;
    - writes the frozen file.

The registry and frozen file live at `<data_dir>/research/registry.json` and `registry.frozen.json`. They are data, not repo files; every report copies them in.

- [ ] **Step 1: Write the failing tests:**
  - `test_holdout_refused_without_freeze`;
  - `test_holdout_refused_on_tampered_file` (edit `n_trials` → sha mismatch);
  - `test_holdout_refused_on_pin_change` (a different `chan_version` → the reason names `chan_version`);
  - `test_holdout_refused_for_unfrozen_variant`;
  - `test_n_trials_counts_dev_variants_and_holdout_reruns` (3 variants dev, 1 holdout twice → 4);
  - `test_registry_write_atomic` (no `.tmp` left; JSON valid).
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/research/test_registry.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): run registry, freeze, holdout lock`.

---

### Task 13: Integrity checks

**Files:**
- Create: `backend/app/research/integrity.py`
- Test: `backend/tests/research/test_integrity.py`

**Interfaces:**
- Consumes: `replay_symbol` (Task 3), `wyckoff_symbol` (Task 6), `load_bars`, `assert_no_future_columns`.
- Produces:
  - `chan_truncation_check(data_dir, symbols: list[str], cuts_per_symbol: int = 5, seed: int = 0, snapshot_fn=None) -> dict`.
    - For each symbol, pick `cuts_per_symbol` random cut indices in `[200, len−1]`.
    - Replay `bars[:cut]` and compare it with the stored points that have `known_date <= date[cut−1]`. Compare `confirmed_date` as `min(stored, cut)`: a stored `confirmed_date` after the cut is null in the truncated run.
    - Returns `{"checked": int, "mismatches": list[dict]}`.
  - `wyckoff_truncation_check(data_dir, symbols, cuts_per_symbol=5, seed=0) -> dict`: the same for events, plus active ranges.
  - `phantom_rate(points: pl.DataFrame, gone: pl.DataFrame) -> pl.DataFrame`.
    - A phantom is a gone row with `point_dt >= window_start`.
    - Returns per type: `type, first_seen, phantoms, rate`.
  - `determinism_check(data_dir, symbols: list[str]) -> dict`: replays into a temporary directory and compares the bytes with the stored parts. It returns `{"identical": int, "different": list[str]}`.

- [ ] **Step 1: Write the failing tests** with the fake `snapshot_fn` from Task 3:
  - `test_chan_truncation_detects_injected_leak`: a fake whose snapshot at length L depends on the full series (a closure over a global), so the replay differs → `mismatches` is non-empty. An honest fake gives an empty list.
  - `test_phantom_counts_only_inside_window`: 2 gone rows, one with `point_dt` before `window_start` → 1 phantom.
  - `test_determinism_detects_change`.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/research/test_integrity.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): truncation, phantom, determinism checks`.

---

### Task 14: Evaluation runner and CLI

**Files:**
- Create: `backend/app/research/evaluate.py`, `backend/scripts/research/evaluate.py`
- Test: `backend/tests/research/test_evaluate.py`

**Interfaces:**
- Consumes: Tasks 2 and 7–13; ZenTrade `DataStore`, `KlineRepository`, `StrategyEngine`, `BacktestEngine`, `StrategyBacktestService`, `StrategyBacktestConfig` (wiring as in `backend/app/backtest/worker.py:196-219`).
- Produces:
  - `Period` (dataclass): `name: Literal["dev","holdout"], start: date, end: date`.
    - `dev_period(elig)` starts at the first date whose eligible count is ≥ `min(top_n, max daily eligible count)` (a full universe), and ends at `DEV_END`. On the real store this is 2023-11-17: a handful of symbols with older history are eligible from 2023-09-22, but the universe holds 7 names until it jumps to 1,500 on 2023-11-17. Test: `test_dev_period_starts_at_full_universe`.
    - `holdout_period()` is `HOLDOUT_START`–`HOLDOUT_END`.
  - `run_table(svc, table_path: Path, period: Period, slippage_bps: float = 5.0) -> StrategyBacktestResult`, with `StrategyBacktestConfig(strategy_id="research_signal_table", symbols=None, start, end, params={"table_path": str(path)}, max_positions=10, position_sizing="equal", entry_fill="open_t+1", exit_fill="open_t+1", slippage_bps=…, mode="position")`.
  - `folds(period: Period, trading_days: list[date], test_len: int = 126, embargo: int = 5) -> list[Period]`: consecutive windows, each starting `embargo` bars after the previous one ends.
  - `evaluate_variant(ctx, variant_id, period, seeds: int = 200) -> dict` returns:
    - the main metrics;
    - the stress metrics;
    - per-fold metrics (dev only);
    - random-baseline CAGRs and the variant's percentile;
    - `gate` (holdout only): `{passed: bool, checks: {cagr_vs_bh, cagr_vs_rand95, trades_ge_100, avg_r_pos, dsr_ge_0_9}, fragile_to_costs: bool}`.

    It records a dev or holdout registry row.
  - CLI: `scripts/research/evaluate.py --period dev|holdout [--variants ...] [--seeds 200] [--workers N]`.
    - `--period holdout` calls `check_holdout_allowed` per variant first.
    - It runs the integrity checks: truncation on 50 random union symbols × 5 cuts (Chan) and 50 (Wyckoff), the phantom rate, and determinism on 10 symbols.
    - It aborts with a non-zero exit if truncation or determinism has any mismatch.
    - It writes the report (Task 15).

Runtime rule: random seeds run through the same ZenTrade path, in a process pool, each worker with its own `StrategyBacktestService`. Before a full run, the CLI times one run and prints `runs × seconds / workers` as an ETA. It continues without asking; the coordinator reports the ETA to the user.

- [ ] **Step 1: Write the failing tests** with a monkeypatched `run_table` (a fake result object with `trades` and `equity_curve`):
  - `test_gate_all_checks`: hand values that pass every check → `passed`; flip each one → that check is False and `passed` is False.
  - `test_fragile_to_costs_flag`: passes at 5 bps and fails at 15 → `passed` is True and `fragile_to_costs` is True.
  - `test_folds_embargo`: 400 trading days → windows of 126 with 5-day gaps; none crosses the period end.
  - `test_holdout_requires_freeze`: the CLI entry with no frozen file raises `HoldoutLocked` before any `run_table` call.
  - `test_evaluate_records_registry_row`.
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/research/test_evaluate.py -q`. Expect a PASS.
- [ ] **Step 5: Commit** with the message `feat(research): evaluation runner, folds, gate, CLI`.

---

### Task 15: Report and end-to-end integration test

**Files:**
- Create: `backend/app/research/report.py`
- Test: `backend/tests/research/test_e2e_harness.py`

**Interfaces:**
- Produces: `write_report(out: Path, run_name: str, results: list[dict], baselines: dict, integrity: dict, registry_rows: list[dict], frozen: dict | None, deviations_md: str) -> Path`.

  Sections, in order:
  1. a summary table (variant, trades, win rate, avg R, CAGR, Sharpe, MDD, exposure, random percentile, DSR, gate);
  2. baselines (B0-bh, B0-spy with CAGR, Sharpe and MDD);
  3. cost stress;
  4. dev folds;
  5. integrity (truncation counts, phantom rate per type, determinism);
  6. trial count N and the reruns;
  7. caveats, verbatim:
     - "Survivorship: moomoo lists only stocks trading today; every baseline uses the same survivor universe."
     - "Prices are forward-adjusted with today's factors."
     - "Universe classification (ETF/unit/preferred) is today's."
  8. deviations.

- [ ] **Step 1: Write the failing test `test_e2e_harness`** (real engines, temporary data dir):
  - Write 5 synthetic symbols plus SPY × 300 trading days of `kline_daily` partitions (`date=YYYY-MM-DD/part.parquet`, the columns from `kline_daily`) and an `instruments/instruments.parquet` marking the 5 as common stock.
  - Monkeypatch `settings.data_dir`, then call `app.indicators.pipeline.run_pipeline(data_dir=tmp)` to build enriched data and the generation.
  - Run `run_chan_replay` and `run_wyckoff_replay` (workers=1), compose `H1-2B` and `H2-sos` with `UniverseParams(min_bars=120, min_price=1, min_dollar_vol=0, top_n=10)`, then evaluate dev with `seeds=3` and write the report.
  - Assertions:
    - the report exists and contains every section heading;
    - each variant has a `trades` field ≥ 0;
    - the registry has 2 dev rows;
    - truncation mismatches == 0.

  Mark it `@pytest.mark.slow` if it takes more than 60 s.
- [ ] **Step 2: Run it.** Expect FAIL.
- [ ] **Step 3: Implement `write_report`** and fix any wiring gaps the e2e test exposes, each in the module that owns it. List the fixes in the task's worker_done message.
- [ ] **Step 4: Run** `uv run pytest tests/research -q`, the full research suite. Expect a PASS. Then run the full backend suite `uv run pytest tests -q -p no:cacheprovider` and compare it with the baseline (74 failed / 6,325 passed on `feat/moomoo-data`). There must be no new failures outside `tests/research`.
- [ ] **Step 5: Commit** with the message `feat(research): report writer + end-to-end harness test`.

---

### Task 16: Operational run, coordinator only (no pi worker)

Run on the real store after the backfill shows `DONE` and the enriched rows are present.

- [ ] Run `scripts/research/replay_chan.py`. Record the symbol count, `failed` and the wall time.
- [ ] Run `scripts/research/replay_wyckoff.py`. Record the same.
- [ ] Run `scripts/research/compose.py`. Record the per-variant log counts.
- [ ] Run `scripts/research/evaluate.py --period dev`. This writes `docs/research/2026-11-XX-dev.md`.
- [ ] **Stop and report the dev results to the user.** Freeze and holdout happen only after the user says go. If a follow-up variant is wanted, it is added here (it counts toward N) before the freeze.
- [ ] Once the user says go: run `freeze.py`, then `evaluate.py --period holdout`, then report.

---

## Self-Review Notes (coordinator)

**Spec coverage:**
- Goals 1–6 → Tasks 3–4, 5–6, 7–8, 9, 10–12 and 14–15, and 13.
- Builder-check items:
  - 1 → Task 10;
  - 2 → Task 12 (`n_trials`);
  - 3 → Tasks 7, 8 and 11;
  - 4 → Task 10;
  - 5 → Task 7;
  - 6 → Tasks 3 and 13;
  - 7 → Tasks 7 and 9;
  - 8 → Task 12;
  - 9 → Tasks 3 and 8 (H1c);
  - 10 → Task 15 caveats;
  - 11 → Task 8 sells.

**Calls made in this plan (beyond the spec):**
- A neutral hash score replaces `score=1`, to avoid the alphabetical slot bias.
- The tradable filter uses the instruments table flags.
- `post_gap` is true only for gaps of more than 5 calendar days.
- H3 counts `ACC_KINDS` (accumulation and generic) ranges as "accumulation TR".
- The H4 legs come from the strokes recorded at first sight.
- The random-baseline invalidation is measured from the signal-day close.
- Dev folds are plain consecutive windows, since there are no fitted parameters.
- The registry lives in the data dir.
- The bridge appears in ZenTrade's strategy list as a research strategy (unrequested, visible in the UI list).
