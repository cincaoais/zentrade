# Alpaca data plugin + plugin framework

## plugin.yaml datasets table: dataset | function | args | returned columns/dtypes | cite

Manifest: `backend/app/plugins/alpaca/plugin.yaml:1-11` — `name: alpaca`, `runtime: none`,
`entry: app.plugins.alpaca.provider:AlpacaProvider`, `check: ...:availability`,
`datasets: [daily, adj_factor, realtime, minute, full_minute]`, `api_key_env: ZENTRADE_ALPACA_KEY`.
All 5 datasets are served by the single class `AlpacaProvider` (`provider.py:152`).

| dataset | function | args | returned columns / dtypes | cite |
|---|---|---|---|---|
| daily | `AlpacaProvider.get_daily` (also streaming `iter_daily`) | `symbols: list[str], start_time: datetime\|None, end_time: datetime\|None, asset_type="stock", on_chunk_done=None` | `symbol(str), date(Date, via str.to_date), open, high, low, close (float from JSON, no explicit cast), volume(float), amount(float v*vw or None)` — no schema; dtypes inferred | `provider.py:241-252`, `222-239` (`_daily_row`, `_daily_rows_to_frame`) |
| adj_factor | `get_adj_factors` | `symbols, start_time, end_time, asset_type="stock", on_chunk_done=None, anchor_dates: dict[str,date]\|None` | explicit schema `{symbol: Utf8, trade_date: Date, ex_factor: Float64, vol_factor: Float64}`, sorted | `provider.py:273-337` (schema at `332-337`) |
| realtime | `get_realtime(self)` (+ `get_realtime_indices(symbols)`) | no args (indices: `symbols: list[str]`) | returns `list[dict]`, not a DataFrame; keys incl. `symbol, name, last_price, prev_close, open, high, low, volume, amount, change_pct, change_amount, amplitude, turnover_rate, timestamp, quote_ts, volume_ts, daily_ts, feed, quote_feed, volume_feed, historical_volume_feed, data_delay, quote_status, volume_status, session` | `provider.py:339-434` (keys `399-423`) |
| minute | `get_minute` | `symbols, start_time, end_time, asset_type="stock", freq="1m", on_chunk_done=None` | `symbol(str), datetime(ET-naive datetime), open, high, low, close, volume(float), amount(float v*vw or None)` — no schema | `provider.py:534-568` |
| full_minute | no dedicated method; served by `get_intraday_batch(symbols, count=300, asset_type="stock")`, `get_intraday_since(symbols, start_time)`, `minute_request_count` | framework calls via `getattr` duck-typing | same frame shape as `minute` (delegates to `get_minute`) | `provider.py:571-588`; framework `kline_sync.py:972-999`, `1058-1065` |

## Provider interface (base class, call flow)

- Contract is a **Protocol**, not an ABC: `MarketDataProvider` in `backend/app/data_providers/base.py:29-75`
  (`get_instruments`, `get_daily`, `get_adj_factors`, `get_minute`, `get_realtime`) with
  `ProviderCapabilities` dataclass (`base.py:19-26`) and `AssetType` literal (`base.py:16`).
  `AlpacaProvider` subclasses nothing; it duck-types (`provider.py:152`).
- **Loading**: `loader.py:_load_builtin_plugins` scans `app/plugins/*/plugin.yaml` (`loader.py:544-566`),
  `_register_one_plugin` runs the manifest `check` (`_call_check`, `loader.py:609-625`), dynamic
  `_load_entry("module:attr")` (`loader.py:628-634`), instantiates and sets `builtin=True`
  (`loader.py:597-602`). Called at import time (`loader.py:637`).
- **Dispatch**: callers resolve the routed provider via `custom_sources.get_provider(name)` after
  `provider_has_dataset(name, ds)` (`loader.py:328-341`); routing prefs in
  `services/preferences.py:274-299`. Call sites: daily `kline_sync.py:256-292` (prefers
  `iter_daily` stream), adj_factor `kline_sync.py:505-513` (passes `anchor_dates`), minute
  `kline_sync.py:783-809`, realtime `quote_service.py:613-626`, full_minute
  `kline_sync.py:972-999`/`1058-1065`.
- **Batching**: inside the provider, bars endpoint is chunked at `_BATCH=100` symbols
  (`provider.py:34,127`) with page limit `_PAGE_LIMIT=10000`; snapshots at `_SNAPSHOT_BATCH=1000`
  (`provider.py:35`). The framework wraps the 2-arg `on_chunk_done(cur,total)` into a 3-arg
  `(cur,total,seg_label)` callback (`kline_sync.py:755-761`, contract note `base.py:63-68`).

## Credentials, rate limits, retries

- Key source: `_load_credentials()` → `app.credentials.alpaca()` (`provider.py:54-58`).
  Fields: packed `ZENTRADE_ALPACA_KEY` / secrets.json field `alpaca_api_key`
  (`credentials.py:26`), fallback `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` (`credentials.py:27-28`);
  full rule `credentials.py:70-79`. Manifest declares `api_key_env: ZENTRADE_ALPACA_KEY` (`plugin.yaml:7`).
- Sent as headers `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` (`provider.py:84-87`).
- **Retries** (`provider.py:80-105`): `retries=3`, `timeout=30.0`; retries on HTTP 429/5xx and
  generic exceptions with exponential backoff `2**attempt` seconds (`provider.py:93-97,101-103`).
  Only 429/5xx/transport retried; other HTTP errors raised.
- **Rate limiting**: Alpaca provider itself has **no rpm pacing** — it just makes requests; the
  framework's rpm is declared via a shim `.config.datasets` with `rpm=200`, `batch=100`
  (1000 for realtime) (`provider.py:170-177`), consumed by
  `capabilities.current_capability_set` (`capabilities.py:67-75`). Generic HTTP/custom path
  uses `sleep_between_batches/chunked` (`rate_limits.py:92-118`); NOT VERIFIED that any service
  paces Alpaca with that helper (Alpaca path paginates internally instead).

## Price adjustment

- **Daily returns RAW (unadjusted) prices**: `get_daily` calls `_iter_bars(..., adjustment="raw", feed="sip")`
  (`provider.py:250-251`); `iter_daily` likewise (`provider.py:263-265`). Docstring states
  "raw OHLC 不复权" (`provider.py:11`).
- **adj_factor is event-based**, not a level series: `get_adj_factors` pulls both `raw` and `all`
  bars and derives events via `events_for_symbols(raw, adjusted, skip_invalid_symbols=True)`
  (`provider.py:308-315`); returns `ex_factor` = price-before / price-after ratio at real ex-dates,
  plus `vol_factor` (`provider.py:281-285`, schema `332-336`). Code-handoff pollution filtered via
  `ticker_handoff` (`provider.py:319-329`). The framework stores these events as
  `adj_factor_events` (`kline_sync.py:514-518`).

## Assumptions baked in (symbols, tz, calendar)

- **Symbols**: plain bare tickers, e.g. `AAPL` (docstring `provider.py:10`); universe =
  active `us_equity` on NYSE/NASDAQ tradable only, `asset_type="stock"`, `region="US"`
  (`provider.py:183-210`, endpoint `PAPER_BASE/assets`).
- **Timezone**: internal naive datetimes are **US/Eastern wall-clock**. Caller windows are
  converted to UTC at the HTTP boundary with naive interpreted as ET (`_as_utc`,
  `provider.py:42-51`); minute bar UTC `t` converted back to ET-naive (`_utc_ms_to_et_naive`,
  `provider.py:631-633`); daily `date` = first 10 chars of `t` (ET date, `provider.py:236`).
  Framework re-enforces ET-wallclock on ingest (`kline_sync._enforce_minute_market_wallclock`,
  `kline_sync.py:713`).
- **SIP delay**: free SIP bars are clamped to `now-16min` before requesting
  (`provider.py:118-124`), declared as `minute_delay_minutes = 16` (`provider.py:161`);
  framework reads it (`kline_sync.py:1031-1037`).
- **Trading calendar**: provider filters minute/pre-market bars to regular session
  `09:30 <= t < session_close` using `us_clock.session_close` (handles half-days)
  (`provider.py:556-557`); uses `us_clock.is_trading_day` for execution quotes
  (`provider.py:535-537` for 143). Calendar lives in `backend/app/market_clock/clock.py`
  (`US_SESSION_OPEN=09:30` line 19, `US_TZ=ZoneInfo("America/New_York")` line 27,
  `is_trading_day` line 86, `session_close` line 157).
- Realtime/execution splits feeds: research = `delayed_sip` (15-min, `_SNAPSHOT_DELAY_SECONDS=900`),
  execution = `iex` price-only; volume disabled on IEX fallback (`provider.py:37-39,396-398`).

## Open questions

- Daily/minute frames have **no explicit Polars schema**; exact dtypes depend on JSON values
  (`open/high/low/close` are raw JSON numbers, not cast) — NOT VERIFIED whether caller
  normalizes/casts them (`normalizer.normalize_daily` casts to Float64 but is not called in the
  Alpaca plugin path).
- Who paces Alpaca rpm=200 in practice (no throttle inside `provider.py`) — NOT VERIFIED.
- `full_minute` has no method named after the dataset; it works only because the framework
  duck-types `get_intraday_*` (`kline_sync.py:972-999`).
