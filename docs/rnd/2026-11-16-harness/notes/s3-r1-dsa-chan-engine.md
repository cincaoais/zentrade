# DSA chan engine

Repo: D:/Desktop/GitHub/daily_stock_analysis (read-only). engine.py = 694 lines, service.py = 156.

## API (inputs/outputs)

- Package exports: `build_level_snapshot`, `build_chan_snapshot`, `LEVEL_KEYS`, `SCHEMA_VERSION`, `SUPPORTED_TIMEFRAMES` (`src/chan/__init__.py:8-18`); schema constant `chan_snapshot/1.1` (`src/chan/engine.py:32`).
- Input frame: required columns `date,open,high,low,close` (`src/chan/engine.py:49`), `volume`/`amount` optional and synthesized from close*volume (`engine.py:241-261`). Timeframes only `1w,1d,60m,30m` (`engine.py:36-39`); anything else raises `ValueError` (`engine.py:515-516`).
- `build_level_snapshot(df, timeframe, symbol="")` (`engine.py:509`) never raises for data problems: returns `status` in available/partial/missing/engine_unavailable/error plus `warnings` (`engine.py:518-555`). Min bars: 30 to attempt structure, 120 for "available" (`engine.py:40-41`).
- `build_chan_snapshot(frames, symbol, market=, name=, sources=)` (`engine.py:646`) → `{schema_version, symbol, name, market, as_of, level_roles, data_quality, levels, cross_level}` (`engine.py:679-694`); level roles = direction/operating/confirmation with D default (`engine.py:629-643`).
- Output level dict (`engine.py:576-600`): `trend_type` (up/down/consolidation, `engine.py:219-232`), `position_vs_last_zhongshu`, `current_bi`, `last_confirmed_bi`, `recent_bis[-7]`, `zhongshu[-3]`, `divergence` (MACD-area, `strength_ratio` only numeric score, `engine.py:482-499`), `buy_sell_points`, `key_levels`, `segment: {status: not_supported}`.
- Signal object (buy/sell point) built by `_point_dict` (`engine.py:445-468`): `evidence_id` ("D.3B.2026-10-20"), `type` 1B/2B/3B/1S/2S/3S, `side`, `level`, `dt`, `price`, `invalidation_price`, `basis` (trend_divergence | range_divergence | after_1B/1S | pivot_exit), `state`, `still_valid`, `bars_since`. No per-point numeric confidence/score.
- `confirmed` flag: `Stroke.confirmed=False` means still-forming last stroke (`engine.py:57,66`); `TradePoint.confirmed` (`engine.py:101`) is copied from the stroke. Schema 1.1 replaced the emitted `confirmed` with `state` (`engine.py:32`; test asserts `"confirmed" not in point`, `tests/test_chan_engine.py:213`). `stroke_state()` returns forming / candidate (newest finished, may be redrawn) / confirmed (`engine.py:404-415`).
- Rules: divergence = new extreme vs stroke i-2 with smaller MACD area (`engine.py:109-133`, ratio < 1.0, `engine.py:46`); 1B/1S divergence outside latest pivot (`engine.py:150-177`); 2B/2S first pullback not breaking 1B/1S (`engine.py:179-196`); 3B/3S pivot exit plus pullback outside (`engine.py:198-217`); only points within last 9 strokes are reported (`engine.py:45,561-566`).
- service.py: `load_chan_snapshot(fetcher_manager, code, ...)` fetches per timeframe via `fetcher_manager.get_kline_data(code, timeframe=, days=_LOOKBACK_DAYS)` (`service.py:37,55-57`; lookback 900/365/120/45 days, `service.py:19`) then calls `build_chan_snapshot` (`service.py:63`); `compact_chan_snapshot` / `chan_snapshot_json` (7500-char prompt budget) / `format_chan_prompt_section` (`service.py:74,107,151`).

## czsc usage + version

- Lazy import `import czsc` (`engine.py:283-287`). All bars go through `czsc.format_standard_kline(bars_df, freq=_TIMEFRAME_FREQ[timeframe])` then **`czsc.CZSC(bars)`** (`engine.py:293-306`).
- Objects read: `analysis.bi_list` with `bi.direction/sdt/edt/high/low/length` (`engine.py:311-323`); `analysis.zs_list` with `zs.bis/zg/zd/gg/dd/sdt/edt` (`engine.py:365-379`). No explicit `RawBar`/`BI`/`ZS` construction and no czsc signals/strategies; `RawBar` only implicitly via `format_standard_kline`.
- Version: `czsc>=1.0.2,<2` (`requirements.txt:38`). Installed in `.venv`: **1.0.2** (Rust/PyO3 wheel, `czsc/_native.pyd`; verified `import czsc; czsc.__version__`). In 1.0.2 `CZSC.zs_list` is recomputed as `get_zs_seq(finished_bis)` on every access (czsc source `crates/czsc-core/src/analyze/mod.rs:536`); `ZS.zg/zd` come from the first 3 bis, `gg/dd` from all bis (`crates/czsc-core/src/objects/zs.rs:48-66`).

## Signal timing / lookahead / repaint (quoted lines)

- Whole-series batch rebuild every call: `czsc.CZSC(bars)` is constructed from the complete frame (`engine.py:306`); there is no retained state object and no incremental path in the engine.
- No forward lookahead past the supplied frame: module docstring "all deterministic, no look-ahead beyond the frame" (`engine.py:8`); MACD is causal pandas `ewm(..., adjust=False)` (`engine.py:264-268`); `_macd_area` only sums bars with `date > start & date <= end` (`engine.py:270-278`).
- A point is dated at the end of the stroke/pullback that creates it (`engine.py:154,187,209`), i.e. it is known only after that stroke is finished; the newest finished stroke is explicitly unstable: "czsc may still redraw or remove the newest finished stroke" and is labelled `candidate` (`engine.py:405-415`).
- `_forming_stroke` scans the whole tail after the last confirmed bi with `tail["high"].idxmax()` / `tail["low"].idxmin()` (`engine.py:338-350`), so its endpoint, price and MACD area move with every new bar; it is emitted with `confirmed=False` (`engine.py:358`).
- Point detection is recomputed over all strokes each call ("over the whole stroke list, oldest first", `engine.py:145`), but the same stroke index can change state candidate→confirmed (`engine.py:404-415`), and 3B/3S read `pivot.last_stroke_index` (`engine.py:198-217`) which advances while czsc extends the last ZS, so later 3B points appear on the ZS's newest leg.
- Pivot levels can repaint while a ZS grows: `zg/zd` fixed after 3 bis but `gg/dd` are over all bis (`zs.rs:48-66`), and the ZS is rebuilt from the full finished-bi list on each property access (`mod.rs:536`).
- `still_valid` is an outcome-so-far computed from bars **after** the point: `after = frame[frame["date"] > point.dt]` (`engine.py:448`), then `after["low"].min() >= invalidation_price` / `after["high"].max() <= ...` (`engine.py:451-453`); `state` becomes `invalidated` when false (`engine.py:465`). It never looks past the last bar of the frame.
- The repo's own replay test encodes the expectation: a point is never seen before its own bar, and `candidate` is seen no later than `confirmed` (`tests/test_chan_event_study.py:48-62`). Determinism: identical snapshot for shuffled/duplicated input (`tests/test_chan_engine.py:224-229`).

## Replay feasibility

- Cannot be fed bar-by-bar as-is: `build_level_snapshot` accepts a full DataFrame and rebuilds `CZSC` from scratch (`engine.py:293-306,509`). czsc 1.0.2 itself does support incremental updates — `CZSC.update(bar)` signature `(self, bar)` (verified via `inspect.signature`; Rust `analyze/mod.rs:182` `update_bar`) and `CZSC.from_dataframe(arrow_bytes, freq, ...)` — but the DSA engine never uses either.
- Existing point-in-time replay: `evals/chan_event_study/study.py:60-80` loops `for t in range(max(warmup,30)-1, len(data)): build_level_snapshot(data.iloc[:t+1], ...)` → one full rebuild per bar (default warmup 120).
- Cost per symbol per level, 1000 bars (loop counts): 1 czsc build = 1000 bar updates; engine loops ≈ n_bi (~1000/5 ≈ 200) `_macd_area` calls, each doing a boolean mask over ≤1000 rows (`engine.py:272-277`) → ≈2e5 row comparisons; pivot loop = n_zs (tens); `detect_trade_points` loops n_bi + n_1B + n_zs, with `_context_pivot_index` scanning pivots backwards per diverging stroke (`engine.py:136-142`); `_point_dict` filters the frame once per reported point, ≤ handful thanks to the 9-stroke lookback (`engine.py:561-566`).
- Full replay of 1000 bars ≈ 880 rebuilds × (~200 mask ops + 3 ewm passes + 1 czsc rebuild) ≈ 1.8e5 pandas mask operations / ~1.8e8 row ops per symbol per level; `service.py` runs up to 4 timeframes (`service.py:15,19`).
- An incremental adapter would need to keep (a) the `CZSC` instance fed by `update`, (b) the bar frame for causal MACD areas, and (c) re-run only `_extract_strokes/_extract_pivots/detect_trade_points`; none of that exists today (module is a set of pure functions, no class/state).

## Dependencies

- engine.py: only `pandas` (`engine.py:26`); stdlib `logging`, `dataclasses`, `typing`. MACD hand-rolled with `ewm` — no talib/TA-Lib anywhere in `src/chan/`, `requirements.txt`, `pyproject.toml` (grep: no match).
- czsc is a lazy optional dependency (`engine.py:283-287`; ImportError → status `engine_unavailable`, `engine.py:539-541`).
- service.py: stdlib `copy,json,logging,typing` (`service.py:5-12`) plus an injected `fetcher_manager`; no pandas import.
- Repo pins: `pandas>=2.0.0` (`requirements.txt:34`), `czsc>=1.0.2,<2` (`requirements.txt:38`).

## Open questions

- Invalid pivots: the engine only filters `len(bis) < 3 or not zg > zd` (`engine.py:367-369`); `ZS.is_valid` exists in czsc 1.0.2 but is never called — NOT VERIFIED whether `zs_list` can contain invalid ZS.
- `bi.length` semantics (bar count vs ubi count) — NOT VERIFIED.
- Older pure-Python czsc 1.0.x (<1.0.2) attribute compatibility (`bi.sdt/edt`, `zs.bis`, `freq="日线"`) — NOT VERIFIED; only 1.0.2 Rust build was inspected.
- How often past 1B/2B/3B outputs actually change on real data (repaint rate) — NOT VERIFIED; the repo's event study measures candidate→confirmed timing, not revision counts.
- Whether 2B/2S at `i+2` (`engine.py:180-196`) matches canonical chan definitions — NOT VERIFIED (not judged here).