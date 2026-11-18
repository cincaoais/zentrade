# Moomoo SIMULATE Forward Test (Step 4a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Here execution runs through Orca orchestration: one pi (or Antigravity) worker per task; Claude Code reviews, runs the tests and commits each task.

**Goal:** Run the research leads H2-spring and H6-H4-3B forward every trading day on the moomoo SIMULATE account, where the unchanged research backtest decides each morning's trades from the real opening prices.

**Architecture:** New package `backend/app/forward/` plus CLI `backend/scripts/forward/run.py`. Nightly: data readiness, point extension, table preparation, night parity. Morning: read opens, build a temp engine store with one open-only bar, run `research.evaluate.run_table`, turn the engine's actions dated today into SIMULATE market orders through a SIMULATE-only broker wrapper with an append-only journal. During the session: a −15% stop watch. After the close: reconcile. No research or engine file is edited.

**Tech Stack:** Python 3.14, polars, `moomoo-api==10.12.7208` (already a dependency), pytest, ruff. Run everything with `uv run` from `backend/`.

**Spec:** `docs/superpowers/specs/2026-11-18-moomoo-forward-test-design.md` (approved 2026-11-18). Read it before any task; section numbers below refer to it.

## Global Constraints

- Leads: `H2-spring`, `H6-H4-3B`. Short ids in order keys: `h2s`, `h6h43b`.
- Backtest config = research: `max_positions=10`, `position_sizing="equal"`, `entry_fill="open_t+1"`, `exit_fill="open_t+1"`, `slippage_bps=5`, cost model `us_per_share_v1`, `mode="position"`, strategy `research_signal_table`; `initial_capital` = the registered capital. Use `app.research.evaluate.run_table` (pass the capital through a wrapper that builds the same `StrategyBacktestConfig` with `initial_capital` set; do not edit `evaluate.py`).
- Pinned, never edited: `backend/app/research/**`, `backend/app/backtest/engine.py`, `backend/app/backtest/strategy.py`, `backend/app/strategy/builtin/research_signal_table.py`, `backend/scripts/research/**`, `<data>/research/registry.frozen.json`.
- Every moomoo trade call passes `trd_env=TrdEnv.SIMULATE` and the registered `acc_id` explicitly. **The SDK's default is `trd_env='REAL'`.** Only `app/forward/broker.py` may import `moomoo` trade classes. `app/forward/` never contains the strings `unlock_trade` or `TrdEnv.REAL`.
- Tests: no OpenD, no network, never the real data dir (use `tmp_path` fixtures). Workers never run the daemon, never connect to OpenD, never run `--check-account`, `flatten` or anything that talks to the account.
- Times are US Eastern via `app.market_clock.us_clock`. Deadlines: night ≥ 18:00 on t, data deadline 07:00 on t+1, opens polled 09:30-09:35, orders by 09:40, poll and cancel by 10:00, stop watch 09:30-16:00, reconcile 16:15.
- Order pacing: `place_order` ≥ 2.5 s apart; order queries ≥ 3.5 s apart. Order key `fw-<short>-<yyyymmdd>-<SYMBOL>-<B|S>`, retry suffix `-r1`, flatten key `fw-flatten-<yyyymmdd>-<SYMBOL>`.
- Caps: ≤ 25 orders per strategy per day; entry notional ≤ 1.1 × engine equity / 10; open notional ≤ live equity.
- Records live under `<data>/forward/` (outside the repo); reports under `docs/forward/`.
- Run tests: `cd backend && uv run pytest tests/forward -q`; lint: `uv run ruff check app/forward scripts/forward tests/forward`. The 259 research tests stay green: `uv run pytest tests/research -q`.

## Review Focus

1. A holiday, early close or a day with no bar for a held symbol (halt): the engine treats the symbol as not tradable; the runner must record `exit_delayed` / `no_open` and never invent a price. Test in Task 6 and Task 9.
2. OpenD restarted mid-morning: an order whose `place_order` call raised after the journal said `submitting` must be matched by remark and never re-sent. Test in Task 8.
3. Both leads hold the same symbol and one exits: only that strategy's lot is sold and reconcile still balances. Test in Task 9.
4. The engine's `end` sales on the open-only bar must never become orders. Test in Task 6.
5. A pinned file or the frozen registry changes between night and morning: the morning refuses to decide. Test in Task 1 and Task 12.

---

### Task 1: Pins and registration

**Files:**
- Create: `backend/app/forward/__init__.py`, `backend/app/forward/config.py`, `backend/app/forward/pins.py`, `backend/app/forward/registration.py`
- Test: `backend/tests/forward/__init__.py`, `backend/tests/forward/test_registration.py`

**Interfaces:**
- Produces:
  - `config.LEADS: dict[str, str] = {"H2-spring": "h2s", "H6-H4-3B": "h6h43b"}`, `config.forward_dir(data_dir: Path) -> Path` (= `data_dir / "forward"`), `config.PINNED_FILES: tuple[str, ...]` (repo-relative paths listed in Global Constraints that are files, plus `backend/app/research/compose.py`, `hypotheses.py`, `universe.py`, `ranking.py`, `bars.py`).
  - `pins.current_pins(repo_root: Path, data_dir: Path) -> dict[str, str]`: keys `chan_version`, `wyckoff_version` (from `app.research.versions`), `bridge_hash` (`versions.file_hash` of `research_signal_table.py`), one `file:<relpath>` sha256 per `PINNED_FILES` entry, `frozen_registry` sha256 of `<data>/research/registry.frozen.json`, `forward_code` sha256 over the sorted `app/forward/*.py` files.
  - `pins.diff_pins(expected: dict, actual: dict) -> list[str]` (names of differing or missing keys, sorted).
  - `class PinMismatch(RuntimeError)`; `registration.require_pins(reg: dict, repo_root: Path, data_dir: Path) -> None` raises `PinMismatch` naming the keys.
  - `registration.write_registration(data_dir, *, capital_per_strategy: float, start_date: date, account_id: int, var_sr_trials: float, repo_root: Path, supersede_reason: str | None = None) -> Path`; refuses if a registration exists unless `supersede_reason` is given, in which case the old file is moved to `forward/superseded/<sha8>.json` and its sha recorded in the new one.
  - `registration.load_registration(data_dir) -> dict`, `registration.registration_sha256(data_dir) -> str`.
- Registration JSON content: per strategy `variant_id`, `short`, `variant_config` (from `hypotheses.variant_config`), `backtest` (the config values in Global Constraints with `initial_capital`), `max_hold_days: 20`, `stop_loss: -0.15`; `pins`; `start_date`; `account_id`; `evaluation` block = the text of spec section 9 as fields (`verdict_book: "engine"`, `min_months: 12`, `min_closed_trades: 100`, `gate: ["cagr_vs_bh", "cagr_vs_rand95", "trades_ge_100", "avg_r_pos", "dsr_ge_0_9"]`, `n_trials: 71`, `var_sr_trials`); `real_never_used: true`.

- [ ] **Step 1: Write failing tests** `test_registration.py`:
  - `test_current_pins_stable` (two calls equal; keys include `chan_version`, `bridge_hash`, `frozen_registry`, `forward_code`).
  - `test_diff_pins_names_changed_key` (`diff_pins({"a":"1","b":"2"},{"a":"1","b":"3"}) == ["b"]`; a missing key is listed).
  - `test_write_once` (second `write_registration` without `supersede_reason` raises `FileExistsError`).
  - `test_supersede_moves_old` (old file under `superseded/`, new JSON has `supersedes` = old sha256 and `supersede_reason`).
  - `test_require_pins_raises_on_change` (write registration with a fake repo root in `tmp_path`, edit one pinned file, `require_pins` raises `PinMismatch` whose message contains that file's key).
  - `test_evaluation_block_is_research_gate` (gate list exactly the five names, `n_trials == 71`).
  - Use a `tmp_path` fake repo tree containing copies of the pinned files and a fake `registry.frozen.json`; `current_pins` takes `repo_root` so it never reads the real tree in tests.
- [ ] **Step 2:** `uv run pytest tests/forward/test_registration.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement the interfaces. Atomic writes (`tmp` + `os.replace`). JSON with `sort_keys=True, indent=2`.
- [ ] **Step 4:** Tests pass; ruff clean.
- [ ] **Step 5:** Commit `feat(forward): pins and write-once registration`.

### Task 2: Forward bars loader, universe snapshots, readiness

**Files:**
- Create: `backend/app/forward/data.py`
- Test: `backend/tests/forward/test_data.py`

**Interfaces:**
- Consumes: `app.research.bars.BAR_COLUMNS`, `EXCLUDED_SYMBOLS`, `tradable_symbols`; `app.research.universe.eligibility`, `UniverseParams`.
- Produces:
  - `load_forward_bars(data_dir, symbols: list[str] | None = None, start: date | None = None, end: date | None = None) -> pl.DataFrame` — same as `research.bars.load_bars` but without the `RESEARCH_LAST_DATE` clip (`end=None` = no upper bound).
  - `snapshot_universe(data_dir, d: date) -> Path` writes `forward/universe/<d>.parquet` (column `symbol`, sorted) from `tradable_symbols(data_dir)`; refuses to overwrite an existing different snapshot (returns the path if identical).
  - `forward_tradable(data_dir, d: date) -> set[str]` = union of all snapshots with date ≤ d (a symbol once tradable stays in, so later delistings do not drop it); raises `FileNotFoundError` if none ≤ d.
  - `class Readiness(NamedTuple): ready: bool; missing: list[str]; coverage: float; checked_at: str`.
  - `check_readiness(data_dir, t: date, required: list[str], universe: list[str], min_coverage: float = 0.98) -> Readiness` — ready iff every `required` symbol has a bar on `t` and the share of `universe` with a bar on `t` ≥ `min_coverage`.
  - `history_hashes(bars: pl.DataFrame, before: date) -> dict[str, str]` (per symbol sha256 of the canonical rows with `date < before`), `changed_symbols(old: dict, new: dict) -> list[str]`.
  - `preserve_bars(data_dir, symbols: list[str], through: date) -> int` copies those symbols' enriched rows into `forward/bars/date=*/part.parquet` (append, idempotent); `load_forward_bars` returns the union of the store and `forward/bars/`, store rows winning on duplicate `(symbol, date)`.
  - Step 0 (discovery, module docstring): which daily-pipeline functions delete or rewrite enriched partitions or drop symbols (`jobs/daily_pipeline._prune_partial_enriched_partitions`, `_prune_stale_price_partitions`, and any instrument-sync removal), and whether a delisted symbol's past rows survive. If they always survive, `preserve_bars` stays but the night step calls it only for held symbols.

- [ ] **Step 1: Failing tests:** `test_preserved_bars_survive_store_deletion` (delete a symbol's rows from the fixture store after `preserve_bars`; `load_forward_bars` still returns them),  `test_loader_matches_research_before_cutoff` (fixture store with `kline_daily_enriched/date=*/part.parquet` across 2026-11-12..2026-11-17; for `end=2026-11-13` frames equal `research.bars.load_bars(...)` with `.equals`), `test_loader_reads_past_cutoff` (rows dated 2026-11-17 present), `test_forward_tradable_keeps_delisted` (snapshot day1 {A,B}, day2 {A}; `forward_tradable(day2) == {"A","B"}`), `test_snapshot_refuses_different_overwrite`, `test_readiness_missing_required` (`ready is False`, `missing == ["HELD"]`), `test_readiness_coverage` (97/100 → not ready, 98/100 → ready), `test_history_hash_detects_revision` (change one old close → symbol listed).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Note for eligibility: callers pass `forward_tradable(...)` where research passes `tradable_symbols(...)`.
- [ ] **Step 4:** Pass; ruff clean.
- [ ] **Step 5:** Commit `feat(forward): forward bar loader, universe snapshots, readiness`.

### Task 3: Measure the point replay (discovery, no product code)

**Files:**
- Create: `backend/scripts/forward/measure_replay.py`, `docs/forward/notes/replay-measure.md`

**Interfaces:**
- Consumes: `app.research.chan_replay.replay_symbol`, `_real_snapshot`; `app.research.wyckoff.replay.wyckoff_symbol`.
- Produces: a written note answering, with numbers and file:line references: (a) can a symbol's replay be extended by one day from saved state without editing pinned files (what state `replay_symbol` / `wyckoff_symbol` carry between days, e.g. the previous snapshot for "gone" tracking); (b) wall time and peak RSS of a fresh full replay for 5 symbols of median history length (Chan and Wyckoff separately), extrapolated to the forward universe size; (c) recommendation: incremental or nightly full re-replay of the needed symbols.

- [ ] **Step 1:** Read `chan_replay.replay_symbol` (lines 139-235) and `wyckoff/replay.wyckoff_symbol` (86-170); list the per-day state.
- [ ] **Step 2:** The script copies 5 symbols' bars from the real store into `tmp` (read-only on the real store), runs both replays fresh with `time.perf_counter` and `psutil` peak RSS, prints a JSON summary. Workers do not run it on the real store; Claude runs it once and pastes the output into the note.
- [ ] **Step 3:** Write the note.
- [ ] **Step 4:** Commit `docs(forward): replay measurement and incremental-vs-full decision`. Claude decides Task 4's mode from the note before dispatching Task 4.

### Task 4: Point extension (N2)

**Files:**
- Create: `backend/app/forward/signals.py`
- Test: `backend/tests/forward/test_signals.py`

**Interfaces:**
- Consumes: Task 2 `load_forward_bars`, `history_hashes`, `changed_symbols`; research replay functions; Task 3's decision.
- Produces:
  - `forward_points_dir(data_dir) -> Path` (= `forward/points/`, separate from the research store: `chan/<symbol>.parquet`, `wyckoff_events/<symbol>.parquet`, `wyckoff_ranges/<symbol>.parquet`, `state/<symbol>.json` if incremental).
  - `extend_points(data_dir, symbols: list[str], t: date, *, snapshot_fn=None, workers: int = 1) -> dict` — brings each symbol's points to exactly what a fresh replay over bars ≤ t yields; full re-replay for symbols in `changed_symbols`; returns `{"extended": [...], "replayed": [...], "failed": [...]}`.
  - `load_points_through(data_dir, t: date) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]` (chan points, wyckoff events, wyckoff ranges) with `known_date ≤ t` (use the research column names for the knowledge date).
  - `verify_sample(data_dir, symbols: list[str], t: date) -> list[str]` — fresh replay vs stored, returns symbols that differ.

- [ ] **Step 1: Failing tests** (injected fake `snapshot_fn` as in `tests/research` chan tests, no czsc needed): `test_extend_day_by_day_equals_fresh` (extend through days 1..N one at a time; frames equal a single fresh replay through N), `test_history_change_triggers_full_replay` (revise an old bar; symbol in `replayed`; output equals fresh replay on revised bars), `test_load_points_through_filters_known_date`, `test_verify_sample_reports_difference` (corrupt one stored row → symbol returned).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement per Task 3's decision (no pinned file edits).
- [ ] **Step 4:** Pass; ruff; `uv run pytest tests/research -q` still 259 passed.
- [ ] **Step 5:** Commit `feat(forward): point-in-time point extension with equivalence checks`.

### Task 5: Forward tables (compose with the frozen variant code)

**Files:**
- Create: `backend/app/forward/tables.py`
- Test: `backend/tests/forward/test_tables.py`

**Interfaces:**
- Consumes: `app.research.hypotheses.VARIANTS`, `Inputs`; `app.research.compose.compose_table`, `with_atr14`; `app.research.ranking.rank_features` (as `scripts/research/compose.py:110-120` does); Task 2 `forward_tradable`; `universe.eligibility`.
- Produces:
  - `build_inputs(bars: pl.DataFrame, spy: pl.DataFrame, chan: pl.DataFrame, wyk_events: pl.DataFrame, wyk_ranges: pl.DataFrame) -> Inputs` (same construction as `scripts/research/compose.py:108-121`, ATR via `with_atr14`).
  - `forward_table(variant_id: str, inputs: Inputs, elig: pl.DataFrame, bars: pl.DataFrame) -> tuple[pl.DataFrame, dict]` = `compose_table(VARIANTS[variant_id](inputs), bars, elig)`.
  - `forward_eligibility(data_dir, bars: pl.DataFrame, d: date) -> pl.DataFrame` = `universe.eligibility(bars, forward_tradable(data_dir, d), UniverseParams())`.
  - `kept_entry_symbols(table: pl.DataFrame, start: date) -> set[str]` (symbols with `entry` true on or after `start`).
  - `candidates_on(signals_entries: pl.DataFrame, elig: pl.DataFrame, t: date) -> list[str]` (eligible day-t entry symbols before the gap filter).

- [ ] **Step 1: Failing tests:** `test_forward_table_equals_research_table_on_past_data` — on a small fixture (bars + fake points), `forward_table` output equals what `scripts/research/compose.py`'s path produces for the same inputs (call `compose_table(VARIANTS[...](Inputs(...)), ...)` directly in the test); `test_last_day_entry_dropped_without_next_bar` (documents compose's behaviour: day-t entry absent when bars end at t); `test_last_day_entry_kept_with_open_only_bar` (append an open-only bar for t+1 with open > invalidation → entry on t present; with open ≤ invalidation → absent); `test_candidates_on_before_gap_filter`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Pass; ruff.
- [ ] **Step 5:** Commit `feat(forward): compose forward tables with the frozen variants`.

### Task 6: Morning engine run with the open-only bar

**Files:**
- Create: `backend/app/forward/engine_run.py`
- Test: `backend/tests/forward/test_engine_run.py`

**Interfaces:**
- Consumes: Task 5; `app.research.evaluate.run_table`, `equity_frame`; `scripts/research/evaluate._build_service` logic (re-implement the 10-line builder in `engine_run.build_service(store_dir)`; do not import the script).
- Produces:
  - `open_only_rows(bars_t: pl.DataFrame, opens: dict[str, float], t1: date) -> pl.DataFrame` — one row per symbol in `opens`: all enriched columns copied from that symbol's day-t row, then `date=t1`, `open=high=low=close=opens[s]`; volume and amount kept from day t. Symbols without an open get no row.
  - `build_engine_store(src_data_dir, dst: Path, symbols: list[str], start: date, end: date, extra_rows: pl.DataFrame | None) -> Path` — writes `dst/kline_daily_enriched/date=*/part.parquet` for `symbols` over `[start, end]` plus `extra_rows`, copies `instruments/` and whatever else the engine reads (Step 1 lists it), never writes `src_data_dir`.
  - `run_engine(store_dir: Path, table: pl.DataFrame, start: date, end: date, capital: float) -> object` — writes the table to `store_dir`, runs `run_table` with Global Constraints config and `initial_capital=capital`, `slippage_bps=5`.
  - `class Action(NamedTuple): kind: str ("buy"|"sell"); symbol: str; shares: int; price: float; reason: str; signal_date: str`.
  - `actions_on(result, t1: date) -> list[Action]` — trades with `entry_date == t1` → buy, `exit_date == t1` → sell; trades with `exit_reason == "end"` on `t1` are dropped; sells first, then buys in the engine's order.
  - `relevant_symbols(table: pl.DataFrame, start: date, held: set[str], t: date) -> list[str]` = sorted(`kept_entry_symbols(table, start)` ∪ held ∪ day-t candidates).

- [ ] **Step 1: Discovery** (write findings as a module docstring): which paths under the data dir `BacktestEngine` / `KlineRepository` read for this strategy (enriched partitions, instruments, factor registry, strategy dirs, matrix cache — disable or redirect the cache into `dst`), and which fields decide "tradable" (`matrix.tradable`).
- [ ] **Step 2: Failing tests** on a fixture store (≥ 60 bars, 12 symbols): `test_end_sales_never_actions`; `test_entry_on_t_needs_open_only_bar` (no extra row → no buy on t1; with row → buy with engine shares); `test_shares_follow_equity` (two runs differing in prior P&L → different share counts, equal to `affordable_shares(equity/10, price)` within the engine's rules); `test_gapped_top_candidate_promotes_next` (top candidate open ≤ invalidation → next candidate bought); `test_no_open_row_means_no_trade_for_symbol`; `test_restricted_symbols_same_trades` (full fixture universe vs `relevant_symbols` → identical trades and equity); `test_source_store_untouched` (hash of src tree before == after).
- [ ] **Step 3:** Run → FAIL. **Step 4:** Implement. **Step 5:** Pass; ruff.
- [ ] **Step 6:** Commit `feat(forward): morning engine run with an open-only bar`.

### Task 7: Historical rehearsal over the final year

**Files:**
- Create: `backend/app/forward/rehearse.py`, `backend/scripts/forward/rehearse.py`
- Test: `backend/tests/forward/test_rehearse.py`

**Interfaces:**
- Consumes: Tasks 2, 5, 6; research points from the research store (`load_chan_points`, `load_wyckoff_events`, `load_wyckoff_ranges`) filtered by knowledge date ≤ d; `evaluate.holdout_period()`.
- Produces: `rehearse(data_dir, variant_id, start: date, end: date, capital: float, work_dir: Path) -> dict` with `{"days": n, "reference_trades": n, "rehearsal_trades": n, "differences": [...], "intraday_stops": n}`. Reference = one `run_table` over `[start, end]` on the real store with the research table rebuilt by `forward_table` on full bars. For each trading day d in `[start, end)`: inputs through d, open-only bar for the next trading day from the stored real open, `actions_on`; collected actions must equal the reference trades (symbol, date, shares, reason), except `stop_loss` exits not at the open, which are compared with `low ≤ entry × 0.85` on the full bars.
- The script writes nothing outside `work_dir` and does **not** write to the research registry (it is a reproduction check, not an evaluation). It prints the dict as JSON.

- [ ] **Step 1: Failing test:** `test_rehearsal_reproduces_reference_on_fixture` (fixture year of 40 days, fake points; `differences == []`); `test_rehearsal_reports_injected_difference` (monkeypatch `actions_on` to drop one buy → one difference).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Pass; ruff.
- [ ] **Step 5:** Commit `feat(forward): final-year rehearsal of the morning procedure`.
- [ ] **Step 6 (Claude, not a worker):** run `uv run python scripts/forward/rehearse.py --variant H2-spring` then `--variant H6-H4-3B` in an Orca terminal, one at a time. Expected: `reference_trades` 74 and 120 and `differences: []`. Any difference blocks Tasks 12-13 until explained and fixed.

### Task 8: SIMULATE-only broker wrapper and order journal

**Files:**
- Create: `backend/app/forward/broker.py`, `backend/app/forward/orders.py`, `backend/tests/forward/fake_trade.py`
- Test: `backend/tests/forward/test_broker.py`, `backend/tests/forward/test_orders.py`

**Interfaces:**
- Produces:
  - `broker.SimBroker(account_id: int, ctx_factory: Callable[[], Any] | None = None)`; default factory = `moomoo.OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host, port)` with host/port from `app.plugins.moomoo.provider.opend_address()`. Methods (each passes `trd_env=TrdEnv.SIMULATE, acc_id=account_id`): `check_account() -> dict` (from `get_acc_list`: raises `NotSimulate` unless the id exists with `trd_env == SIMULATE`), `cash() -> float`, `positions() -> dict[str, int]`, `place_market(symbol: str, side: str, qty: int, remark: str) -> str` (order id; `OrderType.MARKET`, `price=0`, `time_in_force=DAY`, `fill_outside_rth=False`), `orders_today() -> list[dict]` (`order_id, code, remark, status, qty, dealt_qty, dealt_avg_price, trd_side`), `cancel(order_id: str) -> None`. Symbols converted with `provider.to_moomoo` / `from_moomoo`.
  - `broker.Quotes(ctx_factory=None)`: `snapshot(symbols) -> dict[str, dict]` with `open_price`, `last_price`, `update_time` via `OpenQuoteContext.get_market_snapshot`.
  - `orders.Journal(path: Path)`: `append(event: dict)` (JSONL, flush + `os.fsync`), `events() -> list[dict]`, `state(key) -> str | None` (`submitting|submitted|filled|partial|cancelled|failed|unknown`).
  - `orders.Executor(broker, journal, *, clock, place_gap_s=2.5, query_gap_s=3.5)`: `submit(key, symbol, side, qty) -> str` (journal `submitting` first; on exception → `resolve(key)`; never re-sends a key whose state is `submitting`/`submitted`/`unknown`), `resolve(key) -> str` (match `orders_today()` by remark; none → `unknown`), `poll_until(deadline) -> dict[key, dict]` (final status, `dealt_qty`, `dealt_avg_price`; cancels open orders at deadline), `daily_count(short, day) -> int` (refuse past 25).
  - `fake_trade.FakeTradeContext`: records every call with kwargs; **raises if a call lacks `trd_env` or passes anything but SIMULATE, or a wrong `acc_id`**; can inject delay, partial fill, reject, exception-after-accept, remark truncation.
- [ ] **Step 1: Failing tests:** `test_every_call_is_simulate` (all methods; fake asserts kwargs), `test_check_account_rejects_real` (account list with the id as REAL → `NotSimulate`), `test_no_real_strings_in_package` (scan `app/forward/*.py` text for `unlock_trade` and `TrdEnv.REAL` → none), `test_only_broker_imports_moomoo_trade` (AST scan: only `broker.py` imports from `moomoo`), `test_journal_before_submit` (fake raises after accepting; journal shows `submitting` before the call; `resolve` finds it by remark; no second `place_order`), `test_unknown_never_resent`, `test_pacing` (fake clock: consecutive `place_order` ≥ 2.5 s apart), `test_partial_exit_retry_once` (remainder sent once with `-r1`), `test_cancel_at_deadline`, `test_daily_cap`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Pass; ruff.
- [ ] **Step 5:** Commit `feat(forward): SIMULATE-only broker wrapper and idempotent order journal`.

### Task 9: Live book and reconciliation

**Files:**
- Create: `backend/app/forward/book.py`
- Test: `backend/tests/forward/test_book.py`

**Interfaces:**
- Consumes: Task 8 `Journal` events.
- Produces:
  - `class Lot(NamedTuple): symbol; shares; entry_date; entry_price; key`.
  - `LiveBook.from_journal(short: str, capital: float, events: list[dict]) -> LiveBook` with `lots: dict[str, Lot]`, `cash: float`, `equity(prices: dict[str, float]) -> float`, `apply_fill(event)`, `scale(symbol, ratio, reason)`, `close_corporate(symbol, cash_amount)`.
  - `reconcile(books: list[LiveBook], broker_positions: dict[str, int], broker_cash: float, adj_events: dict[str, float]) -> dict` → `{"ok": bool, "mismatches": [{"symbol", "broker", "books"}], "scaled": [...], "frozen_symbols": [...]}`: a mismatch with an adjustment event scales lots by the ratio (floor to whole shares, remainder recorded); otherwise the symbol is frozen for new entries.
  - `save_book(path, book)`, `load_book(path)`; `check_saved_matches_journal(path, book) -> bool`.
- [ ] **Step 1: Failing tests:** `test_rebuild_from_journal`, `test_two_strategies_same_symbol_reconcile_ok`, `test_exit_sells_only_own_lot` (exit qty for `h2s` = its lot even if `h6h43b` holds more), `test_split_scales_lots`, `test_unexplained_mismatch_freezes_symbol`, `test_failed_entry_no_lot_and_later_engine_exit_ignored`, `test_halted_exit_stays_open` (no fill → lot remains, status `exit_delayed`), `test_saved_book_drift_detected`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass; ruff.
- [ ] **Step 5:** Commit `feat(forward): per-strategy live book and reconciliation`.

### Task 10: Stop watch

**Files:**
- Create: `backend/app/forward/stopwatch.py`
- Test: `backend/tests/forward/test_stopwatch.py`

**Interfaces:**
- Consumes: Task 8 `Quotes`, `Executor`; Task 9 `LiveBook`.
- Produces: `stop_line(entry_price: float) -> float` (= `entry_price * 0.85`, the engine's entry price, not the live fill); `StopWatch(books, quotes, executor, *, clock, poll_s=5.0)` with `tick(now) -> list[dict]` (one market sell per lot whose `last_price ≤ stop_line`, key `fw-<short>-<yyyymmdd>-<SYMBOL>-S`, each lot at most once per day, records `{"symbol", "stop", "last", "key"}`) and `run_until(end_time)`. Lots entered today are skipped (the engine skips them too).
- [ ] **Step 1: Failing tests:** `test_stop_triggers_once`, `test_entry_day_skipped`, `test_uses_engine_entry_price`, `test_stale_quote_ignored` (quote `update_time` older than 60 s → no action, logged).
- [ ] **Step 2-4:** FAIL → implement → pass; ruff.
- [ ] **Step 5:** Commit `feat(forward): intraday -15% stop watch`.

### Task 11: Night parity and day records

**Files:**
- Create: `backend/app/forward/parity.py`, `backend/app/forward/days.py`
- Test: `backend/tests/forward/test_parity.py`

**Interfaces:**
- Consumes: Task 6 `run_engine`, `Action`; saved decision records (Task 12 format: `decision/<date>.json` with `actions` and `input_hash`).
- Produces:
  - `compare(engine_trades: list[dict], decisions: dict[date, dict], bars_hash_by_date: dict[date, str]) -> dict` → `{"logic_mismatch": [...], "data_revision": [...], "intraday_stops": [...]}`; compares symbol, date, shares, reason, prices; intraday (non-open) `stop_loss` trades are listed separately; a mismatch on a date whose saved `input_hash` differs from the current hash is `data_revision`.
  - `days.record(data_dir, short, d: date, status: str, reason: str = "")` (statuses: `ok, no_plan, missed_open, exit_delayed, exit_incomplete, stop_missed, no_open, gapped_through, logic_mismatch, data_revision, reconcile_mismatch`), `days.missing_days(data_dir, short, start, end) -> list[date]` using `us_clock` trading days.
- [ ] **Step 1: Failing tests:** `test_equal_is_clean`, `test_shares_difference_is_logic_mismatch`, `test_changed_input_is_data_revision`, `test_intraday_stop_listed_separately`, `test_missing_days_found`.
- [ ] **Step 2-4:** FAIL → implement → pass; ruff.
- [ ] **Step 5:** Commit `feat(forward): night parity and per-day status records`.

### Task 12: Night, morning, daemon and CLI

**Files:**
- Create: `backend/app/forward/night.py`, `backend/app/forward/morning.py`, `backend/app/forward/daemon.py`, `backend/scripts/forward/run.py`
- Test: `backend/tests/forward/test_night_morning.py`, `backend/tests/forward/test_cli.py`

**Interfaces:**
- Consumes: Tasks 1-11.
- Produces:
  - `night.run_night(data_dir, t: date, *, repo_root) -> dict`: `require_pins` → snapshot universe → readiness (required = held lots of both books + SPY) → `extend_points` → per strategy: tables through t, `relevant_symbols`, night record `night/<t>.json` (relevant symbols, held, input hashes) → night parity → `days.record`. Not ready by deadline → `no_plan`.
  - `morning.run_morning(data_dir, t1: date, *, broker, quotes, executor, clock, dry_run: bool, repo_root) -> dict`: `require_pins` → opens for relevant symbols (09:30-09:35) → per strategy: engine store + open-only rows → `run_engine` → `actions_on` → `review(actions)` hook (identity) → decision record `decision/<t1>.json` (opens, actions, engine equity, input hashes, pins) → if not `dry_run`: kill-flag check, caps, executor sells then buys (qty from the engine for buys; for sells, the live lot qty) → `poll_until(10:00)` → journal → `days.record`. Without a night record for t: only exits of held lots and stops run; entries skipped with `no_plan`.
  - `review(actions: list[Action]) -> list[Action]` returns its input (step 5 hook).
  - `daemon.run(data_dir, *, dry_run: bool)`: loop over the section 5 timeline with `us_clock`; morning, stop watch, 16:15 reconcile, night (which calls `preserve_bars` for the day's forward universe); on the last trading day of each week the night also runs `verify_sample` on 20 random forward-universe symbols plus every held symbol and records `replay_mismatch` (entries stop) on any difference; sleeps between steps; every start/stop to `audit.jsonl`. In `dry_run` the stop watch logs what it would sell and sends nothing.
  - CLI `run.py` subcommands: `rehearse --variant`, `register --capital --start-date --account-id [--supersede REASON]` (computes `var_sr_trials` from the research registry's 71 holdout rows the same way `evaluate.finalize_trials` does), `status [--check-account] [--test-order SYMBOL]` (`--test-order`: during regular hours, one 1-share limit buy 20% below the last price with remark `fw-test-<yyyymmdd>-<SYMBOL>`, reads it back by remark, prints the order fields, cancels it, prints the final status), `flatten --confirm`, `dry-run`, `daemon`, `night --date`, `morning`, `reconcile`, `parity --date`, `stop`, `report --month`.
  - `flatten`: lists positions and open orders; with `--confirm` cancels orders, sells positions at market (regular hours only) with keys `fw-flatten-<yyyymmdd>-<SYMBOL>`, re-reads until flat.
- [ ] **Step 1: Failing tests** (fake broker, fixture store, fake clock): `test_morning_refuses_on_pin_change`, `test_dry_run_sends_nothing`, `test_kill_flag_blocks_orders`, `test_no_night_record_only_exits`, `test_sells_before_buys_and_pacing`, `test_decision_record_reproducible` (re-run `run_morning` offline from the record → same actions), `test_missed_open_after_10` (clock at 10:01 → entries dropped `missed_open`), `test_flatten_requires_confirm`, `test_test_order_is_cancelled_and_read_by_remark`, `test_weekly_replay_mismatch_stops_entries`, `test_cli_help_lists_subcommands`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Pass; ruff; full `uv run pytest tests/forward tests/research -q`.
- [ ] **Step 5:** Commit `feat(forward): night and morning steps, daemon and CLI`.

### Task 13: Monthly report

**Files:**
- Create: `backend/app/forward/report.py`
- Test: `backend/tests/forward/test_report.py`

**Interfaces:**
- Consumes: Tasks 1, 9, 11; `app.research.metrics.summarize`, `trade_r`; `app.research.baselines.random_signals`, `buy_and_hold_curve`/`b0_bh`, `b0_spy`; `app.research.evaluate.gate_checks`.
- Produces: `build_report(data_dir, month: str, *, as_of: date) -> str` (markdown) and `write_report(...) -> Path` (`docs/forward/<yyyy-mm>.md`). Sections: header line `**INTERIM, not a verdict**` until the evaluation date (then `**VERDICT**`), registration sha256 and pins; per strategy engine book vs live book (trades, win rate, avg R, CAGR, Sharpe, MDD); research final-year numbers labelled "reference only"; baselines over the same forward window (forward-universe equal-weight buy-and-hold, random-entry percentile, SPY for information); execution (fill vs official open bps median/p90 per side, stop fill vs stop line); status counts from `days.jsonl` and an error line for any missing day; gate checks with `n_trials=71` and the registered `var_sr_trials`.
- [ ] **Step 1: Failing tests:** `test_interim_header_before_evaluation`, `test_missing_day_is_an_error_line`, `test_gate_uses_registered_n_trials`, `test_execution_bps`, `test_reference_numbers_labelled`.
- [ ] **Step 2-4:** FAIL → implement → pass; ruff.
- [ ] **Step 5:** Commit `feat(forward): monthly forward report`.

### Rollout (Claude and the user, after all tasks; spec section 10)

1. Claude: Task 7 Step 6 rehearsal for both leads → `differences: []`.
2. User: `uv run python scripts/forward/run.py status --check-account` (prints account type SIMULATE, cash, positions, open orders).
3. User-approved: `flatten --confirm` if anything is held (during regular hours).
4. Dry run: user starts `uv run python scripts/forward/run.py dry-run` in an Orca terminal for 10 trading days; on one day the user runs `status --test-order SYMBOL` to verify remark round trip and fill fields. Claude reviews the records daily: every morning decided, 0 `logic_mismatch`, measured bar-ready time and morning run time.
5. `register --capital <rule from spec Q2> --start-date <next trading day> --account-id <id>`; then `daemon`.
