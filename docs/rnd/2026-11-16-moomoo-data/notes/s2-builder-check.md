# S2 builder check — questions before planning

Ranked; each answer changes the plan.

1. **How are per-symbol missed days repaired?** The doc says rejected snapshot rows are "picked up next run". The pipeline starts each run from global `repo.latest_daily_date()` (`daily_pipeline.py:230,305`; `repository.py:1892-1903`), not per symbol, and zero-row symbols are only logged (`daily_pipeline.py:297-311`; `kline_sync.py:217-243`). Once any symbol writes today, a rejected/halted symbol's missing day is outside every later range. Decide: immediate subscription fallback for rejected symbols, per-symbol repair job, or extend integrity scan. Otherwise holes are permanent.

2. **What universe and cadence does moomoo realtime serve?** `quote_service` calls the no-arg provider full-market every 6s by default (`quote_service.py:625`; `preferences.py:98`; `quote_service.py:185-187`). 6,857 symbols / 400 = 18 `get_market_snapshot` calls per poll = 90 calls/30s, over the 60/30s limit. Full-market needs ≥10s polling plus caching; a hot-pool choice changes coverage, today-partition completeness, and final-snapshot confirmation. The doc's "configured realtime symbols" does not exist.

3. **Where does the 25% split-suspect prefilter live?** The provider receives only symbols, start/end, `asset_type`, `anchor_dates` (`daily_pipeline.py:437-440`; `kline_sync.py:488-519`), not stored previous closes. Who computes suspects and how are they passed? Otherwise post-close either queries all 6,857 (57 min) or misses splits.

4. **Sweep timing vs enriched rebuild.** The 20:15 sweep calls `mark_rebuild_pending`, but only the daily pipeline consumes pending (`daily_pipeline.py:466-478,571`); there is no morning enriched job. So adjusted prices land at the next 16:35 pipeline, not "next morning". Move the sweep before the pipeline, or add an enriched rebuild after it?

5. **Exact snapshot semantics for halted / after-hours / early-close days.** Is `update_time` the last quote time or retrieval time, and in ET? Is the main OHLCV the regular-session close at 16:35? The `≥16:00 ET` guard rejects every row on a 13:00 early close; if `update_time` advances after hours it accepts after-hours prices; if a halted symbol keeps today's `update_time` with stale OHLCV it becomes a fake flat bar. Need the real contract.

6. **`get_rehab` requirements, history depth and factor semantics.** Does it require an active/K_DAY subscription per symbol? If yes, a 6,857-symbol sweep under the 100 quota cannot run in 57 min. Does it return full lifetime events or a recent window? Are `forward_adj_factorA/B` per-event or cumulative? A short window or cumulative A breaks the 4-year adjusted series even if today's QFQ matches.

7. **Rate limits for `get_cur_kline`, `subscribe`, `get_stock_basicinfo`.** Only rehab and snapshot limits are documented. The 69-batch/70-minute estimate assumes per-symbol K-line calls need no wait; if the limit is 60/30s, a 100-symbol batch is ~50s and the 61s hold dominates; if lower, the backfill is hours.

8. **Realtime field contract (`feed`, `quote_ts`).** If moomoo sets `feed`/`volume_feed` to a non-consolidated value, `normalize_record` nulls volume (`quote_contract.py:35-76`); with `feed=None` the legacy path keeps it. Which values should be emitted, and what maps snapshot `update_time` to `quote_ts`/`volume_ts`? Wrong choice silently drops or trusts realtime volume.

9. **Availability lifecycle.** `availability()` runs only during plugin load (`loader.py:544-605,638`). If OpenD is down at startup the plugin is not registered, while `provider_has_dataset` still reports its datasets (`loader.py:327-340`) and `get_provider` raises (`loader.py:305-309`). Is restart/reload acceptable, or must availability be re-probed per sync so a late-started OpenD recovers?

10. **How is a split row distinguished from a dividend row?** The rule `vol_factor = 1/split_ratio` for "split or reverse-split rows" needs an unambiguous discriminator. What is `split_ratio` on dividend-only rows? A 0/1 value otherwise divides by zero or mis-sets volume factors, and `_ADJ_EVENT_SCHEMA` rejects non-positive (`kline_sync.py:413-426`).

11. **Naive datetime timezone contract.** The pipeline passes `_dt.now()` (server local) as end (`daily_pipeline.py:286,311,333`) and date-only starts; Alpaca treats naive as ET (`alpaca/provider.py:39-48`). Should moomoo treat naive as ET? This decides one-day vs multi-day routing and the snapshot bar date.

12. **OpenD address persistence / QFQ precision.** "Stored like other plugin settings" has no non-key mechanism: built-in plugins expose only `api_key_env`/secrets (`loader.py:94-110`; `settings.py:770-800`). Is env-only plus a read-only card acceptable, or is a new settings endpoint needed? Also, if `get_cur_kline(AuType.QFQ)` returns cent-rounded prices, the proposed 1e-6 relative oracle on 1,000 days cannot pass.

# Claims the code contradicts

- "the gap-fill path picks them up next run": no per-symbol gap fill; pipeline range is global latest (`daily_pipeline.py:230,305`; `repository.py:1892-1903`).
- "reach adjusted prices by the next morning": rebuild pending is consumed only by the daily pipeline (`daily_pipeline.py:466-478`).
- "matches Alpaca's NYSE/NASDAQ universe (`alpaca/provider.py:192`)": Alpaca also requires `tradable` and `status=active` (`alpaca/provider.py:192-197`); moomoo basicinfo has no equivalent filter.
- "sync_adj_factor over the last 15 days (`kline_sync.py:488-519`)": 488-519 is the function only; normal path computes `adj_start = daily_range_start - 7 days` (`daily_pipeline.py:411-417`), and 15 days is only the `daily_range_start is None` fallback.
- Protocol says `pl.DataFrame` for instruments/realtime (`data_providers/base.py:33-38,70-75`), while the doc and actual providers use `list[dict]` (`alpaca/provider.py:183,339`). The doc's `get_realtime` key range also ends at 431, not 423, and `feed`/`volume_status` matter (Q8).
