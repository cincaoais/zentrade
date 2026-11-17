# Own Wyckoff repo

Repo: `D:/Desktop/GitHub/wyckoff` (package `wyk`, 2014-2026 XAUUSD CFD R&D, live trading has no code path: `CLAUDE.md:7`). All paths below are relative to that repo. Perimeter read: `CLAUDE.md`, `pyproject.toml`, `src/wyk/{config.py,detectors/*,features/volume.py,data/bars.py,data/calendar.py,data/store.py,stage1/costs.py}`, `tests/`.

## Events/phases + rules

- Pivots (`detectors/pivots.py:13-27`): pivot high at p needs `H_p > max(H[p-3..p-1])` and `H_p >= max(H[p+1..p+3])`; low mirrored. Trend = close vs EMA50 plus a 60-bar move of >= 8 ATR (`pivots.py:30-37`).
- Phase-A chain, accumulation, mirrored for distribution (`detectors/ranges.py:139-295`): SC/BC candidate = trend bar, low == 60-bar low, range >= 1.5 ATR, bullish close for BC, RV climax; climax = `RV_i >= 2.0` OR RV_i is the max valid RV of the last 60 bars with >= 20 valid values (`ranges.py:112-130,164-179`). A lower low within 2 bars extends an SC cluster and swaps the volume reference (`ranges.py:193-208`).
- AR = highest high after the cluster extreme, requiring `(H-L) >= 3 ATR` and (pivot confirmed 3 bars later OR close <= H - 0.33*(H-L)) (`ranges.py:218-235`).
- ST = bar u after the AR confirmation with low in `[L, L+1.0 ATR]` and (pivot at u+3 OR close >= low + 1 ATR); leg max RV <= 0.7 * RV_SC (`ranges.py:238-274`). TR bounds frozen at that ST: acc lo = SC low, hi = AR high (`ranges.py:276-284`).
- Generic TR (`ranges.py:71-109`): 60-bar window, height 3-10 ATR, >= 2 confirmed pivots within 0.5 ATR of top and >= 2 of bottom, `|OLS slope|*W <= 0.3*height`. S0-1 re-checks the same quality causally at `i >= known_at` (`detectors/quality.py:3-12`).
- Spring (`detectors/accumulation.py:210-289`): break `L_b < TR_lo` with `C_{b-1} >= TR_lo`; the FIRST close back above TR_lo within J=2 decides: CLV >= 0.5 => `spring`, else `spring_failed/weak_close`; depth > 1.5 ATR => `too_deep`; none by b+J => `no_recovery` (`accumulation.py:225-238`). Gates: max RV outside <= 0.7*RV_SC (acc) or <= 0.7 (generic) and break-bar RV < 1.5 (`accumulation.py:232-276`).
- `spring_test` (`accumulation.py:294-349`): test low in `(L_sp, L_sp+1.0 ATR]` or in `[TR_lo, TR_lo+0.5 ATR]` inside 15 bars, trigger = first later close above that bar's high. `entry_aggressive` = close above the high of the largest bear bar, within 2 bars of the spring low (`accumulation.py:319-327`). `entry_steady` = same test trigger after >= 2 closes back inside the range (`accumulation.py:340-348`).
- `sos` (`accumulation.py:377-437`): close > TR_hi, 1-3 bar aggregate bull body >= 0.8 ATR and CLV >= 0.7, mean RV >= 1.5; needs a prior spring or Phase B >= max(10, Phase-A bars). `sos_failed` = first close < TR_hi - 0.25 ATR within 10 bars (`accumulation.py:440-455`).
- `lps` (`accumulation.py:464-511`): 3 small pullback bars (range <= 0.6 ATR, |C-O| <= 0.4 ATR), close above their max high, a pullback low in `[TR_hi-0.25, TR_hi+1.0 ATR]`, mean pullback RV <= 0.8, expiry 20 bars.
- `ut` (`detectors/distribution.py:169-256`): `H_b > TR_hi` with previous close <= TR_hi, then a close < TR_hi within J_u=3 => `ut` known at that close; no fall-back by b+2 => `ut_failed`. Variants hv = RV_b >= 1.5, lv = RV_b < 1 or RV at the UT high < RV of the previous confirmed non-holiday pivot high (`distribution.py:218-232`).
- `utad_confirmed` (`distribution.py:262-287`): lower pivot high after the UT, confirmed 3 bars later, then a close below L_{x+1}; 30-bar expiry. `sow` = close < ice with 1-3 bar bear body >= 0.8 ATR, CLV <= 0.3, RV >= 1.5 flag; `sow_failed` = close > ice + 0.25 ATR within 10; `sow_minor` = low within 0.5 ATR of ice with no close below (`distribution.py:331-386`).
- `lpsy_d` (`distribution.py:396-431`): touch H >= ice - 0.75 ATR, no close > ice + 0.25 ATR, then a close below the min low of the last 2 bars; <= 2 retests, 20-bar expiry, rally mean RV <= 0.8. `lpsy_c` = rally into `[H_bc-0.5 ATR, H_ut)` with stall <= 0.2 ATR or upper wick >= 0.5 range; AE >= 1 or RV >= 1.5 is the effort gate (`distribution.py:449-481`).
- Effort (`detectors/effort.py:20-45,124-246`): `churn` RV >= 2.0 or AE >= 1.0 with result <= 0.2 ATR or upper wick; `absorption` mirrored; `no_supply` mean RV of 3 bars <= 0.7 in a down trend; `no_demand` new high with RV_t <= 0.7 (or <= 0.7 * RV_pivot). `choch` = n_h-bar hold of a confirmed prior swing high after >= 2 rejections (`effort.py:46-60,285-370`).
- No Phase-E / mark-up / distribution-after-UT event beyond the above; `MAIN_KINDS` lists spring, sos, lps, ut, sow, lpsy_d, tr (`stage0/runner.py:113-122`).

## Inputs / CFD-specific bits

- Timeframes D1/H4/H1/M15 (`data/bars.py:30-32`; `stage1/data.py:32`); bars aggregated from M1 with columns `time, close_time, server_open, server_day, open/high/low/close (bid), vol_ftmo, vol_duka, n_minutes, src_duka_share, holiday, holiday_lowvol, news` (`bars.py:72-115`).
- Volume is tick count only: `vol_ftmo` = MT5 `tick_volume` (2014+), `vol_duka` = Dukascopy quote-update count (2019-12+), never spliced across feeds; raw `ticks` of `XAUUSD.zip` is forbidden (`CLAUDE.md:10`; `data/store.py:3-6,30,42`).
- Detectors only ever use RV ratios / AE z-scores, never raw volume across bars (`features/volume.py:1-13`; `ranges.py:117-119`). RV = V / trailing 20-bar median, excluding holidays (`features/volume.py:30-34`); AE = residual of log V on log(range/ATR) fitted on the previous 250 bars (`volume.py:47-71`).
- Prices are bid OHLC rounded to `TICK = 0.01`; ATR = Wilder(14) through i-1 for normalisation (`base.py:19-20,95-106,160`).
- Session: server day 01:00-24:00, `SESSION_MINUTES = 1380` = CME metals 18:00-17:00 New York; the D1 bar closes at 17:00 NY (`data/calendar.py:35-37`; `tests/test_foundation.py:43`). Holidays = CME rule calendar + short-session/missing-minute rule + England & Wales bank holidays for low-volume-only flags (`calendar.py:1-18`; `bars.py:35-70`).
- Spread/pip: no spread model inside detectors; Stage 1 carries FTMO point 0.01 USD, spread in USD/oz per server hour, commission 0.0007%/side, swap % per night (`stage1/costs.py:20-24,50-58`). Symbol constant `SYMBOL = "XAUUSD"` (`config.py:13`).
- Two feeds (FTMO selected, Dukascopy robustness) run in parallel on every event (`base.py:31-33,186-194`). Holdout 2023-01-01..2026-09-30 is guarded in the loader (`CLAUDE.md:8`; `config.py:18`).

Real volume is NOT available: everything is tick-count based (MT5 tick_volume and Dukascopy quote counts only).

## Timing / lookahead (quoted)

- `detectors/base.py:5-8`: "A detector reads candle i only through arrays indexed <= i, and only for decisions made at the close of i ... known_at (the candle whose close makes it knowable, >= idx)."
- `pivots.py:3-4`: "Pivot high at p (confirmation lag k) ... It is known at p+k." Emitted with `known_at=p+lag` (`pivots.py:21-25`), lag=3.
- `ranges.py:14-16`: "A range's bounds are frozen when it becomes known (the spec's optional 'widen on later pivots' is not implemented ...)". `_close_range` does scan to `len(k)` (`ranges.py:48-61`), so `end_idx`/`end_reason` are future-dependent; detectors must not read them: `accumulation.py:23-25` and `distribution.py:36-38` "read only through r.active_at(i) at the bar being decided (never r.end_idx ...)".
- AR event: `idx=a_idx` (the AR high) but `known_at=i` where the pivot/retrace confirms (`ranges.py:229-235`). ST event `idx=u`, `known_at=i`, and the range's `known_at=i` (`ranges.py:273-283`).
- SC/BC fires on the climax bar itself, no confirmation lag: `Event(names[0], i, i, ...)` (`ranges.py:187-189`).
- Spring: `accumulation.py:229` "B1: the FIRST close back above TR_lo decides"; event known at that recovery close (`accumulation.py:279`). `no_recovery` known at b+J (`accumulation.py:238`). Unresolved at the end of data emits nothing (`accumulation.py:235-237`).
- `sos` known at the aggregate's last bar t; `sos_failed` known at the failing close f (`accumulation.py:433,454`). `lps` `idx` = pullback low bar, `known_at` = trigger bar (`accumulation.py:511`).
- UT: `ut` known at the fall-back close `end` (`distribution.py:248-254`); unresolved => "nothing known yet" (`distribution.py:206-210`). `utad_confirmed` uses a pivot confirmed lag later, then a close (`distribution.py:20-22,262-287`). "Pivots are used only once confirmed (pivot idx + lag <= the deciding bar)" (`distribution.py:43-44`).
- Anti-repaint tests assert truncated reruns equal the full run: `tests/test_accumulation.py:168-178` and the review tests "a different future never changes known events" (`test_accumulation.py:549-613`).

## Tests

17 `.py` files, ~5,079 lines under `tests/`. Coverage: causality of RV/AE/ATR/pivots/prepare (`test_foundation.py:17-143`); Phase-A chain, B2 anchor ordering, rv_climax, truncation (`test_ranges.py:12-279`); all eight Phase C/D kinds, volume gates, truncation every 4 bars, adversarial future changes (`test_accumulation.py:183-1004`); UT/SOW/LPSY variants, gates, holiday vetoes (`test_distribution.py:124-279`); effort/choch (`test_effort.py`, 596 lines); range quality/S0-1/S0-2 (`test_quality.py:27-113`); volume de-seasonalisation (`test_volume.py`); bar building/calendar (`test_bars.py`, `test_calendar.py`); import-layer and no-env AST contracts (`test_contracts.py:1-52`); Stage 0 report/charts and Stage 1 costs/data/grid (`test_stage0*.py`, `test_stage1_*.py`). No test measures event profitability: Stage 0 is label-blind (`CLAUDE.md:9`).

## Reuse for US daily (OPINION)

1. The detectors are ATR/RV-normalised and have a D1 code path already (`base.py:140-142`), so ranges, springs, SOS, UT/SOW and LPS logic port to US daily bars almost unchanged.
2. Must replace `data/calendar.py` (CME-metals + UK + 1380-minute session rules), `news` (`usd_events.csv`) and `TICK=0.01` rounding with a US exchange calendar, equity news feed and per-symbol tick size.
3. Volume semantics change from tick counts to consolidated share volume (incl. after-hours prints); RV/AE still work but the 20-bar reference and the 0.7/1.5 thresholds need re-calibration.
4. `stage1/costs.py` (spread/commission/swap in USD/oz) is XAUUSD-specific and must be swapped for US commissions, slippage and borrow.
5. The D1 bar boundary (server 01:00-24:00 closing 17:00 NY) must become the 09:30-16:00 ET session or a 24h print window before daily ranges are comparable.

## Open questions

- SC/BC, `ut` and `sow` fire on the same bar as the climax/fall-back close, with no extra confirmation lag; whether that is acceptable for live execution is NOT VERIFIED in the docs read.
- `distribution.py:65` says "accumulation.springs defaults to b_min_phase_a=False", but `accumulation.py:173` has `True`: doc/code drift, intent NOT VERIFIED.
- Wyckoff Phases A-E as named phases are not emitted as such; only the event set above exists. Whether a phase labeller lives elsewhere in the repo is NOT VERIFIED.
- Thresholds (0.7/1.5 RV, CLV 0.5/0.7, B_min=10) were calibrated on XAUUSD H1/D1; transferability to US equities is NOT VERIFIED.
- No performance/outcome evidence is available in-repo (Stage 0 label-blind), so event quality on any market is unknown.