# Moomoo data plugin — design (step 2 of 5)

Date: 2026-11-16. Status: APPROVED by the user 2026-11-16 (option a: full rotation every evening).

## Intent and why

ZenTrade's only source of US daily bars, adjustment factors and the stock list is Alpaca. The user wants
Alpaca gone and everything served from their own moomoo OpenD ("option B: use all on moomoo openD api to get
all of the information that available"). This step is the data half. Trading (the Alpaca paper mirror) is
step 4.

It matters now because the repo has **no market data on disk** (no `data/` folder, verified 2026-11-16).
Nothing can be backtested, and the 缠论/Wyckoff harness (step 3) cannot start, until moomoo bars flow
through ZenTrade's normal storage.

## Goals

- A built-in plugin `backend/app/plugins/moomoo/` that serves the datasets `daily`, `adj_factor`, `realtime`
  and the stock list (`get_instruments`) from OpenD at `127.0.0.1:11111`.
- After a first backfill, `kline_daily`, `adj_factor/all.parquet` and `kline_daily_enriched` hold about
  4 years (1,000 trading days) for the universe below, through the existing `kline_sync` → enriched path,
  unchanged.
- The daily post-close pipeline (16:35 ET) keeps the data current from moomoo with no Alpaca key present.
- The user picks moomoo per dataset on the existing Data Sources settings page (per-dataset routing already
  exists).

## Non-goals

- Trading, orders and accounts (step 4). The Alpaca plugin stays registered until step 4, because the
  paper mirror and `alpaca_options.py` still import `PAPER_BASE` and credentials from it
  (`notes/s2-r4-alpaca-inventory.md`). In step 4 Alpaca is removed completely.
- Minute history. Through `get_cur_kline`, moomoo gives only the latest 1,000 bars, which for 1-minute bars
  is about 2.5 days. Deeper minute history needs the historical quota (100 symbols per 7 days at the
  user's tier). Minute sync is already optional and off by default in ZenTrade
  (`daily_pipeline.py:584-588`). `minute` and `full_minute` stay unserved.
- Options data, fundamentals, news.
- Data older than about 4 years (see Constraints).

## Anti-goals (would be a failure even if it "works")

- Using today's stock list or today's liquidity to pick stocks for past dates inside backtests. Moomoo's
  list has no delisted names (verified: `delisting` is False for all 13,104 US stock rows). The plugin
  stores a broad list; point-in-time filtering belongs to the step 3 harness, which must report
  survivorship bias.
- Adjusted prices that silently differ from raw × factor. Stored daily bars must be **raw**, and
  adjustment comes only from `adj_factor` events, as today.
- Burning the historical K-line quota (100 symbols per 7 days) on routine syncs.

## Constraints (verified against the user's OpenD on 2026-11-16 unless marked)

| Fact | Evidence |
|---|---|
| Quote login works; US stock list via `get_stock_basicinfo(Market.US, STOCK)`: 13,104 rows (PINK 5,948, NASDAQ 4,231, NYSE 2,622, AMEX 302); ETFs 6,424 | probe2 |
| NYSE + NASDAQ + AMEX stocks: 7,155. 64.7% have the placeholder `listing_date` 1970-01-01 | probe3 |
| Class shares use dots: `US.BRK.B`, `US.BF.B` → ZenTrade symbol `BRK.B` (same as Alpaca) | probe3 |
| Odd codes exist (`US.1061`, `US.2618996D`, `US.55272X706`) | probe3 |
| `get_cur_kline(code, 1000, K_DAY, AuType.NONE)` after `subscribe(K_DAY)`: 1,000 raw bars (2022-11-18 → 2026-11-13). The NVDA 10:1 split is visible (06-07 close 1208.88, 06-10 close 121.79). Columns: open, close, high, low, volume, turnover, pe_ratio, turnover_rate, last_close | probe2 |
| History quota unchanged after `get_cur_kline` (2 used / 98 left) | probe2 |
| Raw bars' `last_close` on an ex-date is NOT adjusted (NVDA 06-10 `last_close` 1208.88), so corporate actions can't be detected from the previous close | probe4 |
| `get_rehab(code)` returns one row per event with `ex_div_date`, `split_ratio`, `per_cash_div`, `forward_adj_factorA/B`. NVDA split: A = 0.1, B = 0. Dividends: A ≈ 0.9991, B = 0 | probe2, probe3 |
| `get_rehab` limit: 60 requests / 30 s, one symbol per call | w6 Q6 (docs) |
| `get_rehab` needs no subscription. It returns the full lifetime of **per-event** rows (NVDA from 2000-06-27, AAPL from 1987-06-16, KO 120 rows). `split_ratio` is NaN on dividend-only rows and set on split rows. `forward_adj_factorB` = 0 on every row of all 3 symbols. 0.02 s per call | probe3, probe5 |
| Subscribing 100 symbols took 0.07 s. Then 200 `get_cur_kline` calls (raw + QFQ for those 100) took 31.9 s with 0 errors. QFQ prices carry 9 decimals (e.g. 119.429457959), not cents | probe5, probe2 |
| NYSE + NASDAQ stocks whose code is letters only (optionally `.X` class suffix): 6,338 | probe5 |
| Subscription quota 100 at this tier; each symbol × data type = 1; minimum hold 1 minute before unsubscribe | w6 Q1 (docs), quota probe |
| `get_market_snapshot`: 400 symbols per call, 60 calls / 30 s. After the close it carries the day's open/high/low/last/volume/turnover with `update_time` 16:00, and its volume matched the day's K-line | w6 Q4 (docs), probe2 |
| ZenTrade wants raw daily bars plus event rows `{symbol, trade_date, ex_factor = price before / price after, vol_factor}`, and forward-adjusts at enriched-build time | notes/s2-r2-storage.md |
| Plugins: `plugin.yaml` (datasets, entry, check) is auto-loaded; the provider is a duck-typed `MarketDataProvider` protocol (`data_providers/base.py:29-75`) | notes/s2-r1 |
| Each dataset routes to exactly one source; there is no fallback | notes/s2-r5 |

Assumed, not verified:
- Subscription rotation (subscribe 100, read, wait ≥ 60 s, unsubscribe, next 100) is allowed by moomoo's
  terms. The docs describe the 1-minute rule but say nothing against rotation.
- There is no hidden daily cap on `get_cur_kline` calls; only 200 calls were tested.
- `forward_adj_factorB` is 0 for all US events. Only 3 symbols were sampled.
- Snapshot behaviour on halted, early-close and after-hours days is unknown. This design therefore does
  not use snapshots for stored daily bars.

**History depth:** 1,000 trading days ≈ 4 years. This matches the step 3 plan (about 3 years development
plus 1 year holdout), with no slack for indicator warm-up. The stored history grows forward from here.

## Approaches considered

1. **Built-in plugin `plugins/moomoo/` with the moomoo SDK (chosen).** Same shape as `plugins/alpaca/`,
   plugged into the existing per-dataset routing, `kline_sync` staging, adj-factor events and enriched
   publication. Least new machinery.
2. **Custom YAML data source (`data/data_sources/*.yaml`).** This is built for HTTP APIs. OpenD speaks the
   SDK's protobuf over TCP, so it doesn't fit.
3. **A separate sidecar that writes parquet directly.** It would bypass staging, the event basis
   (`basis.json`), `rebuild_pending` and generation markers, and duplicate tested code. Rejected.

## Design

**Plugin files.**
- `backend/app/plugins/moomoo/plugin.yaml`: name `moomoo`, entry `app.plugins.moomoo.provider:MoomooProvider`,
  check `availability`, datasets `[daily, adj_factor, realtime]`.
- `provider.py`.
- New dependency: `moomoo-api` in `backend/pyproject.toml` (+ `uv.lock`). The SDK version to pin is left to
  the builder; use the version already working in `D:/Desktop/GitHub/_dsa_rnd/.venv`.

**Connection and availability.**
- The OpenD address comes from env `ZENTRADE_MOOMOO_OPEND` (via `credentials.py`, same lookup order as other
  secrets), default `127.0.0.1:11111`. No API key: OpenD handles login.
- `availability()` stays cheap and is only a load-time check (`loader.py:544-605`): the SDK imports and the
  address parses. It does **not** require OpenD to be up, so the plugin is always registered. Connection
  failures surface per sync as failed symbols / stage errors, and a late-started OpenD recovers on the next
  run. *(Answers builder Q9.)*
- A live status (`get_global_state()`: `qot_logined`, `market_us`) is shown on the settings card and in the
  setup checklist's new `moomoo` item (group `market`). When moomoo is routed for daily, the Alpaca item
  stops being `required`.
- One quote context per sync call, always closed in `finally`.

**Time.** Naive datetimes are US/Eastern wall clock, as in the Alpaca provider (`alpaca/provider.py:42-51`).
*(Builder Q11.)*

**Symbols.** ZenTrade symbol = moomoo code without the `US.` prefix (`US.BRK.B` → `BRK.B`). Map back by adding
`US.`.

**`get_instruments("stock")`** returns `list[dict]`, like Alpaca (`alpaca/provider.py:183`). The protocol in
`base.py` says DataFrame, but callers use the list form.
- Source: `get_stock_basicinfo(Market.US, SecurityType.STOCK)`, filtered to:
  - `exchange_type` in {US_NYSE, US_NASDAQ};
  - code matching `US.[A-Z]+(\.[A-Z])?`;
  - `delisting` False.

  About 6,338 names.
- Alpaca additionally required `tradable` and `status=active`. Moomoo has no such flag. The builder must
  check the `suspension` field values and drop suspended names if they are marked there.
- Each row: `{symbol, name, code, exchange, asset_type: "stock", source: "moomoo", region: "US"}`.
  `region` "US" makes `instrument_sync` run the existing Nasdaq reference enrichment
  (`instrument_sync.py:83-100`), which flags preferreds, warrants and units.
- `listing_date` 1970-01-01 is written as null.
- The core ETFs SPY/QQQ/DIA/IWM are added by `_resolve_universe` as today.

**`daily` and `adj_factor`: one rotation pass serves both.** Snapshots are not used for stored bars
(builder Q5).
- For the requested symbols, rotate subscriptions in batches sized to the **free** subscription quota (read
  it with `query_subscription`; never assume 100).
- Per batch:
  - `subscribe(K_DAY, subscribe_push=False)`;
  - per symbol, `get_cur_kline(code, 1000, K_DAY, AuType.NONE)` and `get_cur_kline(code, 1000, K_DAY,
    AuType.QFQ)`;
  - hold until ≥ 61 s after subscribe, then unsubscribe.
  - Measured: a 100-symbol batch reads in about 32 s, so the hold dominates. About 64 batches ≈ 65 min for
    the whole universe.
- `get_daily` / `iter_daily` returns the raw bars in `[start, end]` as the canonical 8 columns
  (`amount = turnover`), streaming one batch at a time through `on_chunk_done(cur, total)`.
- The same pass derives adjustment events from raw vs QFQ closes with the **existing**
  `services/adjustment_events.events_for_symbols(raw, adjusted, skip_invalid_symbols=True)`. That is the
  exact method the Alpaca plugin uses with Alpaca's `raw`/`all` bars (`alpaca/provider.py:308-315`), so
  existing contract tests apply.
  - The derived events for every symbol are written to a provider cache:
    `<data_dir>/moomoo/adj_events_<YYYY-MM-DD>.parquet`, plus the set of symbols covered.
- `get_adj_factors(symbols, start, end, anchor_dates)` returns cached events for covered symbols. For
  symbols not covered today, it calls `get_rehab` (paced ≤ 2/s) and maps each row:
  - `ex_factor = 1 / forward_adj_factorA`;
  - `vol_factor = 1 / split_ratio` where `split_ratio` is set, else 1;
  - several rows on one date are multiplied together;
  - a symbol with any row where `forward_adj_factorB ≠ 0` fails closed (logged, skipped).

  *(Builder Q3, Q4, Q6, Q10: no split-suspect filter and no separate nightly sweep are needed. Events land
  in the same 16:35 run, so the enriched rebuild picks them up immediately.)*
- The pipeline's adj window is `daily_range_start − 7 days` (`daily_pipeline.py:411-417`). The provider
  serves any window from cache or rehab.
- **Routine post-close cost:** a full rotation (about 65 min) every trading day. The 16:35 pipeline's
  enriched data is ready around 17:45 ET. *(This is the open question for the user below.)*
- **First backfill:** the same pass with `start` = 1,000 bars back, run once from the Data page backfill
  action or a script.

**No permanent per-symbol holes** (builder Q1).
- The pipeline starts from the global `latest_daily_date()` (`daily_pipeline.py:230,305`), so a symbol that
  returned nothing on day D would never be refetched.
- Requirement: any symbol in `zero_row_out`/`failed_out` is re-requested on the next run from its own last
  stored date. Each rotation already fetches 1,000 bars per symbol, so this costs no extra calls; only the
  date filter differs.
- How: a per-symbol start map passed into the provider (like `anchor_dates`), or the existing
  data-integrity scan. The builder picks.
- Test: a symbol missing day D gets D on the next run.

**`realtime` (`get_realtime`).**
- `get_market_snapshot` over the same universe, chunked at 400. That's about 16 calls per poll, so when
  moomoo is routed the effective poll interval floor is **15 s** (32 calls / 30 s, under the documented
  60). *(Builder Q2.)*
- It returns the same `list[dict]` keys as Alpaca (`alpaca/provider.py:399-431`) where moomoo has them;
  missing ones are None. `quote_ts`/`volume_ts` come from `update_time` interpreted as ET.
- The builder sets `feed`/`volume_feed` so that `quote_contract.normalize_record` (`quote_contract.py:35-76`)
  keeps snapshot volume, and documents the choice. The US quote level of the user's account was not checked,
  so `data_delay` is derived from `update_time`. *(Builder Q8.)*
- Realtime never writes stored daily bars.

**Pacing and failures.**
- A per-API sliding-window pacer inside the provider, because ZenTrade doesn't pace providers (notes/s2-r1):
  - snapshot ≤ 60 / 30 s;
  - rehab ≤ 60 / 30 s;
  - `get_cur_kline` unpaced but backing off on any rate-limit error.
- An OpenD disconnect or `RET_ERROR` on a batch records those symbols in `failed_out` and continues with the
  others.
- `adj_factor` stays fail-closed per symbol.

**Settings UI.**
- A read-only `MoomooDetail` card on Data Sources: OpenD address (from env, with how to change it), live
  login status, last rotation time and duration. No new settings endpoint. *(Builder Q12.)*
- The frontend display fallback `'alpaca'` for unset prefs (`DataSources.tsx:101-106`) becomes "未配置".
- The Alpaca card is removed in step 4.

## Tests the builder must include

1. **Provider unit tests with a fake quote context** (no OpenD):
   - symbol mapping and instrument filtering;
   - rotation honouring the free quota and the 61 s hold (injected clock);
   - raw/QFQ → events through `events_for_symbols`;
   - rehab → event mapping:
     - NVDA 2024-06-10: A 0.1, split_ratio 0.1 → ex_factor 10, vol_factor 10;
     - an AAPL dividend → vol_factor 1;
     - B ≠ 0 rejection;
   - the per-symbol hole repair.
2. **Existing contract tests still pass**, e.g. `tests/test_adjustment_failure_contract.py`.
3. **Opt-in live test** (skipped unless `ZENTRADE_MOOMOO_LIVE=1`) on AAPL, NVDA, BRK.B, KO:
   - forward-adjusting the raw bars with the derived events reproduces moomoo's QFQ closes within 1e-6
     relative on all 1,000 days (QFQ has 9 decimals, verified);
   - rehab-mapped events equal raw/QFQ-derived events on overlapping dates.
4. **Dry run of the full pipeline** on a temporary `DATA_DIR` with a 20-symbol universe:
   instruments → daily → adj → enriched. Enriched rows exist and `.matrix_generation_stock` changes.

## Decided by the user

- Moomoo replaces Alpaca for all data (option B). Order: presets → moomoo data → harness → moomoo
  SIMULATE trading → LLM veto.
- Harness approach A, which will read ZenTrade's stored bars.

## My calls (the user can override)

- Alpaca stays registered until step 4. It's needed by the mirror; this step only re-routes data.
- Universe = NYSE + NASDAQ letter-coded stocks, about 6,338. Point-in-time liquidity filtering happens in
  step 3.
- The minute datasets are dropped, not replaced.
- Adjustment comes from moomoo's own QFQ bars through the existing event code; rehab is the fallback and
  oracle.
- The OpenD address comes from env, with a read-only card.
- Realtime polls the full universe at ≥ 15 s.

## Resolved question (user chose a, 2026-11-16)

- **Post-close readiness vs robustness.** A full rotation makes the day's data ready about 70 min after the
  16:35 start (about 17:45 ET):
  - **(a) Rotation every day (my pick).** Same code path as the backfill; per-symbol holes are repaired;
    dividends and splits land the same evening.
  - **(b) A fast snapshot path for the routine day (about 1 min).** It needs live checks of halted,
    early-close and after-hours behaviour first. It also still needs a rotation or rehab sweep for
    adjustments.

## Left to the builder

- SDK version pin.
- How the pacer and cache files are built.
- Card layout.
- Whether `get_daily` and `iter_daily` share code.
- Where the backfill action is exposed.
- How per-symbol starts reach the provider.
- `suspension` filtering.
