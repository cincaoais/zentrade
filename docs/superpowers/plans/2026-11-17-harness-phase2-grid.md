# Research Harness Phase 2: Ranking Grid Implementation Plan

> Executed through Orca: pi and Antigravity workers implement one task each; the coordinator reviews and commits each task.

**Goal:** Add 53 pre-declared variants (signal set x ranking x slot count) to the 18 v1 variants, so the dev evaluation tests whether *which* signals fill the 10 slots matters, while every trial stays counted.

**Approved by the user (2026-11-17):** "ok go with it" for this exact grid. Nothing below may add, drop or tune a variant.

**Base plan:** `docs/superpowers/plans/2026-11-17-chan-wyckoff-harness.md` (its Global Constraints still apply: no OpenD, no network in tests, never write the real data dir from tests, no trading APIs).

## The grid (fixed before any run)

| Group | Bases | Ranks | Slots | Count |
|---|---|---|---|---|
| A | H1c-2B, H1-2B, H1c-3B, H1-3B, H1-1B | rs, retr, risk, relvol, lowvol | 10 | 25 |
| B | H5-w10, H2-sos | rs, risk, relvol, lowvol | 10 | 8 |
| C | H1c-2B, H1-2B | rs, retr, risk, relvol, lowvol | 5 and 20 | 20 |

Ids: `<base>~<rank>` for 10 slots, `<base>~<rank>~p<slots>` otherwise (e.g. `H1c-2B~rs`, `H1-2B~retr~p5`). The 18 v1 ids are unchanged and are `rank="hash", slots=10`. Total 71.

## Ranking definitions (decision date t; higher raw score = traded first)

All read bars with `date <= t` only. `RS_LOOKBACK = 63`, fixed, not tuned.

| rank | raw score | null when |
|---|---|---|
| `rs` | `close[t]/close[t-63] - spy_close[t]/spy_close[t-63]` (63 stored bars back, per series) | fewer than 64 bars, or SPY missing at t |
| `retr` | `-(range(leg_last) / range(leg_prev))`, range = max(high) - min(low) over the stroke's bars `[start, end]` inclusive; several points on one (symbol, t): keep the largest raw score | leg dates null, prev range 0, or no bars in a stroke |
| `risk` | `-(close[t] - invalidation) / atr14[t]` | atr14 null or 0 |
| `relvol` | `rel_volume[t]` (`features.rel_volume`, previous 20 bars) | fewer than 21 bars |
| `lowvol` | `-(atr14[t] / close[t])` | atr14 null |

`retr` exists only for Chan bases (group A, C). Its leg dates must all be `<= t`; any later leg date raises `ValueError` (a lookahead guard, not a filter).

## Final table score (composer)

The bridge casts `score` to float32 and orders descending, so the composer writes exact small integers: per decision date, kept entries sorted by `raw_score` descending (nulls last), ties and nulls by `entry_score(symbol, t)` descending; `score = n - position` (best = n, worst = 1). Entries without a `raw_score` column keep the v1 hash score unchanged (v1 tables must stay byte-identical).

## Interfaces

**Task 17 — `app/research/ranking.py` (new)**
- `RANKS = ("rs", "retr", "risk", "relvol", "lowvol")`, `RS_LOOKBACK = 63`
- `rank_features(bars: pl.DataFrame, spy: pl.DataFrame) -> pl.DataFrame` — columns `symbol, date, close, atr14, rs63, relvol, atrp`; `bars` may already carry `atr14` (reuse it).
- `chan_retracement(points: pl.DataFrame, bars: pl.DataFrame, types: tuple[str, ...], date_column: str) -> pl.DataFrame` — columns `symbol, date, retr_score` (date = the point's `date_column`, null dates dropped, one row per (symbol, date) keeping the max score).
- `raw_scores(entries: pl.DataFrame, rank: str, features: pl.DataFrame, retr: pl.DataFrame | None = None) -> pl.DataFrame` — returns `entries` plus `raw_score: Float64`; unknown rank or `retr` without a frame raises `ValueError`.

**Task 18 — `app/research/compose.py`**
- `Signals.entries` may carry an optional `raw_score` column. `compose_table` merges it per (symbol, date) with `max`, and writes the integer score above. No other behaviour changes.

**Task 19 — `app/research/hypotheses.py`, `scripts/research/compose.py`**
- `@dataclass(frozen=True) class VariantConfig: base: str; rank: str; slots: int`
- `VARIANT_CONFIGS: dict[str, VariantConfig]` (71 entries, v1 first in v1 order, then groups A, B, C in table order, bases then ranks in the listed order, p5 before p20).
- `VARIANTS` gains the 53 ranked ids. A ranked variant = the base variant's `Signals` with `raw_score` attached via `ranking.raw_scores`.
- `Inputs` gains `features: pl.DataFrame | None = None`; ranked variants use it when set, else compute `rank_features(inputs.bars, inputs.spy)`.
- `variant_config(variant_id: str) -> VariantConfig`.
- Compose CLI computes `rank_features` once and passes it in `Inputs`.

**Task 20 — `app/research/evaluate.py`, `app/research/registry.py`, `scripts/research/evaluate.py`, `scripts/research/freeze.py`**
- `run_table(svc, table_path, period, slippage_bps=MAIN_SLIPPAGE_BPS, max_positions: int = 10)`.
- `EvalContext` gains `configs: dict[str, dict]` (variant id -> `{"base","rank","slots"}`, default `{}` meaning slots 10) and `random_cache_dir: Path | None = None`, `cache_salt: str = ""`.
- `random_runner(paths, period, max_positions) -> list[float]`.
- Random baseline cache: key = sha256 of canonical JSON `{counts: [[date, n], ...] of entry rows, k (repr), slots, period name/start/end, seeds, code: [file_hash of baselines.py, compose.py, evaluate.py], salt: ctx.cache_salt}`; value file `<random_cache_dir>/<key>.json` = `{"cagrs": [...], "note": null}`; also an in-memory dict. `result["random_cache_hit"]: bool`. The CLI salt = data generation + universe hash + bridge hash.
- `result["config"]` = the variant's config dict (or `{"base": id, "rank": "hash", "slots": 10}`).
- `Registry.record(..., config: dict | None = None)` stores `config` in the row.
- `Pins` gains `variant_configs: dict[str, dict]`, added to `_PIN_FIELDS`; both CLIs fill it from `VARIANT_CONFIGS`.
- `folds(period, trading_days, test_len=126, embargo=5, min_tail=63)`: after the full windows, the remaining days (after the embargo) form one more window if there are at least `min_tail` of them.

**Task 21 — `app/research/report.py`**
- Summary gains `Rank` and `Slots` columns from `result["config"]`.
- New `## Ranking grid` section after Summary: one table per slot count present; rows = base, columns = `hash, rs, retr, risk, relvol, lowvol`, cell = `CAGR / random pct` or `—`.

## Review Focus

1. A ranking that peeks: `rs`/`relvol`/`lowvol`/`risk` at t must equal the value computed on bars truncated at t; `retr` must raise on a leg date after t.
2. v1 tables and v1 results must not change: compose of the 18 v1 variants is byte-identical before/after.
3. Ranked variants of one base must have exactly the same entry rows (only `score` differs), which is what makes the random cache exact.
4. Slot count must reach main, stress, folds and every random seed; a cache hit must never cross slot counts.
5. float32: two different raw scores on one date must never produce equal table scores.
