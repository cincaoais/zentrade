# 缠论 + Wyckoff test harness — design (step 3 of 5)

Date: 2026-11-17. Status: DRAFT, builder-checked (answers folded in, see the last section). Waiting for the user's approval.
No product code before approval.

## Intent and why

The user trades Wyckoff and 缠论 (Chan) separately by hand. They want to know whether the two, alone or
combined, give an edge on US stocks, proven without hindsight, before anything trades on the moomoo
simulated account (step 4) or gets an LLM veto (step 5). The user's priority (2026-11-17): **trustworthy
trading results over compute speed** ("I want the best one. I care more about the performance").

Why the bar is high:
- Public 缠论 backtests collapse once signals are dated when they became knowable. JY-lc/chanlun-postmortem
  went from 20.65% to 2.05%, about buy-and-hold (R&D report
  `docs/rnd/2026-11-16-chan-wyckoff/rnd-chan-wyckoff-moomoo-2026-11-16.md`).
- No profitable automated Wyckoff study was found.
- The user's own XAUUSD work tested 369 versions and kept none (`D:/Desktop/trading/rnd/kb/DECISIONS.md:150`).

So the harness must report "no edge" just as honestly as "edge".

## Decided by the user

- Approach **A** for the harness: a day-by-day replay engine writes a dated signal table, and ZenTrade's own
  backtester trades it (2026-11-16).
- Replay method **A, exact replay**: the user's DSA 缠论 engine is called once per stock per trading day,
  on bars up to that day only (2026-11-17).
- The hypothesis list B0, H1–H6 (L1 later, forward only) and the pass gate (approved 2026-11-16):
  on the untouched final year, after costs, beat buy-and-hold of the same stocks and random entry,
  ≥ 100 trades, positive average R, deflated Sharpe ≥ 0.9.
- Data: moomoo via ZenTrade's store (step 2, done).
- Budget: at most 24 variants, counted.

## Goals

1. A dated **Chan signal table** for every stock that ever enters the universe, using the user's engine
   unchanged.
2. A dated **Wyckoff event table**, from the user's own `wyk` detectors ported to US daily bars with real volume.
3. **Hypothesis composer**: the tables become one entry/exit table per hypothesis variant.
4. **Bridge strategy**: one ZenTrade `matrix_native` strategy that reads a variant's table, so trades use
   ZenTrade's fills, costs and exits.
5. **Evaluation runner**:
   - development and holdout runs;
   - baselines;
   - metrics, including the deflated Sharpe ratio (DSR) with N = variants tried;
   - the pass gate;
   - a written report.
6. **Integrity checks** that run in CI or the runner:
   - truncation test (a past signal never changes when later bars are added);
   - phantom rate (Chan points that later disappear);
   - holdout lock.

## Non-goals

- LLM veto (step 5), live or simulated orders (step 4), UI pages, minute data.
- Multi-level Chan (weekly direction + daily operation). v1 is daily only; weekly is a possible later variant
  inside the 24 budget.
- Short selling. v1 is long-only; sell points are used only as exits.
- Fixing survivorship bias. Moomoo lists no delisted stocks. It is reported, not fixed.
- Speed optimisation (replay method B). It may come later, proven equal to A.

## Anti-goals (a failure even if it "works")

- Any signal, filter or universe rule reading data after its decision date. This includes:
  - today's liquidity, prices or split factors applied to past dates;
  - a Wyckoff range end (`end_idx`) used before it is known;
  - DSA `still_valid`/`state=invalidated` used as an entry filter. These are computed from bars after the point.
- Looking at holdout results before the variant list is frozen, or adding variants after a holdout run
  without counting them.
- Reporting a winner without baselines, costs and DSR next to it.
- Silently changing the user's engine rules. Any deviation is listed in the report.

## Constraints (verified unless marked)

| Fact | Evidence |
|---|---|
| Stored history: 2022-11-18 → 2026-11-13 (1,000 trading days), forward-adjusted enriched bars plus raw close; about 6,342 stocks once the backfill finishes | step 2 dry run + backfill log |
| DSA engine: `build_level_snapshot(df, "1d")`, batch-only, rebuilds `czsc.CZSC` on the full frame each call; points have `type, dt, price, invalidation_price, basis, state`; min 30 bars to attempt, 120 for `available`; only points within the last 9 strokes are reported | notes/s3-r1 (`engine.py:49,306,445-468,561-566`) |
| Engine speed: 0.042 s per snapshot at 1,000 bars; about 30 s per stock for a full daily replay (estimate) | measured 2026-11-17 |
| `still_valid` and `state` read bars after the point (`engine.py:448-465`) | notes/s3-r1 |
| czsc 1.0.2 (Apache-2.0), DSA engine MIT, ZenTrade MIT; the user's `wyk` repo has no licence file (own code) | checked |
| `wyk` detectors: ATR- and RV-normalised, with `known_at` discipline and truncation tests. Built on tick volume and the XAUUSD session/calendar; thresholds calibrated on XAUUSD | notes/s3-r3 |
| ZenTrade backtester: `matrix_native` strategies return entry/exit arrays. It has fills `open_t+1`, costs 0.005 $/share + $1 min + 5 bps slippage, stop / trailing / take-profit / `max_hold_days`, `max_positions`, and walk-forward with purge/embargo | notes/s3-r2 |
| Strategy files are AST-checked: allowed imports are polars, numpy and a few app modules; `pl.read_parquet` is allowed (not verified end to end) | notes/s3-r2 |
| Machine: 16 logical CPUs; earlier memory pressure seen when installing packages | `nproc`, session |

## Approaches considered (for the harness as a whole)

- **A. Replay engine → dated signal table → ZenTrade backtester (chosen).** Exact point-in-time signals, and
  trades use ZenTrade's tested execution.
- B. Signals computed inside a ZenTrade strategy over full arrays. Rejected: this is exactly the hindsight
  trap, and slow.
- C. A standalone backtester. Rejected: it duplicates costs/fills and its results wouldn't match what ZenTrade
  trades.

Replay method: **A (exact)** chosen over B (skip unchanged days, faster, needs a proof) and C (incremental
rewrite, risky).

## Design

### Package layout (new)

- `backend/app/research/`, a research package, not imported by the live app:
  - `vendor/dsa_chan/engine.py`: a **verbatim copy** of `daily_stock_analysis/src/chan/engine.py`, with the MIT
    notice and the source commit hash in its header.
  - `chan_replay.py`, `wyckoff/` (port), `universe.py`, `compose.py`, `evaluate.py`, `integrity.py`,
    `registry.py`.
- `backend/app/strategy/builtin/research_signal_table.py`: the bridge strategy.
- `backend/scripts/research/`: CLIs `replay_chan.py`, `replay_wyckoff.py`, `compose.py`, `evaluate.py`,
  `freeze.py`.
- Output data under `<data_dir>/research/<table>/<version>/…parquet`.
- Reports under `docs/research/YYYY-MM-DD-<run>.md`.
- New dependency: `czsc==1.0.2`.

### Inputs

Bars are read from ZenTrade's enriched store (forward-adjusted OHLC, split-adjusted volume, `raw_close`).

Forward adjustment uses today's factors for all history. That rescales past prices but leaks no direction,
and splits must be adjusted for structure to make sense. This is a known, accepted approximation and is
stated in the report.

### Universe (point-in-time)

On each trading day t, a stock is eligible if all of these hold:
- it has ≥ 250 stored bars up to t;
- its raw close at t is ≥ $5;
- its 20-day median dollar volume, using the **unadjusted `amount`** column (USD), days ≤ t, is ≥ $10 M.
  Split-adjusted `volume` must not be used here: its split factor is future information
  (`pipeline.py:282-286`).

The eligible set is capped at the top 1,500 by that dollar volume. The core ETFs are excluded from trading,
and SPY is kept as a reference.

Replay runs for the union of all stocks eligible on any day.

### Chan replay (`chan_replay.py`)

- For each stock and each trading day t from its 120th bar: call `build_level_snapshot(bars[:t+1], "1d")`
  and collect `buy_sell_points`.
- A point's identity is `(type, dt)`.
- **First-seen rows**: the first t at which an identity appears gives a row with:
  - `symbol, known_date = date[t], type, point_dt, price, invalidation_price, basis, state_at_known`;
  - `engine_version` = hash of the vendored file + czsc version.
- **Disappearance rows**: when a previously seen identity is absent from a later snapshot, record
  `gone_date`. This is used for the phantom rate only.
- `still_valid`/`state` at later dates are never used for entries.
- Parallel: `multiprocessing` pool. Worker count defaults to `min(12, cpu_count − 2)`, configurable; a lower
  default is kept because of the earlier memory pressure.
- Each stock's result is written as its own parquet part, so a crash resumes where it stopped.
- The table is cached by `engine_version`.

### Wyckoff replay (`wyckoff/`)

- Port the `wyk` detectors the event set needs: `pivots, ranges, accumulation, distribution, effort,
  quality`, plus `features/volume`, ATR and `base.Event`.
- Changes:
  - (a) volume = real share volume; RV = V / median of the previous 20 bars, AE as in `wyk`;
  - (b) the XAUUSD calendar, holiday and news flags, and tick rounding are removed; ZenTrade's trading days
    are used;
  - (c) thresholds are kept at the `wyk` defaults for v1, with no re-tuning (re-tuning would spend variant
    budget).
- Every event is emitted with `known_date = date[known_at]`, plus the range bounds as frozen at that point.
- Detectors must not read `end_idx`/`end_reason` (as in `wyk`).
- The `wyk` truncation tests are ported and must pass on US bars.

### Composer (`compose.py`) — hypothesis definitions (v1)

Common rules for every hypothesis:
- **Entry:** decided at the close of t, filled at the open of t+1 by ZenTrade.
- **Exit:** whichever comes first:
  - (a) close < the entry's invalidation price → exit at the next open;
  - (b) a same-level sell point (Chan) or SOW/UTAD (Wyckoff) known while holding;
  - (c) `max_hold_days = 20`;
  - (d) catastrophic `stop_loss_pct = −15%`.
- **R** = (exit − entry) / (entry − invalidation).
- The composer emits per-variant entry rows `(symbol, date, score=1, invalidation)` and exit rows.

| Id | Rule (entry at t) | Variants in v1 |
|---|---|---|
| B0-bh | equal-weight buy-and-hold of the universe members at period start, held to period end | 1 |
| B0-rand | random entries: same count per day as the tested variant, drawn from that day's universe, same exits; 200 seeds | per tested variant |
| B0-spy | SPY buy-and-hold | 1 |
| H1 | Chan point known at t: 2B, 3B, 1B | 3 |
| H2 | Wyckoff event known at t: spring (`entry_steady`), SOS, LPS | 3 |
| H3 | H1 entry, only if an accumulation TR is active at t with spring/SOS/LPS known in the last 20 bars | 2 (2B, 3B) |
| H4 | 3B, only if the breakout leg's mean RV ≥ 1.5 and the pullback's mean RV ≤ 0.8 (Wyckoff effort/result) | 1 |
| H5 | a Chan buy point and a Wyckoff buy event both known within 10 bars of each other, entry at the later one | 2 (window 5 / 10) |
| H6 | H3, H4, H5 with a regime filter: SPY close > SPY 200-day MA at t | 5 |

Plus **H1c**: 2B and 3B entered only at their `confirmed` date (2 variants), as a repaint-robustness check.

Total tested variants: 18, under the 24 budget, leaving 6 for follow-ups. Baselines don't count.

### Bridge strategy

`research_signal_table.py`:
- `matrix_native`.
- `params.table_path` points to a variant's composed parquet.
- `compute_signals` reads it with polars and sets `entry`/`exit` at `(timestamp, symbol)`.
- `MAX_HOLD_DAYS = 20`, `STOP_LOSS = −0.15`.

### Evaluation (`evaluate.py`)

- **Periods:**
  - development = first date with a full universe → **2025-11-13**;
  - holdout = **2025-11-14 → 2026-11-13** (about 250 trading days).
  - Development is also cut into walk-forward folds (126-day test windows, 5-bar embargo) to report stability.
- **Run config:**
  - `max_positions = 10`, equal sizing;
  - `entry_fill = exit_fill = open_t+1`;
  - default costs;
  - a cost-stress run at 15 bps slippage.
- **Metrics per variant:**
  - trades, win rate, average R, median R, CAGR, Sharpe, max drawdown, exposure;
  - percentile versus the 200 random-entry runs;
  - excess over B0-bh and SPY;
  - **DSR** (Bailey & López de Prado 2014) with N = variants tested so far (from the registry).
- **Pass gate (holdout, default costs).** All of:
  - CAGR > B0-bh;
  - CAGR above the **95th percentile** of the random-entry runs;
  - trades ≥ 100;
  - average R > 0;
  - DSR ≥ 0.9.

  Each must also hold at 15 bps slippage, or the report flags the variant "fragile to costs".
- **Holdout lock (`registry.py` + `freeze.py`):**
  - Every variant run is recorded in `research/registry.json` (id, params, code hash, date).
  - `freeze.py` writes `registry.frozen.json` with a hash.
  - `evaluate.py --holdout` refuses to run unless the frozen file exists and matches.
  - Each variant's holdout run happens once. A rerun is logged and counted.
- **Report:** a markdown table per variant and baselines, the integrity results, the survivorship and
  adjustment caveats, and the list of deviations from the engines.

### Integrity checks (`integrity.py`)

1. **Truncation test.** For 50 random stocks and 5 random cut dates each, a replay on `bars[:cut]` must
   produce exactly the first-seen rows with `known_date ≤ cut` from the full replay. Same for Wyckoff.
2. **Phantom rate.** The share of Chan first-seen points that later disappear, per type, reported.
3. **No-future-column check.** Composer inputs must not include `still_valid`, `state` (except
   `state_at_known`), `gone_date`, `end_idx`. Asserted in code.
4. **Determinism.** Rerunning the replay for 10 stocks gives byte-identical tables.

## Tests the builder must include

- Unit tests:
  - universe rule on synthetic bars;
  - first-seen/gone logic with a fake engine;
  - each composer rule on hand-made tables;
  - DSR against a published worked example;
  - registry/freeze refusal.
- Port tests: the `wyk` truncation and anti-repaint tests on synthetic US-style bars.
- An integration test on 5 stocks × 300 days in a temp data dir: replay → compose → bridge strategy backtest
  → report.
- The integrity checks above, run as part of `evaluate.py`.

## My calls (the user can override)

- Universe thresholds: $5 price, $10 M median dollar volume, top 1,500.
- 20-day max hold and −15% catastrophic stop. The prior KB note said canon has no time cap (`DECISIONS.md:9`);
  the cap is used here for swing-trading comparability.
- Long-only v1.
- The "beat random" gate means above the 95th percentile, not just above the median.
- `wyk` thresholds unchanged (no tuning).
- Daily level only.
- Forward-adjusted bars.

## Left to the builder

- File splits inside `research/`.
- Pool chunking.
- Parquet partitioning.
- The report layout.
- How `compute_signals` aligns symbols/dates.
- The CLI flags.

## Builder-check resolutions (2026-11-17; all my calls, the user can override)

Source: `docs/rnd/2026-11-16-harness/notes/s3-builder-check.md`. These override anything above that
conflicts with them.

1. **Random baseline (B0-rand).**
   - Entry count: the variant's **emitted** entries per day (before capacity), drawn from that day's
     eligible universe.
   - Invalidation: entry − *k*·ATR14, where *k* = the median invalidation distance in ATR of the variant's
     own entries.
   - Same exits otherwise (invalidation, 20-day cap, −15%), same `max_positions`, same costs.
   - 200 seeds. Gate = above the 95th percentile of the seeds' CAGR.
2. **DSR trial count N.**
   - N = every variant ever run on development (registry rows), including follow-ups, plus any holdout rerun.
   - Random seeds and baselines are excluded.
   - N is written into `registry.frozen.json` at freeze. A holdout rerun increments N and is shown in the
     report.
3. **R and invalidation.**
   - If the t+1 open is ≤ invalidation, the entry is skipped and logged ("gapped through").
   - R's denominator is floored at 0.5 × ATR14 at entry.
   - Invalidation by source:

     | Source | Invalidation |
     |---|---|
     | Chan | the engine's `invalidation_price` |
     | Spring (`entry_steady`) | its `stop_ref` |
     | LPS | its `stop_ref` |
     | SOS | TR_hi − 0.25 ATR (the `sos_failed` level) |

   - Composite hypotheses use the **higher** (tighter) of their members' invalidations.
4. **Buy-and-hold (B0-bh, B0-spy).**
   - Computed by `evaluate.py` directly, not as a ZenTrade run.
   - B0-bh: equal weight across the universe eligible on the period's first day. One entry and one exit
     cost with the same per-share commission and slippage. Names that later leave the universe are held.
   - The report also shows Sharpe and max drawdown next to CAGR, so cash drag is visible.
5. **Universe enforcement.**
   - `compose.py` keeps an entry only if its symbol is eligible at t.
   - Exits are always allowed.
   - The bridge strategy trusts the composed table.
6. **Phantom rate.**
   - A Chan identity counts as a phantom only if it vanishes on day d while its `point_dt` is still on or
     after the oldest stroke start in that day's `recent_bis`. In other words, it disappears while still
     inside the visible window; window exits don't count.
   - The replay records each day's visible-window start for this. It may slightly undercount, because the
     report window (9 strokes) is wider than `recent_bis` (7).
7. **Path-independent exits.**
   - The composer emits per symbol a running "active invalidation" = the invalidation of the **latest**
     entry signal for that variant, and an exit row whenever close < active invalidation or a sell event
     fires.
   - ZenTrade applies exits only to held names.
   - No re-entry while holding. The plan must verify the engine skips an entry for a symbol already held.
   - If a newer signal arrives while holding, its invalidation takes over ("newest structure governs").
8. **Freeze pins:**
   - enriched data generation id + last date;
   - universe parameters;
   - replay engine versions (Chan vendored-file hash + czsc version; Wyckoff port hash);
   - each composed table's hash;
   - bridge strategy file hash.

   `evaluate.py --holdout` re-checks all of them.
9. **Candidate vs confirmed.**
   - The replay also records `confirmed_date`: the first day the identity's `state` is `confirmed`.
   - Default entries use first-seen. The H1c variants test confirmed-date entries.
   - `state` is used only at the day it is read, never from the future.
10. **Survivorship.**
    - The moomoo stock list holds today's survivors only. This is the one accepted exception to the
      anti-goals.
    - Every baseline uses the same survivor universe, so the bias is shared, and it is stated in each report.
11. **Sell-point exits.**
    - Chan entries exit on any daily 1S/2S/3S first seen while holding.
    - Wyckoff entries exit on SOW, UTAD-confirmed or `sos_failed`.
    - Composite entries (H3–H6) exit on either family.

Data facts the checker couldn't see (they were in the step 2 work, not the cited notes):
- the 20-stock dry run gave 1,000 days, 2022-11-18 → 2026-11-13;
- the full backfill is writing to the real data dir now (log
  `D:/Desktop/rnd-scratch-2026-11-16/backfill/backfill.log`).
