# w3b — Chan+Wyckoff combiner details

Sources (README only unless noted; downloaded 2026-11-16, never executed):
`Lasigebo-T/analyze-stocks-chan-wyckoff` → `analyze-stocks-chan-wyckoff/references/chan-wyckoff-boundaries.md`;
`DamonWu302/ChanWyckoff-Mainline` → `systems/chanwyckoff-mainline/backend/app/signals/third_buy.py` and `docs/system_design.md`.

## 1. Lasigebo-T boundary / mapping rules (chan-wyckoff-boundaries.md)

### Chan minimal definition (L12-54)
- Structure order fixed: K-line containment → top/bottom fractals → unified 笔 → 线段 by feature sequence (not one 笔) → 中枢 from ≥3 completed overlapping sub-level moves → recurse to higher level (L14-21).
- Verify trend/comparable segment before 背驰; verify 1/2/3 buy-sell points item by item (L22-23).
- 中枢 core: `ZD = max(L1,L2,L3)`, `ZG = min(H1,H2,H3)`, valid when `ZD <= ZG`; `[DD,GG]` is the outer range and must not be mixed with core (L25-32).
- 盘整 = one 中枢 at that level; 趋势 = ≥2 same-direction non-overlapping 中枢; "minimum structure met" ≠ imminent reversal (L34-38).
- 背驰 boundary: no trend → no strict trend divergence; later/earlier segments must be same direction, same level, comparable; small-level divergence cannot declare large-level reversal; MACD only assists already-determined segmentation (L40-45).
- Buy/sell labels: 一买/卖 = trend-divergence endpoint; 二买/卖 = first pullback after a completed reverse sub-level move; 三买 = completed upward leave of 中枢 and first completed sub-level retest whose low ≥ ZG; 三卖 = mirror, high ≤ ZD (L47-52).
- Labels are structural tags, not unconditional orders (L54).

### Wyckoff minimal definition (L56-108)
- Call a range only a `Trading Range` candidate; do not pre-label accumulation/distribution (L58).
- Accumulation chain: A `PS—SC—AR—ST`; B repeated tests/absorption; C Spring/Shakeout + Test (Spring optional); D SOS then LPS/BU; E accepted leave into Markup (L60-66).
- Distribution chain: A `PSY—BC—AR—ST`; B turnover/supply; C UT/UTAD + test (UTAD optional); D SOW then LPSY; E accepted leave into Markdown (L68-74).
- Every event needs "position + current volume-price + subsequent confirmation"; a single long wick, volume spike or boundary poke cannot confirm SC/Spring/UTAD alone (L76).
- Effort–result: effort = relative volume, turnover, duration; result = spread, close location, net progress, overshoot, follow-through (L78-80).
  - high volume + wide close at favorable end + progress → active demand/supply, still verify follow-through/retest (L84).
  - high volume + narrow spread/long wick/little progress → absorption or distribution, do not label on the spot (L85).
  - low volume + narrow/weak → no-supply/no-demand test candidate, check position and next response (L86).
  - low volume + big progress/gap → liquidity vacuum, fragile, check refill/support/volume (L87).
- Compare volume only with same symbol, similar background and session; handle open/close, limit-up/down, suspension/resumption and ex-rights anomalies separately (L89).
- Breakout acceptance has 3 gates: leave (close beyond boundary, not just wick), continuation (no quick return), acceptance (first reasonable retest holds outside new boundary then expands) (L93-99). Only gate 1 = "breakout candidate" (L99).
- False-breakout evidence: big effort/small progress; quick return into range; weaker volume-price on retest; clear opposite-direction progress (L101-106).
- Post-breakdown recovery = Spring candidate; post-breakout recovery = UT candidate; wait for Test/SOS/LPS/SOW/LPSY (L108).

### Explicit non-equivalences (mapping table, L110-121)
- Trading Range ≠ 中枢: supply-demand behavior zone vs strict sub-level overlap definition (L114).
- Phase A–E ≠ 缠论走势类型: supply-demand stage vs recursive geometry (L115).
- Spring ≠ 一买: may co-occur, or be only a lower-boundary false break (L116).
- Test/LPS/BU ≠ 二买/三买: buy points still need Chan sub-level completion and boundary conditions (L117).
- SOS/SOW ≠ 中枢离开: usable as leave-quality evidence, cannot replace structural completion (L118).
- UT/UTAD ≠ 一卖: range false break does not require a strict uptrend divergence (L119).
- 努力大、结果小 ≠ 缠论背驰: absorption/distribution vs divergence requiring trend + comparable segments (L120).
- Creek/Ice retest ≠ 三买/三卖: dual-label only if structural conditions also hold (L121).

### Joint (联立) rules (L123-130)
- Chan decides level, structural position and definition boundaries; Wyckoff judges breakout, retest and supply-demand quality (L125-126).
- Same direction + follow-through → upgrade evidence (L127); conflict → downgrade to wait, never pick the view that suits the position (L128).
- Large-level boundary constrains small-level signals; small-level change supplies early trigger (L129); never promote a small-level signal into a large-level reversal (L130).

## 2. DamonWu302/ChanWyckoff-Mainline — rules as coded

### 三买 rule (third_buy.py)
- Breakout prerequisites: structure quality ≥70 (L67, L80-81); 30m close above platform upper by ≥0.5% (L68, L85-87); breakout volume / platform average ≥1.5 (L69, L89-91) → `proto_3buy`, action `light_position` (L94-103).
- Confirmation window ≤8 pullback bars, else `failed_3buy`/`pullback_timeout` (L115-122); any pullback close below upper → failed (`heavy_volume_close_back_inside` if volume ≥ platform avg, else `close_back_inside`) (L124-145).
- Must touch upper support within `max(0.5*ATR, 2% of upper)` (L147-149, L200-202); max pullback volume must be below both breakout volume and platform average (L151-157) → `confirmed_3buy`, `upgrade_position` (L159-169).
- Mapping note: platform `upper` acts as the ZG proxy; "first sub-level retest low ≥ ZG" is approximated by a wick touching the upper band while close never goes below it — stricter than classic 三买 (interpretation, code L137-149, L200-202).

### Wyckoff filter rule (third_buy.py + system_design.md)
- Wyckoff is a scoring/explanation layer, not a trigger: `wyckoff_score` + natural-language evidence; trade state stays with `proto_3buy`/`confirmed_3buy`/`failed_3buy` (system_design L180, L188).
- Score is computed after the Chan gate, base 45: +15 if structure score ≥80 else +8; +15 if breakout volume ratio ≥1.8 else +8; +12 if breakout strength ≥2.5% else +6 (third_buy.py L204-241); confirmed pullback adds +12 (L252). Clamped 0–100 (L232).
- Design score bands: 80–100 light probe then standard size after retest; 60–79 observe/very light; 40–59 watchlist; <40 filter, keep for review only (system_design L192-195).
- As coded, no `wyckoff_score` threshold gates a signal: the only `filter` action is the fixed score-25 `failed_3buy` path (third_buy.py L184, L189-197). Whether <40 filtering exists elsewhere: NOT VERIFIED (not in these 2 files).

### Result numbers
- No backtest result/report data file exists: recursive tree grep for `backtest|result|report` returned only `backend/app/backtest/engine.py`, `api/backtests.py`, UI pages, `lib/backtestSummary.ts` and tests (no CSV/JSON/markdown results).
- README/system_design contain only the plan: A-share, 30-minute main timeframe (system_design L19), structure backtest 2019→present, theme-enhanced backtest only where theme history is reliable (L244-246); metrics planned = win rate, mean/median return, profit factor, expectancy, max drawdown, holding period, post-signal 1/3/5/10-day return distribution (L231-240). No actual market/period/metric values — NOT VERIFIED / none published.
