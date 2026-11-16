# Market data storage (KlineRepository + kline_sync)

Scope: `backend/app/storage/repository.py`, `backend/app/storage/__init__.py`, `backend/app/services/kline_sync.py` (+ helpers).

## On-disk layout

- Root = `settings.data_dir` (env `DATA_DIR`, default project-root `data/`), created in `DataStore.__init__` (`repository.py:136-138`; `config.py:152,162-165`). Sub-dirs created up front (`repository.py:141-168`): `kline_daily`, `kline_daily_enriched`, `kline_index_daily`, `kline_index_enriched`, `kline_etf_daily`, `kline_etf_enriched`, `kline_minute`, `kline_etf_minute`, `adj_factor`, `adj_factor_etf`, `financials/*`, `instruments*`, `kline_ext`, `pools`, `backtest_results`, `screener_results`, `ai_cache`, `user_data`.
- DuckDB is in-memory; each dir = view over `read_parquet('<data_dir>/<dir>/**/*.parquet', union_by_name=true)`, incl. `adj_factor`, `adj_factor_etf` (`repository.py:170-197`); unified views `:233-306`.
- Partitioning: **one file per trading day for ALL symbols**, not per symbol — `kline_daily/date=YYYY-MM-DD/part.parquet` (`_write_daily_partition`, `repository.py:2101-2121`; same for `kline_daily_enriched`, `kline_index_*`, `kline_etf_*`). Minute: `kline_minute/date=YYYY-MM-DD/part.parquet` (and `kline_etf_minute/...`) via `_write_minute_partition` (`kline_sync.py:671-706`); legacy `symbol=` partitions migrate to `date=` (`kline_sync.py:1169-1228`, called `:1265`).
- Adj factors: one market-wide `adj_factor/all.parquet` (`adj_factor_etf/...`) plus `basis.json`, `rebuild_pending.json` (`kline_sync.py:402-410,428-455,457-486`).
- Daily streaming sync stages to hidden `<data_dir>/.daily_sync_staging/<uuid>/date=.../part-N.parquet` before commit; stale staging >24h swept (`kline_sync.py:344-397`).
- Read-side pruning: glob expanded only to `date=` dirs inside [start,end] (`_partition_source`, `repository.py:102-131`); globs `:363-371`.
- Enriched writes via `EnrichedPublication` (atomic replace + generation marker); markers `<data_dir>/.matrix_generation_{stock|etf}.json` + `.lock` (`enriched_generation.py:28-29,268-350`).

## Schemas (daily / minute / adj_factor)

- Raw daily (`kline_daily[/_etf/_index]`), canonical 8 cols (`CANONICAL_DAILY_COLS`, `kline_sync.py:53-55`; normalized `:61-100`): `symbol` Utf8, `date` Date, `open/high/low/close` Float64, `volume` Float64, `amount` Float64. Key (`symbol`,`date`), `unique(keep="last")` (`repository.py:2147-2155`). Prices **unadjusted** (Alpaca `adjustment="raw"`, `plugins/alpaca/provider.py:245-246`).
- Enriched daily (`kline_daily_enriched[/_index/_etf]`), exactly 14 stored cols (`ENRICHED_STORAGE_COLS`, `indicators/pipeline.py:87-95`): `symbol` Utf8, `date` Date, `open/high/low/close` Float64 (**forward-adjusted**), `volume` Float64 (split-adjusted via `vol_factor`), `amount` Float64 (unadjusted), `raw_close/raw_high/raw_low` Float64, `turnover_rate` Float64 (often null), `quote_ts` Int64 source epoch ms (`data_providers/normalizer.py:51-53`). Only these 14 written; indicators recomputed on read (`repository.py:2014-2021`; `indicators/pipeline.py:941-945`).
- Minute (`kline_minute[/_etf]`), canonical 8 cols (`CANONICAL_MINUTE_COLS`, `kline_sync.py:526-528`): `symbol` Utf8, `datetime` Datetime("us") naive **US-Eastern wall clock** (`:590-649`, ts→ET `:652-664`), `open/high/low/close/volume/amount` Float64. Key (`symbol`,`datetime`); regular-session rows only (`:549-571`).
- Adj factor (`adj_factor/all.parquet`) event schema (`_ADJ_EVENT_SCHEMA`, `kline_sync.py:402,413-426`): `symbol` Utf8, `trade_date` Date, `ex_factor` Float64 (pre/post ratio per event, finite >0), `vol_factor` Float64. Key (`symbol`,`trade_date`), `unique(keep="last")` (`:450-452`).

## Adjustment on read

- Daily: applied **at enriched build time, not on read**. `compute_enriched` copies raw OHLC to `raw_close/raw_high/raw_low`, then `_apply_adj_factor` rewrites `open/high/low/close` (+`volume` for splits) (`indicators/pipeline.py:746-778`). **Forward adjustment**: cumulative product of event `ex_factor` per symbol; `adjusted = raw * cum_at_D / total_cum` (asof backward join) (`pipeline.py:213-290`). Readers (`get_daily`, `_scan_daily_symbol`) only scan enriched parquet and recompute indicators (`repository.py:1387-1457,1744-1759`); no factor join on the read path.
- Minute: parquet stores **raw** prices once marker `kline_minute/.raw_basis` exists; read path applies a **forward** projection `ratio = cum_at_D / total_cum` to `open/high/low/close/amount` (volume unchanged) (`services/minute_adjust.py:1-13,145-170,113-142`; hook `_maybe_adjust_minute`, `repository.py:1556-1571`). Without the marker (un-migrated legacy partitions) data is returned as stored (old forward-adjusted). A factor table without `basis == "event_v2"` is treated as empty, ratio 1 (`minute_adjust.py:84-89`).

## kline_sync flow (step list with cites)

1. Pipeline reads `repo.latest_daily_date()`; start = explicit override, else `min(latest, stale_day)`, else today−365d (`jobs/daily_pipeline.py:230,275-341`).
2. `sync_and_persist_daily_batch` resolves bare symbols, then provider from `preferences.get_daily_data_provider()` (`kline_sync.py:209-262`).
3. Daily pull: Alpaca `iter_daily` streams 100-symbol chunks, each bars request paged `limit=10000`, `adjustment=raw`, `feed=sip` (end clamped to now−16min) (`plugins/alpaca/provider.py:34-37,108-149,253-262`); `get_daily` fallback `:240-251`. Vendor/TickFlow path retired, only records failure (`kline_sync.py:181-207`).
4. Persist: `_persist_daily_chunks` normalizes each chunk (`_normalize_daily`: rename ts_code/trade_date, cast OHLCV Float64, drop halt days), writes staging partitions per date, then `repo.append_daily` per date dir (`kline_sync.py:344-386`; `repository.py:2008-2012`), merge-upserting `date=/part.parquet` (`repository.py:2101-2121,2131-2179`).
5. Adj factor: `sync_adj_factor` calls `provider.get_adj_factors(...)` over `daily_range_start −7d`, or last 15 days when daily was incremental, or full history if basis is legacy (`kline_sync.py:488-519`; `daily_pipeline.py:405-447`). Alpaca pulls raw+all bars per 100-symbol chunk, derives event ratios via `services.adjustment_events.events_for_symbols` (`plugins/alpaca/provider.py:262-330`); merged by `store_adj_factor_events` (`kline_sync.py:428-455`). Failures raise and land in `stage_errors` (`daily_pipeline.py:443-447`); `mark_rebuild_pending` persists the pending enriched recompute for crash recovery (`kline_sync.py:457-486`; consumed `daily_pipeline.py:467-478`, cleared `:569-571`).
6. Enriched: `run_pipeline` recomputes full or incremental (new dates + affected symbols), writing the 14-col partitions (`indicators/pipeline.py:941-1030`; `daily_pipeline.py:505-566`).
7. Minute (optional): `sync_and_persist_minute` starts from `_latest_minute_datetime` (or backward extension / force-full), then `sync_minute_batch` → provider `get_minute` in symbol chunks, streamed per 20-trading-day segment into `_write_minute_partition` (`kline_sync.py:1229-1344,807-855,671-706`); `raw_basis` picks raw vs legacy forward-adjusted fetch (`:1240-1241,1326`).
8. Failure recording: 0-row symbols → `zero_row_out`, per-chunk pull failures → `failed_out` (`kline_sync.py:188-242`; callers `api/kline.py:1022,1038`); custom provider isolates a failed batch and continues other chunks (`data_providers/custom/provider.py:132-181`); Alpaca adj-factor failures propagate fail-closed (`plugins/alpaca/provider.py:308-327`); pipeline raises `PipelineStageError` if any stage error exists (`daily_pipeline.py:646-652`). Minute errors are only logged, degrading to empty/0 rows (`kline_sync.py:780-789,1318-1330`).

## data_generation

- = enriched matrix generation, a UUID in `<data_dir>/.matrix_generation_{asset_type}.json` (`enriched_generation.py:28-29`), read by `KlineRepository.get_matrix_data_generation()` (`repository.py:1954-1956`).
- Changes when a write publishes enriched data: `EnrichedPublication.commit` writes a new `uuid4().hex` ready payload only if something changed (`enriched_generation.py:315-350`; no-change commit returns `None`, `:309-314`). Also changes on marker (re)initialisation, on reader recovery of a dead-owner `publishing` claim (`:204-235`), and via `bump_enriched_generation` (`:255-266`).
- Purpose: cache-consistency token. The full-history in-memory cache stores the generation it was built from and is invalidated on mismatch (`repository.py:606-627,1188-1204`); backtest matrix cache keys include a source generation (`backtest/matrix.py:378,660-661,721`). While publishing, marker state is `publishing`, so readers fail closed (`enriched_generation.py:206-214`).

## Open questions

- `kline_daily` rows can carry `quote_ts` via the custom-provider `append_daily` path, bypassing `_normalize_daily` (`kline_sync.py:281-300`), so real parquet schema may be wider than the canonical 8 cols — NOT VERIFIED on disk.
- Whether a non-Alpaca custom source is currently configured (affects raw vs adjusted provider output, per-batch failure isolation) — NOT VERIFIED (preferences/config not read).
- Minute `amount` is ratio-scaled for ETF minute data too, but only stock/ETF factor tables were inspected; index minute adjustment short-circuits (`minute_adjust.py:156`), so `kline_etf_minute` behavior in practice is NOT VERIFIED.