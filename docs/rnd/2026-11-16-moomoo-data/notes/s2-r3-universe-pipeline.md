# Universe + daily pipeline

Scope: `backend/app/jobs/daily_pipeline.py`, `backend/app/services/instrument_sync.py`,
`backend/app/services/kline_sync.py`, `backend/app/services/index_const.py`,
`backend/app/services/preferences.py`, `backend/app/indicators/pipeline.py`.

## _resolve_universe
- Def: `daily_pipeline.py:142` `_resolve_universe(capset, repo=None) -> list[str]` (line 142-175).
- Inputs: `capset` is accepted but NOT used in this function (no reference in body). `repo` optional.
- Steps:
  1. Watchlist symbols: `watchlist_svc.list_symbols()`, union of `symbol` (lines 155-158).
  2. Read `data_dir/instruments/instruments.parquet`, `pl.read_parquet(..., columns=["symbol"])`,
     union symbols (lines 159-166). Missing file is soft (logged, not fatal).
  3. Union `CORE_INDEX_SYMBOLS` (SPY/QQQ/DIA/IWM) explicitly — NYSE Arca ETFs absent from
     stock `instruments`, needed for SPY<MA200 market gate (lines 168-170).
  4. If `repo` given: `base -= (repo.get_index_symbol_set() - set(CORE_INDEX_SYMBOLS))`
     — drops index symbols from watchlist fallback to avoid polluting `kline_daily`/`kline_minute`
     (index daily K lives in `kline_index_*`). ETFs intentionally kept (lines 171-174).
- Output-size logic: none explicit — returns `sorted(base)` (line 175); size = dedup union of the
  four sources. Docstring notes vendor `pools` (CN_Equity_A) retired (P1-1b2); full-market pool is
  deferred to Alpaca provider / opportunity-engine universe (P2) (lines 143-149).
- Callers: `run_now` Step 0 (~line 221); minute K uses same universe via
  `_resolve_minute_symbols` → `_resolve_universe` (lines 805-807).

## instruments.parquet (writer, source, columns, refresh)
- Writer: `instrument_sync.py:76` `sync_instruments(data_dir)` → writes
  `data_dir/instruments/instruments.parquet`, atomic_write_parquet (lines 112-118).
  Called by `daily_pipeline.run_instruments_sync` (line 181) and `run_now` Step 0 (line 214).
- Source/provider: `_fetch_instruments_via_provider` (lines 48-71) — uses
  `preferences.get_daily_data_provider()`; only if it is a custom provider exposing
  `get_instruments("stock")`. Vendor direct connect retired; returns None → 0 rows, table kept
  (lines 72-81). So the维表 follows the daily-K data source (coupled, no separate preference).
- Enrichment: if any row `source=="alpaca"` or `region=="US"`, merge Nasdaq US reference via
  `us_reference.enrich_instruments/load_us_reference`; prior sector/industry kept with original
  dates if new is blank (lines 83-100).
- Columns: `symbol,name,code,exchange,region,type` + `_METADATA_FIELDS` (sector, industry,
  sector_source, industry_source, sector_as_of, industry_as_of, source, asset_type,
  instrument_type, classification_source, risk_flags, is_etf, is_preferred, is_warrant, is_unit,
  reference_status, reference_as_of) + `listing_date,total_shares,float_shares,tick_size`
  (lines 17-20, 24-45). Plus `as_of = us_clock.today()` column (lines 109-110).
- Refresh schedule: pre-market cron `pre_market_instruments`, default 09:10 ET, Mon-Fri
  (`daily_pipeline.py:1119-1129`; default in `preferences.py:379-382`).

## CORE_INDEX_SYMBOLS
- Value: `index_const.py:16` `CORE_INDEX_SYMBOLS = tuple(CORE_INDEX_NAMES.keys())` =
  `("SPY","QQQ","DIA","IWM")` (`CORE_INDEX_NAMES` at lines 10-15: 标普500/纳斯达克100/
  道琼斯工业/罗素2000).
- Used by: `daily_pipeline.py:168-173` (universe union + index filter);
  `api/overview.py:219,226,248,273`; `plugins/tickflow/provider.py:176,187`;
  `services/data_integrity.py:141,146`; `services/index_sync.py:42`;
  `services/kline_sync.py:141-142`; `services/market_overview_builder.py:83,89`.

## Pipeline data steps (ordered table: step | function | dataset | cite)
| step | function | dataset | cite |
|---|---|---|---|
| 0 sync_instruments | `instrument_sync.sync_instruments` (+`_refresh_instruments_view`) | `instruments/instruments.parquet` | daily_pipeline.py:213-218, instrument_sync.py:76 |
| 0 resolve_universe | `_resolve_universe` | (instruments + watchlist + CORE_INDEX) | daily_pipeline.py:220-222 |
| 1 sync_daily | `kline_sync.sync_and_persist_daily_batch` (3 branches: override range / gap-fill / first 365d) | `kline_daily/date=*/` (view `kline_daily`) | daily_pipeline.py:273-343; kline_sync.py:209, 350, 297-300 |
| 1.5 sync_adj | `kline_sync.sync_adj_factor` → `store_adj_factor_events` | `adj_factor/all.parquet` | daily_pipeline.py:398-461; kline_sync.py:488, 514 |
| 2 compute_enriched | `indicators.pipeline.run_pipeline` (full / new_dates_only / symbols) | `kline_daily_enriched/date=*/part.parquet` | daily_pipeline.py:478-565; pipeline.py:939-943, 971, 1056 |
| 2.5 sync_minute (optional) | `kline_sync.sync_and_persist_minute` | `kline_minute/date=*/` | daily_pipeline.py:583-604; kline_sync.py:1229, 1309 |
| 3 refresh_views | `_refresh_views` | DuckDB views | daily_pipeline.py:616 |
| 3 zen_trade | `_run_zen_post_close` | paper ledger | daily_pipeline.py:622-629 |
| (post) | `warn_if_enriched_too_thin` | enriched data sufficiency | daily_pipeline.py:575-578 |

- Realtime: the vendor realtime-overwrite branch is retired (`elif False:` at
  daily_pipeline.py:305-306, P1-1b2); realtime quotes instead flow through `quote_service`
  (paused around the pipeline: `_pipeline_then_refresh` uses `qs.paused()`, lines 1136-1148).
- Step 2.3 sync_index is deleted (P1-5); SPY/QQQ etc. pulled in the stock daily batch,
  `skipped.append("sync_index")` (lines 579-581).
- Minute is gated on `preferences.get_minute_sync_enabled()` + `Cap.KLINE_MINUTE_BATCH`
  (lines 584-588), else appended to `skipped`, no emit.

## Schedule
- Framework: APScheduler `AsyncIOScheduler` (tz `America/New_York`), not system cron.
  Imports `daily_pipeline.py:21-23`; `start_scheduler` at lines 1099-1102.
- `pre_market_instruments`: CronTrigger day_of_week mon-fri, hour/minute from
  `preferences.get_instruments_schedule()` default 09:10 (lines 1119-1129;
  preferences.py:379-382).
- `daily_pipeline`: CronTrigger mon-fri, from `preferences.get_pipeline_schedule()`
  default `{"hour":16,"minute":35}` (lines 1164-1171; preferences.py:359-366). Note module
  docstring still says 15:35 (lines 4-6) — stale vs default 16:35.
- `daily_pipeline_catchup`: one-shot `run_date` now+120s on boot (lines 1193-1194).
- `daily_pipeline_retry`: `IntervalTrigger(minutes=15)` (lines 1217-1218).
- API surfaces next runs via `_next_cron_run(scheduler, "pre_market_instruments"/"daily_pipeline")`
  (`api/data.py:547-553, 620-621`); job IDs also reference `zen_paper_tick` etc.
- Grep `apscheduler` also hits `api/settings.py:1686-1714` (reschedule via CronTrigger).

## Open questions
- Watchlist source file/store for `watchlist_svc.list_symbols()` NOT VERIFIED (only its use here).
- Exact provider class/name backing `get_instruments("stock")` for a given install depends on the
  user's `daily_data_provider` preference; no default constant verified.
- Whether the pipeline date-set (`override_start_date`) path is reachable via API was not traced.
