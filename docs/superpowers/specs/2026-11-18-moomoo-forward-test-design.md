# Step 4a: Forward test of the research leads on moomoo SIMULATE

Status: v2 approved by the user (2026-11-18: "Looks good to me. You may proceed."); section 12 holds the user's
answers. Approach A chosen by the user ("go with A then. As detail as
you can to prevent illusion and mistake"). v2 follows a builder check (pi worker, 2026-11-18) that found six places
where v1 contradicted the code; section 15 lists them and what changed.

## 1. Intent

The final-year test (docs/research/2026-11-18-holdout.md) passed no variant. Two leads came close:

| Lead | Final-year CAGR | Sharpe | MDD | Random pct | Trades | DSR | Failed checks |
|---|---|---|---|---|---|---|---|
| H2-spring | 27.1% | 2.28 | -5.6% | 96% | 74 | 0.837 | trades ≥ 100, DSR ≥ 0.9 |
| H6-H4-3B | 34.0% | 1.69 | -13.1% | 98% | 120 | 0.651 | DSR ≥ 0.9 |

They were picked **after** looking at the final year, so the final year can no longer judge them. The only clean
test left is data that did not exist when they were picked. Step 4a runs both leads forward every trading day on the
moomoo **simulated** account, with exactly the rules they were tested with, so that after enough months their
forward results can be put next to the research numbers and believed.

Success for this build: a forward record that a skeptic cannot explain away by a rule mismatch, a data mismatch, a
cherry-picked window or a quiet change of code. Making money is not the success criterion of the build; an honest
record is.

## 2. Goals, non-goals, anti-goals

Goals
- G1. The research rules decide every trade: the unchanged backtest engine, strategy, composer, signals and engines.
- G2. Every decision can be reproduced from saved inputs, and is checked every night against a normal backtest run
  over the forward window.
- G3. Three numbers are kept apart: the research result on forward data (engine book), what the account did (live
  book), and the gap between them (execution).
- G4. The test is registered before day 1. Any change afterwards starts a new registration and a new clock.
- G5. REAL money is impossible from this code path.

Non-goals
- No new signals, filters, guards or parameter changes. ZenTrade's own guards (earnings guard, entry guard, circuit
  breaker, opportunity cards, target zones, 7-day time stop) are not used: they would change the strategy.
- No UI: a CLI, a daily log and a monthly markdown report.
- No Alpaca removal (step 4b, section 13). ZenTrade's own swing trader and its Alpaca mirror are untouched.
- No LLM veto (step 5). Only a hook is reserved (section 11).

Anti-goals (failures even if the code "works")
- A1. Forward numbers come from rules that differ from the research rules.
- A2. A decision or report uses information not available at that moment.
- A3. Anyone, including Claude, tweaks a rule mid-test and the record continues.
- A4. An interim result is presented as the verdict.
- A5. Missed days, failed orders or data gaps silently disappear.

## 3. The research rules, as the code runs them (verified by reading code, 2026-11-18)

Path: `app/research/evaluate.run_table` → backtest service, strategy `research_signal_table`, `mode="position"` →
`_simulate_portfolio_matrix` (`backtest/strategy.py:1640-1652`, `backtest/engine.py` from about line 1900).

Config: `max_positions=10`, `position_sizing="equal"`, `entry_fill="open_t+1"`, `exit_fill="open_t+1"`,
`slippage_bps=5` (stress 15), cost model `us_per_share_v1` ($0.005/share, $1 minimum), `initial_capital=$1,000,000`.

Rules:
1. **Signals and stops come from the composed table** (`research/compose.py`): an entry is kept only if the next
   bar exists and its open is above the invalidation (`compose.py:144-150`); the exit flag on day t is
   `close < active_invalidation` or a sell signal; `active_invalidation` is forward-filled from the latest **kept**
   entry for that symbol, including later signals on a symbol already held, so a held position's stop can move,
   up or down.
2. **Exit at the next open** for an exit flag, and at 20 held bars (`MAX_HOLD_DAYS = 20`,
   `research_signal_table.py:40`).
3. **−15% stop during the day** (`STOP_LOSS = -0.15`, `research_signal_table.py:39`; `engine.py:2055-2075`): if
   the open is at or below entry × 0.85, sell at the open; else if the day's low touches it, sell **at exactly the
   stop price**. Proceeds of an intraday sale are not usable for entries that day (`engine.py:1934`).
4. **Entries at the next open**: candidates are the day's kept entries, minus symbols held and symbols sold that day
   (`engine.py:2104-2111`), ranked by score, then 20-day average dollar volume, then prior dollar volume. A gapped
   candidate never reaches the engine, so the next one moves up.
5. **Sizing compounds**: each new position gets about `equity at the open / 10`, in whole shares including costs
   (`engine.py:2139-2165`).
6. **End of run**: on the last day of any run every position is sold with reason `end`.

Universe: ≥ 250 bars, raw close ≥ $5, 20-day median dollar volume ≥ $10M (`research/universe.py`), over
`tradable_symbols`, which reads today's `instruments/instruments.parquet` (`research/bars.py:60-74`), so delisted
names are missing (survivorship, stated in every research report). H6 adds SPY above its 200-day average.

Research gate (`research/evaluate.py:257-263`): exactly five checks: CAGR above same-universe equal-weight
buy-and-hold; CAGR above the 95th percentile of random entry; ≥ 100 trades; average R > 0; DSR ≥ 0.9 with the trial
count (`n_trials = 71`, frozen).

Data: `research/bars.load_bars` clips at `RESEARCH_LAST_DATE = 2026-11-13` (`research/__init__.py:12`), so the
research loader cannot read forward data. Prices are forward-adjusted with today's factors; the daily pipeline
(`jobs/daily_pipeline.py`) syncs bars and factors and reports `affected_symbols` when a factor changes.

Engines: Chan points come from `research/chan_replay.replay_symbol` (whole-history replay, resumable per symbol,
no per-day mode); Wyckoff has the same shape. The frozen registry pins `chan_version`, the Wyckoff version,
`variant_configs`, `bridge_hash`.

Moomoo (R&D report, section "Moomoo OpenD findings"; **docs, not tested by us**): SIMULATE US takes limit and market
orders only, DAY only, fills at best bid/ask; no fill push or deal query on SIMULATE, fills come from order updates
(`dealt_qty`, `dealt_avg_price`); `place_order` 15 per 30 s, modify/cancel 20 per 30 s, order queries 10 per 30 s;
REAL needs `unlock_trade`, SIMULATE does not. Stop orders are not listed for SIMULATE. Pre/after-market support is
unclear. `remark` behaviour is unverified.

## 4. Approaches

- **A (chosen by the user): faithful daily runner** on SIMULATE with the research rules.
- B (rejected): route signals through ZenTrade's SwingTrader; its exit rules are a different strategy.
- Inside A, how tomorrow's trades are decided:
  - v1 planned a small planner that re-implements the engine's rules. The builder check showed four rules it got
    wrong (−15% stop, sizing, gap promotion, a stop that moves at the open). Re-implementing the engine is where
    mismatches come from.
  - **v2 (chosen, Claude's call): the engine itself decides, every morning.** At 09:30 ET the runner builds the
    real opening price of every relevant symbol into one extra "open-only" bar for today (open = high = low = close
    = the official open), then runs the **unchanged** research backtest over the forward window ending today. The
    engine's actions dated today (exits at the open, at-open stops, entries with share counts) are the orders.
    Because the engine sees the real open, it applies the gap rule, promotes the next candidate, resolves the moving
    stop and sizes from equity at the open exactly as in research. Its `end` sales on that extra bar are artifacts
    and are ignored.

## 5. Daily timeline (US Eastern time, calendar from `app.market_clock.us_clock`)

| Time (ET) | Step | Output |
|---|---|---|
| ≥ 18:00 on day t | N1 data: wait for final daily bars for t (SPY, every held symbol, every candidate, ≥ 98% of the eligible universe); deadline 07:00 on t+1 | readiness record |
| after N1 | N2 signals: extend Chan and Wyckoff points to day t | points to t |
| after N2 | N3 prepare: compose the tables through t; list "relevant symbols" (held + day-t candidates + SPY); save the night record | night record |
| after N3 | N4 parity: full backtest over the forward window through t on real bars; compare with the books | parity record |
| 09:30-09:35 on t+1 | M1 open: read the official open of each relevant symbol | open record |
| after M1 | M2 decide: build the open-only bar, run the engine, extract today's actions | decision record |
| after M2, by 09:40 | M3 orders: exits first, then entries | order journal |
| until 10:00 | M4 poll: read order status; cancel what is still open at 10:00 | fills |
| 09:30-16:00 | S1 stop watch: for each live position, sell at market when the last trade ≤ entry × 0.85 | stop orders |
| 16:15 | R1 reconcile: broker positions and cash against the live book | reconcile record |

If N1 misses 07:00, or N2/N3 fails, the morning still runs M1-M2 for **exits and stops of held positions** (they need
no new signals), but takes no entries; the day is recorded `no_plan` with the reason. Nothing is guessed.

## 6. Components

New code only in `backend/app/forward/` and `backend/scripts/forward/`. **No research or engine file is edited.**

### 6.1 Registration (frozen before day 1)

`<data>/forward/registration.json`, written once by `forward register`, read-only afterwards, sha256 in every report:
- per strategy: `variant_id`, full `variant_config`, the backtest config above with `initial_capital` =
  the strategy's capital (section 12, Q2), slippage 5 bps, costs as research;
- pins: `chan_version`, Wyckoff version, `bridge_hash`, sha256 of `compose.py`, `hypotheses.py`, `universe.py`,
  `ranking.py`, `research_signal_table.py`, `backtest/engine.py`, `backtest/strategy.py`, the frozen registry file,
  and the `app/forward/` code; computed with the same functions the freeze used;
- `start_date`, `evaluation` (section 9) in full, the SIMULATE account id, and "REAL is never used".

Every step recomputes the pins first. **Any difference stops the runner before it decides or sends anything**,
naming the pin. A rule change needs `forward register --supersede <reason>`, which closes the old record and starts
a new one with a new clock; records are never merged.

### 6.2 Data

- **Forward loader** (`app/forward/bars.py`): the research loader without the `RESEARCH_LAST_DATE` clip. A test
  requires byte-identical frames to `research.bars.load_bars` for any range ending on or before 2026-11-13.
- **Universe snapshots**: each night the runner appends today's tradable list to
  `<data>/forward/universe/<date>.parquet`. The forward universe for day d is the snapshot of day d, never today's
  list, so a stock that delists later stays in the forward record. Bars of any symbol that was ever in a forward
  snapshot are kept: the builder checks what the daily pipeline prunes (`_prune_stale_price_partitions` and
  friends) and, if it would delete them, the runner keeps its own copy under `<data>/forward/bars/`.
- **Readiness** (N1): bars for t must exist for SPY, every held symbol and every day-t candidate; ≥ 98% of the
  eligible universe; the run starts no earlier than 18:00 ET. The time the bars became complete is recorded so the
  dry run can measure it.
- **History changes**: a per-symbol hash of bars before t is saved each night; a change (adjustment event, revised
  bar) is recorded and triggers N2's full re-replay for that symbol.

### 6.3 Signals (N2)

Requirement: points through t equal what a fresh full replay over bars through t produces.
- Incremental extension in a new module, persisting whatever per-symbol replay state is needed, without editing
  `chan_replay.py` or the Wyckoff modules. If that is impossible, fall back to full re-replay of the needed symbols
  nightly, only if the measured time fits before 07:00 ET and the memory fits next to the user's other jobs
  (15.2 GB). The builder measures and reports both numbers before choosing.
- Full re-replay of a symbol whenever its history before t changed (6.2).
- Tests: on fixtures, day-by-day extension equals a fresh replay (byte-identical). Weekly on real data, 20 random
  universe symbols plus every held symbol are replayed fresh; any difference stops new entries.

### 6.4 Decision (M1-M2): the engine decides

1. **M1.** For each relevant symbol, read the official opening price of t+1 (moomoo snapshot `open_price`, polled
   09:30-09:35). A symbol with no open by 09:35 gets no open-only bar; the engine then treats it as not tradable
   that day, exactly as it treats a missing bar.
2. **M2.** Build an engine input = the forward store through t + one open-only bar for t+1 for the relevant
   symbols (open = high = low = close = official open; volume = the symbol's volume on t, so the engine does not
   mark it suspended; the builder confirms which fields the engine reads for tradability). Run the research
   backtest (same config as research, window from `start_date`) on this input.
   - Speed: the run must finish by 09:40. The engine only needs the forward universe's symbols that ever had a
     signal plus SPY; the builder may restrict the input to those, **only after a test shows identical trades and
     equity with and without the restriction** over the final year.
   - The input never touches the real store: it is built in a temp directory per day.
3. Extract the engine's actions dated t+1: exits at the open (reason exit flag, max hold, at-open stop) and entries
   (symbol, shares, entry price). Ignore `end` sales on t+1. Save the decision record: input hashes, opens used,
   the engine's actions, its equity, pins. The record is enough to re-run M2 offline and get the same actions.
4. The live orders use **the engine's share counts**. Engine and live therefore hold the same shares; they differ
   only in fill prices and in execution failures.

### 6.5 Execution (M3-M4, S1)

- **M3 orders.** Market DAY orders, exits first, then entries in the engine's order. At most one `place_order`
  every 2.5 s (≤ 12 per 30 s, under 15). Exit quantity = this strategy's live lot quantity, never the whole broker
  position.
- **Idempotency.** Each order gets a deterministic key `fw-<strategy-short>-<yyyymmdd>-<symbol>-<B|S>` (suffix
  `-r1` for a one-time retry), sent as `remark`. Before `place_order` the runner appends `submitting` with the key to
  the order journal (append-only JSONL, fsync). After a crash or timeout it **never re-sends** a key in
  `submitting`: it reads the day's orders and matches by remark; if no match can be made, the key becomes `unknown`
  and the user decides. The dry run verifies the remark round trip and length limit before the first real order;
  if it fails, the design comes back for a change.
- **M4 poll.** Order list at most every 3.5 s (≤ 9 per 30 s) until every order is final or 10:00. Fills come from
  `dealt_qty` and `dealt_avg_price`. At 10:00 remaining open orders are cancelled. A partial entry keeps what
  filled; a partial exit sends the remainder once (`-r1`); anything still unsold is `exit_incomplete` and goes at the
  next open.
- **S1 stop watch.** For each live position, subscribe to quotes; when the last trade ≤ entry price × 0.85 (entry
  price = the engine's entry price, so engine and live use the same line), send one market sell. The research
  assumes a fill at exactly the stop price; live fills will usually be lower. The difference is reported, never
  hidden. If the watch is down (OpenD or PC off), a stop that the engine later shows is recorded `stop_missed` and the
  position is sold at the next open.
- **Missed open.** If M3 has not started by 10:00, entries are dropped as `missed_open` and exits roll to the next
  open as `exit_delayed`. No catch-up buying later in the day.

### 6.6 Books and reconciliation

- **Engine book**: the research backtest over the forward window on real bars (from N4). This is the research
  result on forward data and the basis of the verdict.
- **Live book**: lots per strategy (symbol, shares, entry date, fill price, key), cash = capital − entries + exits,
  rebuilt from the order journal at every start and compared with the saved book; a difference stops the runner.
- When a live entry fails, the live book simply lacks that lot; when the engine later exits it, there is nothing to
  sell. When a live exit fails, the live book holds the lot until it is sold. The engine book never changes because
  of live failures, so one failure cannot cascade into different decisions.
- One SIMULATE account holds both strategies; each sells only its own lots.
- R1 at 16:15: broker quantity per symbol = sum of live lots; broker cash ≥ 0 and no margin used.
- Share changes: a quantity difference with an adjustment event for that symbol scales the lots by the event ratio
  (recorded). Any other difference: no new entries in that symbol, `reconcile_mismatch` recorded, exits capped at the
  broker quantity.
- Delisting or cash merger: lot closed at the broker's cash amount as `corporate_exit`.

### 6.7 Parity guards

- **Night parity (N4).** Run the normal research backtest over the forward window through t on real bars (no
  open-only bar). Its trades through the open of t (symbol, entry date, exit date, exit reason, **shares**, entry and
  exit price) must equal the engine actions saved by the morning decisions, except:
  - intraday −15% stops, which only the full bar can show; they are compared with S1's triggers separately;
  - `data_revision`: the decision's input hash for that date differs from today's data for that date (recorded,
    no halt).
  Anything else is `logic_mismatch`: **new entries stop** until the user looks; exits and stops continue.
- **Historical rehearsal (before the dry run, the main proof).** Run the whole morning procedure (M1-M2 with
  open-only bars built from the stored real opens) for every day of the final year, using stored points with
  `known_date ≤ d`, and require the resulting trades to equal the official final-year trades of each lead
  (H2-spring 74 trades, H6-H4-3B 120 trades), share for share, except intraday stops, which must match the bars'
  lows. This proves the procedure reproduces the research before it touches the account.
- **Replay parity (weekly)**: 6.3. **Pin check (every step)**: 6.1.

### 6.8 Safety

- One function creates the trade context, hard-codes `TrdEnv.SIMULATE` and the registered account id, and checks
  via the account list that the id is a SIMULATE account before every session.
- `app/forward/` never contains `unlock_trade` or `TrdEnv.REAL`; a test greps for both.
- `forward stop` writes a kill flag, checked before every order. Starts, stops and overrides go to `audit.jsonl`.
- Caps: ≤ 25 orders per strategy per day; an entry's notional ≤ 1.1 × (engine equity / 10); a strategy's open
  notional ≤ its live equity. A cap hit refuses the order and records why.
- Claude never runs the live runner. Tests use a fake trade context. The first command that talks to the account is
  run by the user (section 10).

### 6.9 Records and reports

`<data>/forward/`: `registration.json`, `audit.jsonl`, `universe/`, and per strategy `night/<date>.json`,
`decision/<date>.json`, `orders.jsonl`, `book.json`, `parity/<date>.json`, `reconcile/<date>.json`, plus
`days.jsonl` with one line per trading day per strategy (status and reason). A missing day is itself an error in the
report.

`forward report --month YYYY-MM` writes `docs/forward/<yyyy-mm>.md`. Contents:
- bold "INTERIM, not a verdict" until the evaluation date;
- per strategy: engine book and live book side by side; the final-year research numbers for reference only;
- the research baselines over the **same forward window** with the same engine: equal-weight buy-and-hold of the
  forward universe, random-entry percentile; SPY for information;
- execution: live fill vs official open in bps (median, p90) per side, and live stop fill vs stop price;
- counts by status (`no_plan`, `missed_open`, `exit_delayed`, `stop_missed`, `data_revision`, `logic_mismatch`);
- the registration sha256 and pins.

### 6.10 CLI

`scripts/forward/run.py`: `rehearse`, `register`, `flatten --confirm`, `dry-run`, `daemon`, `night --date`, `morning`, `reconcile`,
`parity`, `status [--check-account]`, `stop`, `report`. The daemon runs section 5 and is started by the user in an
Orca terminal; it is not in the web app's scheduler, so the web app can restart without touching orders.

## 7. Illusion and mistake register

| # | Risk | Guard | Test |
|---|---|---|---|
| 1 | Rules differ from research | The unchanged engine decides; no re-implemented rule | rehearsal over the final year |
| 2 | Day-t entries lost (no next bar) | open-only bar gives the engine its next open | rehearsal; unit test with a last-day entry |
| 3 | Moving stop resolved wrongly | engine resolves it with the real open | rehearsal day with a held-symbol signal |
| 4 | Gapped top candidate leaves a slot empty | engine promotes as in research | unit test: top candidate gaps |
| 5 | Sizing differs | engine's share counts are the orders | night parity on shares |
| 6 | `end` sales on the open-only bar sent as orders | filtered by date and reason | unit test |
| 7 | −15% intraday stop missing or at a better price than live | S1 watch; stop fill gap reported | fake-quote tests; report test |
| 8 | Incremental replay ≠ fresh replay | 6.3 | fixture + weekly real check |
| 9 | Adjustment event, old points in wrong price space | full re-replay on history change | fixture with a factor event |
| 10 | Bar not final at decision time | N1 waits until 18:00 and checks completeness | missing bar → `no_plan` |
| 11 | Forward loader differs from research loader | identical-frame test | unit test |
| 12 | Survivorship creeps into the forward record | daily universe snapshots; bars kept | test: delisted symbol stays in a later rerun |
| 13 | Code changed mid-test | pin check every step | changed pin → runner refuses |
| 14 | Duplicate order after a crash | journal before submit, match by remark | fake-context crash tests |
| 15 | One failure cascades into different decisions | engine book independent of live book | test: failed entry, later exit |
| 16 | Two strategies sell each other's shares | lots per strategy | test with both holding one symbol |
| 17 | Missed days vanish | `days.jsonl` completeness | report test |
| 18 | Interim read as verdict | report header; evaluation fixed in registration | report test |
| 19 | Pass bar moved | exact research gate, N = 71 | registration test |
| 20 | Costs understated | live bps vs 5 and 15 bps; stop fill gap | report test |
| 21 | REAL reached | SIMULATE-only constructor, grep test, account-type check | unit tests |

## 8. Testing rules

No OpenD, no network, no real data dir in unit tests (fixtures, temp dirs). The rehearsal (6.7) reads the real store
read-only and writes only to a temp dir; it is a script the user starts, not a unit test. A fake trade context
supports account list, place order, order list, cancel, quotes, and injects delays, partial fills, rejections,
crashes between submit and journal, and remark truncation. `ruff` clean; the 259 research tests stay green; a test
compares the hashes of the pinned files with the registration fixture.

## 9. Evaluation (written into the registration before day 1)

- Verdict book: the engine book. The live book shows execution on top. Final-year numbers are shown for reference
  and never pooled with forward data.
- Interim: monthly, labelled interim. No change, stop or go-real decision on an interim report, except stopping for a
  bug or a safety problem.
- Verdict date: 12 months after `start_date`, extended until the engine book has ≥ 100 closed trades (H2-spring made
  74 in the final year, so about 16 months).
- Pass, per strategy, on the forward window: **the research gate unchanged**: CAGR above same-universe equal-weight
  buy-and-hold (forward universe snapshots), CAGR above the 95th percentile of random entry, ≥ 100 trades, average R
  > 0, DSR ≥ 0.9 with N = 71. SPY is reported for information only.
- Also reported: the same gate on the live book, and on the engine book with the live median costs if they exceed
  15 bps per side.

## 10. Rollout

1. Build with unit tests only (fake trade context). No account access.
2. **Rehearsal** over the final year (6.7). It must reproduce both leads' official trades. Any difference is fixed
   before going on.
3. **User-run read-only check**: `forward status --check-account` shows the SIMULATE account's type, cash and
   positions.
3b. **Flatten**: if the account holds positions or open orders, `forward flatten --confirm` cancels the open orders
   and sells every position at market during regular hours through the same SIMULATE-only context, pacing and
   journal as M3 (keys `fw-flatten-<yyyymmdd>-<symbol>`), then re-reads the account until it is flat. The user
   approved closing pre-existing positions; the command still prints the list and needs `--confirm`.
4. **Dry run, 10 trading days**: the daemon runs everything except sending orders (S1 watches and logs what it
   would send). It must produce a decision every morning and 0 `logic_mismatch`. On one day the user runs one 1-share
   test order and cancel with a test remark, to verify the remark round trip, the order status fields and the fill
   read path. The dry run also measures the bar readiness time and the morning decision time.
5. `forward register` with the evaluation section. Day 1 is the next trading day.
6. Go live.

## 11. Hook for step 5 (reserved, not built)

Between M2 and M3: `review(actions) -> actions`, returning the actions unchanged for now. Step 5 puts the LLM veto
there and must record vetoed entries as shadow trades. Whether a vetoed slot goes to the next candidate is a step 5
decision; it changes the engine book, so step 5 will need its own registration.

## 12. Decisions by the user (2026-11-18)

- Q1. The −15% stop watch runs the whole US session: **yes, the PC stays on** (daemon and OpenD up 09:30-16:00 ET).
  A stop missed anyway is sold at the next open and recorded `stop_missed`.
- Q2. Capital: the user confirms the SIMULATE account holds **more than $1,000,000** (exact amount unknown). Rule:
  $1,000,000 per strategy if the account's cash is at least $2,000,000; otherwise each strategy gets half of the
  cash, rounded down to the nearest $10,000. Fixed at registration from the `--check-account` reading and written
  into the registration.
- Q3. The SIMULATE account is used only by this runner. **Positions already in it may be closed** (user: "if the
  simulated account has opening stock, you can close it"). Rollout step 3b does this with the runner's own guarded
  SIMULATE code, never ad hoc, and records every order.

## 13. Out of scope, next

- Step 4b, Alpaca removal: Alpaca is in 60+ files (mirror, options, shortable list, IEX quote cross-check in the
  trading loop, market calendar). It changes ZenTrade's own swing trader. Separate design after 4a.
- Step 5, LLM veto: separate design, using the hook in section 11.

## 14. Left to the builder

- How incremental Chan/Wyckoff replay keeps state (6.3), within "no edits to pinned files".
- How the open-only bar enters the engine input without editing pinned files (a temp store directory is the default).
- Record field names, daemon internals (sleep/wake, retries), as long as section 5's deadlines hold.

Assumed, not verified (each is measured or tested in the dry run): final bars for t by 18:00 ET; snapshot
`open_price` is the official open and appears within 5 minutes; the morning engine run finishes in under 10 minutes;
SIMULATE fills market orders fully; `remark` survives the round trip; SIMULATE has enough cash.

## 15. Builder check (2026-11-18) and what changed from v1

| v1 claim | Code | v2 |
|---|---|---|
| Exits: composed flag + 20-day hold | also a −15% intraday stop at the exact stop price (`research_signal_table.py:39`, `engine.py:2055-2075`) | rule 3, S1 stop watch, Q1 |
| Sizing: initial capital / 10, fixed | equity at the open / 10 on the research path (`engine.py:2139`) | engine's share counts are the orders |
| Pass bar "same as research" with SPY and N = 2 | five checks, no SPY, N = 71 (`evaluate.py:257-263`) | research gate unchanged, N = 71 |
| Nightly stop recompute | a day-t signal on a held symbol moves the stop only if kept at the next open | engine decides at the open |
| Plan up to free slots, gap check later | gapped candidate removed before ranking; same-day re-entry refused | engine decides at the open |
| "Forward has no survivorship" | universe = today's instrument list (`bars.py:60-74`) | daily universe snapshots, bars kept |
| (not in v1) | research loader clipped at 2026-11-13 | forward loader with identical-frame test |
