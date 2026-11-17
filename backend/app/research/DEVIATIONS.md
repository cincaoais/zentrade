# Engine deviations (research harness)

Source: `D:/Desktop/GitHub/wyckoff/src/wyk` at commit `fc142999e4fbfb617333878c72c5b3d3a03ea255` (the user's own
code). The port keeps every threshold and every detector rule verbatim. This file lists the only changes, plus the
`wyk` tests that were dropped because they exercise code that was not ported. Copy this file into every report.

## Port changes

| File | Change | Why |
|---|---|---|
| `backend/app/research/wyckoff/base.py` | Header added; imports rewritten `wyk.*` -> `app.research.wyckoff.*` | New package location. |
| `backend/app/research/wyckoff/base.py` | Deleted `prepare()`, `slot_of()`, `_volume_features()` and the `wyk.timeutil.to_ms` import | XAUUSD/FTMO-specific `wyk.data.bars` paths (server sessions, ftmo/duka feeds, holiday flags); US daily bars use `us_bars.us_candles()`. |
| `backend/app/research/wyckoff/base.py` | No `TICK` rounding in the US path; `TICK` constant remains only for provenance | US daily OHLC is already in price cents; rounding would be a no-op at best and could alter vendor precision. |
| `backend/app/research/wyckoff/base.py` | Docstrings that pointed at deleted `wyk.data.bars` / intraday helpers reworded | They described code that no longer exists here; no rule changed. |
| `backend/app/research/wyckoff/volume.py` | Header added; module copied verbatim (intraday helpers kept, unused) | Intraday helpers do not affect the D1 path; keeping them reduces the diff. |
| `backend/app/research/wyckoff/pivots.py` | Header added; imports rewritten | New package location. |
| `backend/app/research/wyckoff/ranges.py` | Header added; imports rewritten | New package location. |
| `backend/app/research/wyckoff/accumulation.py` | Header added; imports rewritten | New package location. |
| `backend/app/research/wyckoff/distribution.py` | Header added; imports rewritten | New package location. |
| `backend/app/research/wyckoff/effort.py` | Header added; imports rewritten; `choch` helper `mean` binds the loop slice as a default argument (`def mean(x, sl=sl)`) | New package location; the repo lint test forbids unbound loop-variable closures (ruff B023). The helper is called inside the same iteration, so output is unchanged. |
| `backend/app/research/wyckoff/quality.py` | Header added; imports rewritten | New package location. |
| `backend/app/research/wyckoff/us_bars.py` | New adapter replacing `prepare()` for US daily bars | Input `date, open, high, low, close, volume`; drops `high <= low`; `t`/`tc` = UTC midnight / +1 day; `post_gap` when the stored-bar gap is > 5 calendar days; `holiday`, `holiday_lowvol`, `news` all False; D1 `rv`/`ae`; `v` = real share volume, `v2`/`rv2`/`ae2` NaN; no TICK rounding. |

## Dropped `wyk` tests

These tests exercise deleted XAUUSD / FTMO code, so they were not ported.

- `tests/test_foundation.py::test_holdout_guard_raises_without_token` (XAUUSD holdout guard, `wyk.data.holdout`).
- `tests/test_foundation.py::test_loader_never_reads_past_2022_without_token` (`wyk.data.store` M1 loader).
- `tests/test_foundation.py::test_d1_bar_is_the_server_day_closing_at_17_ny` (`wyk.data.bars.build_bars`).
- `tests/test_foundation.py::test_h4_blocks_are_server_aligned` (`wyk.data.bars.build_bars`).
- `tests/test_foundation.py::test_bar_volume_is_sum_and_partial_feed_bar_is_blanked` (`wyk.data.bars.build_bars`).
- `tests/test_foundation.py::test_prepare_is_causal_under_truncation` (tests the deleted `prepare()`; the causal
  `rv` / `deseason` / `abnormal_effort` tests and the pivots truncation tests are kept).
- `tests/test_ranges.py::test_b2_anchor_invariants_on_isdev_h1` and
  `tests/test_ranges.py::test_b2_event_ranges_truncation_on_isdev_h1` (the `isdev_h1` fixture loads XAUUSD H1 data
  through the deleted `prepare()` and `wyk.stage1.data`).

Also copied: `tests/shapes.py` -> `backend/tests/research/wyckoff/shapes.py` (synthetic shapes used by the ported
detector tests).
## Composer deviations

| File | Change | Why |
|---|---|---|
| `backend/app/research/compose.py` | `score` is a neutral deterministic hash of `(symbol, decision date)` instead of the spec's constant `score=1` | With equal scores ZenTrade fills the 10 slots in panel order (alphabetical), a selection bias. The hash keeps selection deterministic without favouring symbol names. |
