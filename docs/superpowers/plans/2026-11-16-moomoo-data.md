# Moomoo Data Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this project, each task is dispatched as one Orca **pi** worker (Claude Code coordinates), so every task below is self-contained.

**Goal:** A built-in `moomoo` data plugin that serves ZenTrade's stock list, raw daily bars, adjustment events and realtime quotes from the user's OpenD, replacing Alpaca as the data source.

**Architecture:**
- A new `backend/app/plugins/moomoo/` package is auto-loaded by the existing plugin loader. It plugs into per-dataset routing, `kline_sync` staging, adj-factor events and enriched publication unchanged.
- One subscription-rotation pass reads raw and forward-adjusted (QFQ) daily bars. Raw bars are stored. Adjustment events come from the existing `events_for_symbols`, and are cached per day for `get_adj_factors`.
- `get_rehab` is the fallback and the oracle.

**Tech Stack:** Python 3, polars, moomoo SDK `moomoo-api==10.12.7208` (module `moomoo`), pytest. Frontend: React + TypeScript.

**Spec:** `docs/superpowers/specs/2026-11-16-moomoo-data-design.md` (approved 2026-11-16, option a).

## Global Constraints

- The OpenD address comes from env/secret `ZENTRADE_MOOMOO_OPEND`, default `127.0.0.1:11111`. No API key.
- Datasets declared: `daily`, `adj_factor`, `realtime`. No `minute`/`full_minute`.
- Stored daily bars are **raw** (`AuType.NONE`). Adjustment comes only from event rows `{symbol, trade_date, ex_factor, vol_factor}`.
- Rotation batch size = the free subscription quota from `query_subscription()` (never hard-code 100). Hold ≥ **61 s** after subscribe before unsubscribe.
- Pacing:
  - `get_market_snapshot` ≤ 60 calls / 30 s, ≤ 400 codes per call;
  - `get_rehab` ≤ 60 calls / 30 s;
  - `get_cur_kline` unpaced, but backs off on a rate-limit error.
- Universe: `exchange_type ∈ {US_NYSE, US_NASDAQ}`, code matches `^US\.[A-Z]+(\.[A-Z])?$`, `delisting` False.
- Naive datetimes are US/Eastern wall clock.
- The Alpaca plugin stays registered (step 4 removes it). Do not edit `alpaca_mirror.py`, `alpaca_options.py` or `api/trading.py`.
- **No git commits** unless the user asks (user rule). Each task ends with a `git diff --stat` checkpoint instead.
- Work on branch `feat/moomoo-data`, created from `main`.
- No test may require a running OpenD except the opt-in live test (`ZENTRADE_MOOMOO_LIVE=1`).

## Review Focus

1. **A symbol that returns no bars one evening** (halt, OpenD hiccup) must get that day on a later run. `iter_daily` re-emits the last 14 calendar days for every symbol (idempotent upsert) → Task 5 test `test_iter_daily_reemits_repair_window`.
2. **OpenD down or not logged in** at startup or mid-run. The plugin stays registered, the sync records the batch's symbols as failed, and other batches continue → Task 4 test `test_rotation_batch_error_marks_failed_and_continues`, Task 1 test `test_availability_true_without_opend`.
3. **Subscription quota partly used by another app** (e.g. moomoo desktop holds 30). Batches shrink to the free quota; zero free → clear error, no spin → Task 4 tests `test_batch_size_follows_free_quota`, `test_zero_free_quota_raises`.
4. **Rehab rows with additive factor B ≠ 0, or a split row missing `split_ratio`.** That symbol fails closed and others are unaffected → Task 6 tests `test_rehab_b_nonzero_rejected`, `test_rehab_split_without_ratio_rejected`.
5. **Realtime polled faster than moomoo allows.** `quote_service` must not poll a moomoo-routed source faster than 15 s, even if the user's preference says 6 s → Task 7 test `test_quote_interval_floor_for_provider`.

---

## File map

| File | Responsibility |
|---|---|
| Create `backend/app/plugins/moomoo/__init__.py` | empty |
| Create `backend/app/plugins/moomoo/plugin.yaml` | manifest |
| Create `backend/app/plugins/moomoo/provider.py` | `MoomooProvider`, `availability`, symbol mapping, address, context factory |
| Create `backend/app/plugins/moomoo/pacer.py` | sliding-window call pacer |
| Create `backend/app/plugins/moomoo/rotation.py` | subscription rotation reading raw + QFQ daily bars |
| Create `backend/app/plugins/moomoo/rehab.py` | `get_rehab` rows → event rows |
| Create `backend/app/plugins/moomoo/events_cache.py` | per-day adjustment-event cache on disk |
| Modify `backend/app/credentials.py:26-31` | add `MOOMOO_OPEND` spec |
| Modify `backend/app/services/setup_checklist.py:107-114` | `moomoo` item; Alpaca not required when moomoo routes daily |
| Modify `backend/app/services/quote_service.py` (interval resolution near `:183-187`) | provider interval floor |
| Modify `backend/pyproject.toml`, `backend/uv.lock` | add `moomoo-api==10.12.7208` |
| Modify `frontend/src/pages/settings/DataSources.tsx` (`:101-106`, `:551`, `:567-575`, detail switch near `:1049`) | `MoomooDetail` card; unset fallback "未配置" |
| Create `backend/tests/moomoo_fakes.py` | `FakeQuoteContext`, `FakeClock` |
| Create `backend/tests/test_moomoo_*.py` | one file per task |

---

### Task 0: Branch and test environment (coordinator, not a pi worker)

- [ ] Commit or stash step 1 on `feat/llm-presets` (needs the user's OK), then `git switch main && git switch -c feat/moomoo-data`.
- [ ] `cd backend && uv sync` (installs the backend venv; the machine is low on memory, so run it while nothing else is running).
- [ ] Baseline: `uv run pytest tests/test_alpaca_provider.py tests/test_adjustment_events.py tests/test_adjustment_failure_contract.py -q`. Expected: all pass. Record the count.

### Task 1: Plugin skeleton, address, availability, symbol mapping, dependency

**Files:**
- Create: `backend/app/plugins/moomoo/{__init__.py,plugin.yaml,provider.py}`, `backend/tests/moomoo_fakes.py`.
- Modify: `backend/app/credentials.py`, `backend/pyproject.toml`, `backend/uv.lock`.
- Test: `backend/tests/test_moomoo_provider_basics.py`.

**Interfaces — Produces:**
- `credentials.MOOMOO_OPEND = SecretSpec("ZENTRADE_MOOMOO_OPEND", "moomoo_opend")`.
- `provider.opend_address() -> tuple[str, int]`: parses `host:port`. Empty → `("127.0.0.1", 11111)`. Malformed → `ValueError`.
- `provider.to_moomoo(symbol: str) -> str` (`"BRK.B"` → `"US.BRK.B"`); `provider.from_moomoo(code: str) -> str` (inverse; raises `ValueError` if no `US.` prefix).
- `provider.availability() -> tuple[bool, str]`: True iff `import moomoo` works and `opend_address()` parses. **Does not connect.**
- `provider.set_ctx_factory(factory: Callable[[str, int], Any]) -> None` and `provider.open_ctx()`: a context manager yielding a quote context from the factory (default `moomoo.OpenQuoteContext(host=, port=)`), always closing it.
- `class MoomooProvider`:
  - `name = "moomoo"`;
  - `capabilities = ProviderCapabilities(instruments=True, daily=True, adj_factor=True, minute=False, realtime=True, financial=False)`;
  - `min_realtime_interval_seconds = 15.0`;
  - a `.config` shim shaped like `alpaca/provider.py:173-177` with datasets `daily` (rpm 120, batch 100), `adj_factor` (rpm 120, batch 100), `realtime` (rpm 120, batch 400);
  - `close()`;
  - `opend_status() -> dict` with keys `ok: bool, qot_logined: bool, market_us: str | None, error: str | None`.
- `plugin.yaml`: `name: moomoo`, `runtime: none`, `entry: app.plugins.moomoo.provider:MoomooProvider`, `check: app.plugins.moomoo.provider:availability`, `datasets: [daily, adj_factor, realtime]`. Copy the remaining keys from `plugins/alpaca/plugin.yaml` but leave out `api_key_env`.
- `tests/moomoo_fakes.py`:
  - `FakeQuoteContext`, with configurable returns for `get_global_state`, `query_subscription`, `subscribe`, `unsubscribe`, `get_cur_kline`, `get_stock_basicinfo`, `get_rehab`, `get_market_snapshot`. It records calls with timestamps from a `FakeClock`.
  - `FakeClock`, with `monotonic()` and `sleep(s)` advancing time.

- [ ] **Step 1: Write failing tests** in `test_moomoo_provider_basics.py`:
  - `test_symbol_roundtrip`: `to_moomoo("BRK.B") == "US.BRK.B"`, `from_moomoo("US.AAPL") == "AAPL"`, and `from_moomoo("HK.00700")` raises `ValueError`.
  - `test_opend_address_default_and_env(monkeypatch)`: env unset → `("127.0.0.1", 11111)`; env `"10.0.0.5:22222"` → `("10.0.0.5", 22222)`; `"bad"` → `ValueError`.
  - `test_availability_true_without_opend(monkeypatch)`: env unset, no server → `(True, ...)`.
  - `test_opend_status_reports_login(monkeypatch)`: fake `get_global_state` returns `{"qot_logined": True, "market_us": "AFTER_HOURS_END"}` → `ok` and `qot_logined` True. A factory raising `ConnectionError` → `ok` False, `error` non-empty.
  - `test_plugin_registered()`: after `from app.data_providers import custom as cs`, `cs.provider_has_dataset("moomoo", "daily")` is True and `"minute"` is False.
- [ ] **Step 2:** `uv run pytest tests/test_moomoo_provider_basics.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement the interfaces above. Add `"moomoo-api==10.12.7208"` to `[project].dependencies` and run `uv lock`.
- [ ] **Step 4:** Rerun. Expected: PASS. Also rerun the Task 0 baseline: same count passing.
- [ ] **Step 5:** Checkpoint `git diff --stat`.

### Task 2: Pacer

**Files:** Create `backend/app/plugins/moomoo/pacer.py`. Test: `backend/tests/test_moomoo_pacer.py`.

**Produces:** `class Pacer(max_calls: int, window_s: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)` with `wait() -> None`. It blocks so that no more than `max_calls` `wait()` returns fall in any `window_s` interval, and is thread-safe.

- [ ] **Step 1:** Write `test_pacer_allows_burst_then_waits` (FakeClock):
  - 60 `wait()` calls at t = 0 do not sleep;
  - the 61st sleeps until t ≥ 30.0;
  - after that, 60 calls lie within any 30 s window.
- [ ] **Step 2:** Run it; it fails.
- [ ] **Step 3:** Implement with a deque of timestamps.
- [ ] **Step 4:** Run it; it passes.
- [ ] **Step 5:** Checkpoint `git diff --stat`.

### Task 3: `get_instruments`

**Files:** Modify `provider.py`. Test: `backend/tests/test_moomoo_instruments.py`.

**Consumes:** `open_ctx`, `from_moomoo` (Task 1).
**Produces:** `MoomooProvider.get_instruments(asset_type: str = "stock") -> list[dict]`. Each dict has `symbol, name, code, exchange ("NYSE"|"NASDAQ"), asset_type "stock", source "moomoo", region "US", listing_date (date | None)`. It returns `[]` for `asset_type != "stock"`.

- [ ] **Step 1:** Write `test_instruments_filter`. The fake `get_stock_basicinfo` returns rows:
  - `US.AAPL` NASDAQ;
  - `US.BRK.B` NYSE;
  - `US.1061` NASDAQ;
  - `US.2618996D` NASDAQ;
  - `US.XYZ` PINK;
  - `US.OLD` NYSE with `delisting` True;
  - `US.NEW` NASDAQ with `listing_date` "1970-01-01".

  Expected symbols: `["AAPL", "BRK.B", "NEW"]`. `NEW.listing_date is None`. `exchange` for BRK.B == "NYSE".
- [ ] **Step 2:** Run it; it fails.
- [ ] **Step 3:** Implement. Also log the distinct `suspension` values seen at INFO (the spec leaves suspended-name filtering to the builder: drop rows whose `suspension` is not `"N/A"` only if the live check in Task 9 shows that field marks suspended names).
- [ ] **Step 4:** Run it; it passes.
- [ ] **Step 5:** Checkpoint `git diff --stat`.

### Task 4: Subscription rotation

**Files:** Create `backend/app/plugins/moomoo/rotation.py`. Test: `backend/tests/test_moomoo_rotation.py`.

**Produces:**
- `@dataclass BatchResult: bars: dict[str, tuple[pl.DataFrame, pl.DataFrame]]` (code → (raw, qfq)); `failed: list[str]`.
- Each frame has columns `time_key (str), open, high, low, close, volume, turnover` exactly as returned by `get_cur_kline`.
- `rotate_daily(ctx, codes: list[str], *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, hold_s: float = 61.0) -> Iterator[BatchResult]`:
  - batch size = `ctx.query_subscription()[1]["remain"]`, re-read before each batch;
  - if 0 → `RuntimeError("moomoo subscription quota exhausted")`;
  - per batch: `subscribe(batch, [SubType.K_DAY], subscribe_push=False)`;
  - per code: `get_cur_kline(code, 1000, KLType.K_DAY, AuType.NONE)` then the same with `AuType.QFQ`;
  - sleep until `hold_s` has passed since subscribe; `unsubscribe(batch, [SubType.K_DAY])`; yield.
  - A `RET_ERROR` on subscribe → the whole batch goes to `failed`. A `RET_ERROR` on one code → only that code goes to `failed`.
  - A ret message containing "frequen" (rate limit) → sleep 5 s, retry that code once.

- [ ] **Step 1: Write tests (FakeClock + FakeQuoteContext):**
  - `test_batch_size_follows_free_quota`: remain = 70, 150 codes → batches of 70, 70, 10. (The fake reports remain = 70 each time.)
  - `test_zero_free_quota_raises`.
  - `test_hold_at_least_61s`: for each batch, unsubscribe time − subscribe time ≥ 61.0.
  - `test_rotation_batch_error_marks_failed_and_continues`: subscribe fails on batch 1 → batch 1 codes in `failed`; batch 2 has bars.
  - `test_raw_and_qfq_both_fetched`: each code yields two frames, and the fake saw the `AuType.NONE` and `AuType.QFQ` calls.
- [ ] **Step 2:** Run them; they fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them; they pass.
- [ ] **Step 5:** Checkpoint `git diff --stat`.

### Task 5: `iter_daily` / `get_daily` + event cache write

**Files:** Create `backend/app/plugins/moomoo/events_cache.py`. Modify `provider.py`. Test: `backend/tests/test_moomoo_daily.py`.

**Consumes:** `rotate_daily`, `BatchResult` (Task 4); `services.adjustment_events.events_for_symbols(raw, adjusted, *, skip_invalid_symbols)`, which takes iterables of `(symbol, {"t": iso_date_str, "c": close, "v": volume})` and returns dicts `{symbol, trade_date: date, ex_factor, vol_factor}`.

**Produces:**
- `events_cache.write(data_dir: Path, day: date, events: list[dict], covered: set[str]) -> None`. Merges into `<data_dir>/moomoo/adj_events_<day>.parquet` (schema = `kline_sync._ADJ_EVENT_SCHEMA`) and `<data_dir>/moomoo/adj_covered_<day>.json`.
- `events_cache.read(data_dir: Path, day: date) -> tuple[pl.DataFrame, set[str]] | None`.
- `MoomooProvider.iter_daily(symbols, start_time=None, end_time=None, asset_type="stock", on_chunk_done=None) -> Iterator[pl.DataFrame]`. One frame per batch, in the canonical 8 columns `symbol Utf8, date Date, open, high, low, close, volume, amount Float64` (`amount = turnover`). Rows are within `[start − 14 calendar days, end]`, with the date taken from `time_key[:10]`. `on_chunk_done(cur, total)`. Failed codes are logged and simply absent (kline_sync reports them as zero-row).
- `MoomooProvider.get_daily(...) -> pl.DataFrame`: concatenation of `iter_daily`.
- `REPAIR_WINDOW_DAYS = 14` (module constant).
- Side effect per batch: `events_for_symbols(raw, qfq, skip_invalid_symbols=True)` over the full 1,000 bars, then `events_cache.write(settings.data_dir, today_et, events, covered=codes_with_bars)`.

- [ ] **Step 1: Write tests:**
  - `test_iter_daily_canonical_columns`: schema equals the 8 columns and dtypes above; `amount` equals the fake turnover.
  - `test_iter_daily_reemits_repair_window`: the fake has bars 2026-10-01 … 2026-11-13. With `start=2026-11-13` the output's min date is 2026-10-30 (13 Nov − 14 days).
  - `test_iter_daily_writes_event_cache(tmp_path)`. The fake raw closes are 100 on day 1 and 10.1 on day 2; the QFQ closes are 10.0 and 10.1. Then `events_cache.read` returns one event with `ex_factor` ≈ 10.0 and covered contains the symbol.
  - `test_failed_codes_absent_not_raised`.
- [ ] **Step 2:** Run them; they fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them; they pass. Also run `uv run pytest tests/test_kline_sync_bare_symbols.py tests/test_kline_sync_timezone.py -q` and check it still passes.
- [ ] **Step 5:** Checkpoint `git diff --stat`.

### Task 6: Rehab mapping + `get_adj_factors`

**Files:** Create `backend/app/plugins/moomoo/rehab.py`. Modify `provider.py`. Test: `backend/tests/test_moomoo_adj.py`.

**Consumes:** `events_cache.read` (Task 5), `Pacer` (Task 2).

**Produces:**
- `rehab.rehab_events(symbol: str, rows: list[dict]) -> list[dict]`:
  - one event per distinct `ex_div_date`: `ex_factor = Π 1/forward_adj_factorA`; `vol_factor = Π 1/split_ratio` over rows where `split_ratio` is set (else 1.0);
  - raises `ValueError` if any `forward_adj_factorB` ≠ 0, or if any row has `split_base` set but `split_ratio` missing.
- `MoomooProvider.get_adj_factors(symbols, start_time=None, end_time=None, asset_type="stock", on_chunk_done=None, anchor_dates=None) -> pl.DataFrame`:
  - schema exactly `kline_sync._ADJ_EVENT_SCHEMA`, sorted by symbol and trade_date, filtered to `[start, end]`;
  - symbols covered by today's cache come from the cache;
  - the rest come from `get_rehab` (paced 60/30 s) → `rehab_events`;
  - a `ValueError` for one symbol is logged and that symbol skipped (`skip_invalid_symbols` semantics).

- [ ] **Step 1: Write tests:**
  - `test_rehab_split_nvda`: a row `{ex_div_date "2024-06-10", split_base 1, split_ert 10, split_ratio 0.1, forward_adj_factorA 0.1, forward_adj_factorB 0}` gives `ex_factor == 10.0` and `vol_factor == 10.0`.
  - `test_rehab_dividend_aapl`: `{ex_div_date "2026-11-12", per_cash_div 0.27, split_ratio NaN, forward_adj_factorA 0.99924, B 0}` gives `ex_factor ≈ 1/0.99924` and `vol_factor == 1.0`.
  - `test_rehab_same_date_rows_multiply`.
  - `test_rehab_b_nonzero_rejected`.
  - `test_rehab_split_without_ratio_rejected`.
  - `test_adj_uses_cache_then_rehab(tmp_path)`: the cache covers AAPL; NVDA is uncovered. Assert the fake `get_rehab` was called only for `US.NVDA`.
  - `test_adj_bad_symbol_skipped_others_kept`.
- [ ] **Step 2:** Run them; they fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them; they pass. Also run `uv run pytest tests/test_adjustment_failure_contract.py tests/test_adjustment_events.py -q`; same pass count as the baseline.
- [ ] **Step 5:** Checkpoint `git diff --stat`.

### Task 7: Realtime + poll-interval floor

**Files:** Modify `provider.py`, `backend/app/services/quote_service.py`. Test: `backend/tests/test_moomoo_realtime.py`.

**Consumes:** `Pacer`, `get_instruments` (for the universe; cache the symbol list for 1 h).

**Produces:**
- `MoomooProvider.get_realtime() -> list[dict]`:
  - snapshots in chunks of 400 (paced 60/30 s);
  - keys as in `alpaca/provider.py:399-431`: `last_price ← last_price`, `prev_close ← prev_close_price`, `open/high/low ← open_price/high_price/low_price`, `volume`, `amount ← turnover`;
  - `quote_ts = volume_ts = epoch ms of update_time` read as ET;
  - **no `feed` key**, so the legacy contract keeps volume (`quote_contract.py:35-40`);
  - `data_delay` = seconds between now and `update_time`.
- `quote_service`: the effective interval = `max(configured, CUSTOM_PROVIDER_MIN_INTERVAL, getattr(provider, "min_realtime_interval_seconds", 0))`.
- Note (corrects the spec): `quote_service` writes intraday quotes into today's daily partition (`quote_service.py:612`). The post-close rotation then overwrites that day via upsert, so no change is needed. The design doc line "Realtime never writes stored daily bars" is wrong and is fixed in Task 9.

- [ ] **Step 1: Write tests:**
  - `test_realtime_maps_snapshot`: one snapshot row maps to the expected dict; there is no `feed` key; `quote_ts` equals the ET epoch ms of "2026-11-13 16:00:00".
  - `test_realtime_chunks_of_400`: 900 symbols → 3 calls.
  - `test_quote_interval_floor_for_provider`: provider attr 15, preference 6 → effective 15.
- [ ] **Step 2:** Run them; they fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them; they pass.
- [ ] **Step 5:** Checkpoint `git diff --stat`.

### Task 8: Setup checklist + Data Sources card

**Files:** Modify `backend/app/services/setup_checklist.py`, `frontend/src/pages/settings/DataSources.tsx`. Test: `backend/tests/test_moomoo_checklist.py`.

**Consumes:** `MoomooProvider.opend_status()` (Task 1), `credentials.MOOMOO_OPEND`.

**Produces:**
- Checklist item:
  - id `moomoo`, group `market`, label "moomoo OpenD（美股行情）";
  - level `required` when the daily provider is `moomoo` or unset, else `optional`;
  - configured = `opend_status()["ok"] and qot_logined`;
  - env `["ZENTRADE_MOOMOO_OPEND"]`;
  - where `settings?tab=data-sources&source=moomoo&highlight=source-detail`.
- The Alpaca item's level becomes `optional` when `preferences.get_daily_data_provider() == "moomoo"`.
- Frontend:
  - a `MoomooDetail` read-only card showing: OpenD address (from the API's source detail), login status, and the hint "在 .env 设置 ZENTRADE_MOOMOO_OPEND=host:port";
  - the unset-preference fallback `'alpaca'` at `DataSources.tsx:101-106, 551, 567-575` becomes empty, displayed as "未配置".

- [ ] **Step 1:** Write `test_checklist_moomoo_required_when_routed` (monkeypatch preferences and `opend_status`) and `test_checklist_alpaca_optional_when_moomoo_routed`.
- [ ] **Step 2:** Run them; they fail.
- [ ] **Step 3:** Implement the backend, then the frontend card.
- [ ] **Step 4:** Run them; they pass. Then run `cd frontend && pnpm typecheck` (needs `pnpm install`; ask the user first because of memory). Expected: no errors.
- [ ] **Step 5:** Checkpoint `git diff --stat`.

### Task 9: Live verification + pipeline dry run + doc fix (coordinator-supervised)

**Files:** Create `backend/tests/test_moomoo_live.py`. Modify the spec (realtime line).

- [ ] **Step 1:** Write `test_moomoo_live.py`, skipped unless `ZENTRADE_MOOMOO_LIVE=1`:
  - `test_live_events_reproduce_qfq`: for AAPL, NVDA, BRK.B and KO, forward-adjust raw closes with the derived events (`adjusted = raw × cum_at_D / total_cum`, as `indicators/pipeline.py:213-290`). This must equal moomoo QFQ closes within 1e-6 relative on all returned days.
  - `test_live_rehab_matches_derived`: rehab events equal the raw/QFQ-derived events on overlapping dates (ex_factor within 1e-4).
  - `test_live_suspension_values`: print the distinct `suspension` values (informs the Task 3 filter).
- [ ] **Step 2:** `ZENTRADE_MOOMOO_LIVE=1 uv run pytest tests/test_moomoo_live.py -q -s`. Expected: PASS. Paste the output in the report.
- [ ] **Step 3: Pipeline dry run** on `DATA_DIR=<tmp>`:
  - route daily, adj_factor and realtime to `moomoo`;
  - create a watchlist with 20 symbols;
  - set an env var for a temporary instruments override with the same 20, or run `sync_and_persist_daily_batch` + `sync_adj_factor` + `run_pipeline` directly on those 20.

  Expected:
  - `kline_daily/date=*` partitions exist covering about 1,000 days;
  - `adj_factor/all.parquet` has NVDA 2024-06-10 `ex_factor` ≈ 10;
  - `kline_daily_enriched` NVDA 2024-06-07 close ≈ 120.544 (moomoo's own QFQ value for that day, which
    includes later dividends; verified in the 2026-11-16 probe);
  - `.matrix_generation_stock.json` changed.
- [ ] **Step 4:** In the spec, replace the line "Realtime never writes stored daily bars." with "Realtime intraday writes to today's partition (existing quote_service behaviour) are overwritten by the post-close rotation."
- [ ] **Step 5:** Full backend suite: `uv run pytest -q`. Expected: same failures as the Task 0 baseline or fewer. Report the counts.

---

## Self-review notes

- **Spec coverage:**

  | Spec item | Task |
  |---|---|
  | instruments | 3 |
  | rotation / daily | 4, 5 |
  | QFQ events + cache | 5 |
  | rehab fallback | 6 |
  | hole repair | 5 (14-day window) |
  | realtime + 15 s floor | 7 |
  | availability lifecycle | 1 |
  | time zone | 5, 7 |
  | address | 1 |
  | checklist / card | 8 |
  | live oracle + dry run | 9 |
  | pacing | 2, 6, 7 |

  The minute non-goal is honoured by the manifest datasets (Task 1).
- **Spec deviation, flagged:** the spec's "Realtime never writes stored daily bars" is contradicted by `quote_service.py:612`. It's handled in Task 7 and Task 9 step 4.
- **Hole-repair mechanism chosen** (the spec left it to the builder): a 14-calendar-day re-emit window in `iter_daily`. It costs no extra OpenD calls, because 1,000 bars are read anyway. Older holes stay with the existing integrity scan (`daily_pipeline.py:254`).
