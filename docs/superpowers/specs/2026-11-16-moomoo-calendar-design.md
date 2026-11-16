# Moomoo trading calendar — short design (small change, step 2 follow-up)

Date: 2026-11-16. Decided by: Claude, delegated by the user ("i don't have alpaca. You can do your own decision now").

**Why.** ZenTrade's US trading calendar comes only from Alpaca (`backend/app/market_clock/calendar.py:_fetch_calendar_rows`).
The user has no Alpaca keys, so `MarketClock` falls back to treating every weekday as a trading day. That
breaks:
- holidays, e.g. the integrity scan treats them as missing data and starts a full repair run
  (`data_integrity.py:258-264`);
- half-day closes;
- trading-day counts.

**Verified (2026-11-16 probes, user's OpenD).** `request_trading_days(TradeDateMarket.US, start, end)` returns
`[{"time": "YYYY-MM-DD", "trade_date_type": "WHOLE"|"MORNING"}]`:
- Thanksgiving and Christmas 2025 are absent.
- `MORNING` days are 2025-07-03, 2025-11-28, 2025-12-24, 2026-11-27 and 2026-12-24, the NYSE 13:00 ET early closes.
- A request through 2027-12-31 returned data only to 2026-12-31. The existing cache records coverage as the
  last day returned (`calendar.py`, `f_hi = max(fetched)`), so later days fall back to weekdays rather than
  being treated as holidays.

**Change (one function plus tests).**
- `_fetch_calendar_rows(start, end)` tries moomoo first, through `app.plugins.moomoo.provider.open_ctx()`.
  Each moomoo day maps to an Alpaca-shaped row: `{"date": time, "close": "13:00" if trade_date_type ==
  "MORNING" else "16:00"}`.
- If moomoo fails (OpenD down, `RET_ERROR`, import error), it falls back to Alpaca when Alpaca keys exist.
  Otherwise it raises the moomoo error, and the existing cache/backoff/fail-open logic handles it unchanged.
- Function names (`alpaca_trading_days`, `alpaca_session_close`) and `clock.py` are untouched. Renaming them
  belongs to step 4 (Alpaca removal).
- An unknown `trade_date_type` (not WHOLE/MORNING) is treated as a full day and logged at WARNING once.

**Approaches.**
1. Swap the row source inside `_fetch_calendar_rows` (chosen): smallest change; the cache, refresh, backoff
   and half-day logic are reused as is.
2. A new calendar provider injected into `MarketClock`: duplicates the cache logic.
3. Wait for step 4: keeps wrong holidays until then.

**Tests** (`backend/tests`, no OpenD):
- moomoo rows map to dates plus `13:00` early closes;
- moomoo failure with no Alpaca keys raises;
- moomoo failure with Alpaca keys uses Alpaca;
- an end-to-end `alpaca_trading_days` call with a fake moomoo context on a temporary data dir:
  - excludes 2025-11-27;
  - `alpaca_session_close(2025-11-28) == 13:00`;
  - a day past the returned range (2027-01-04) is treated as a trading weekday.

Existing calendar tests must still pass.

**Not covered.** Holidays that moomoo learns about later. The existing 30-day refresh handles those.
