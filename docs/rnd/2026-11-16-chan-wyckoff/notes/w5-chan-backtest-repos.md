# Chan backtest repos

## Search log

All searches run 2026-11-16 with `gh search repos "<q>" --limit 20 --json fullName,stargazersCount,pushedAt,description,url`
(scratch dir: `D:/Desktop/rnd-scratch-2026-11-16/task_79009302701d/`).

| query | gh CLI result | GitHub REST `/search/repositories` (same q, per_page=20, sort=stars) |
|---|---|---|
| `缠论 回测` | **0 results** | 20 results (total_count=20) |
| `chanlun backtest` | **0 results** | 3 results |
| `缠论 策略` | 3 results | 20 (rate-limited on retry, see below) |
| `chan theory trading` | 1 result | rate-limited on retry |

- DISCREPANCY NOT VERIFIED: `gh search repos` returned 0 for multi-term queries where the REST API returned 20;
  numbers below come from REST (sorted by stars). CLI was used to confirm the low-count queries
  (`缠论 策略` → sunshinelover/chanlun 86★, 991025Q/quanthub 0★, JY-lc/chanlun-postmortem 1★;
  `chan theory trading` → feizhu-runkang/cl-trade-system 0★). REST then hit `API rate limit exceeded`
  (user 144331525) at 04:34:45Z, so rows 3–4 are CLI-only and incomplete.
- Excluded per task (yijixiuxin/chanlun-pro showed up in results, skipped); `lbh-boopoo/chanlun-v51` (0★) is an
  **empty repo** (`git clone` → "You appear to have cloned an empty repository"; raw README → 404). Twin
  `lbh-banboo/chanlun-v051` is populated and used instead.

## Table: repo | stars | last push | market | timeframe | buy points | results shown? | mentions lookahead?

| repo | ★ | last push | market | timeframe | buy points | results shown? | mentions lookahead? |
|---|---|---|---|---|---|---|---|
| YuYuKunKun/chanlun.py | 166 | 2026-09-01 | stock + crypto (README L8, L39) | daily (step=86400, L47) | "简单标注买卖点" only (L8) | no — only `output.gif` | **no** (grep 未来/确认: no hit) |
| haigechanlun/chanlun_auto_trading | 135 | 2026-05-01 | crypto / FX (L72) | 1H/4H/daily (L46) | 一/二/三买 + 一/二/三卖 (L45) | yes, claimed only (L15, L70, L175) | no — only generic risk disclaimer (L74) |
| qhgary/zen_plugin_apps | 46 | 2026-11-13 | A-share (TDX) | intraday bars, replay (L63) | 三类买卖点 (L23) | no results; paid license (L65) | **yes** — "不引用未来K线" (L27) |
| V0idk/easy_tdx_1 | 16 | 2026-09-11 | A/HK/US (desc) | user-set (daily example, L63) | 买卖点 + 背驰 (L82) | sample report screenshots (L61–72) | "无未来数据" for TDX formulas only (L58) |
| JY-lc/chanlun-postmortem | 1 | 2026-09-10 | A-share (L123) | daily (L123) | 买点 generic, 103/127/189 trades (L46) | **yes, negative result** (L16–18) | **yes — whole repo is about 未来函数** (L35–42) |
| lbh-banboo/chanlun-v051 | 1 | 2026-11-08 | A-share (L5) | 30-min (L7, L43) | 一/二/三买 + 三卖 + 趋势一卖 (L43) | **yes, honest/negative** (L114–119) | **yes — 无前视 + truncation test** (L5, L169) |
| Hong-Chen-web/watchdog | 2 | 2026-10-22 | A-share | daily, 2019–2026 (L38) | B1/B2/B3·S1/S2/S3 (L62) | "八年回测 每年正收益" (desc, L38) | not mentioned |
| sunshinelover/chanlun | 86 | 2017-05-18 | A-share (vnpy) | — | — | no | not mentioned |
| rucmeteor1025/chanlun-skills | 5 | 2026-08-12 | A-share | — | type-1/2/3 | no | not mentioned |
| JohnnyTenkyo/meigulianghua | 4 | 2026-03-10 | US | custom | 分型/买卖动能 | yes, report images | not mentioned |
| leuchtend27/a-share-multistrategy-backtest | 0 | 2026-09-15 | A-share | daily | 分型 only | no | not mentioned |
| feizhu-runkang/cl-trade-system | 0 | 2026-08-27 | unspecified | — | — | no | not mentioned |

## Notes on the top 5 (max 6 lines each, cite README lines)

Ranked by relevance-to-task × stars; JY-lc and lbh-banboo rank high on "publishes results / discusses lookahead".

### 1. YuYuKunKun/chanlun.py — 166★ (`README.md`)

- Scope: 笔/段/中枢 + "简单标注买卖点" + TradingView-style web UI + Backtrader backtest (L8, L14).
- Market crypto (symbol=ethusd/btcusd, L39, L45); timeframe is a `step` seconds param, 86400=daily (L47).
- Buy points are only "简单标注" (no 一/二/三买, L8); backtest = "实时回测" via Backtrader (L14) — a harness, not a study.
- **No performance numbers at all**; only an `output.gif` preview (L74). No 未来函数/repaint/确认 discussion (grep: 0 hits).
- Take-away: useful as a reference implementation, worthless as evidence.

### 2. haigechanlun/chanlun_auto_trading — 135★ (`README.md`)

- Crypto/FX automated system; detects 一/二/三买 + 一/二/三卖, multi-timeframe 1H/4H/daily (L45–46, L72).
- Claims: "回测收益年化 80%+" (L15), "> 85%" over ~1 year, "Sharpe 表现突出" (L69–71, L175).
- Numbers are **claims only** — evidence is two PNG screenshots (L76–77); no trade log, no cost/slippage assumptions.
- No 未来函数/lookahead/repaint/确认 statement anywhere; only a generic disclaimer (L74). 1-year ETH sample = high overfit risk.
- Take-away: marketing funnel (Telegram + exchange referral links L22–31), not reproducible research.

### 3. qhgary/zen_plugin_apps — 46★ (`README.md`)

- Rust engine, ~40k LOC; TDX DLL plugin + optional offline replay/backtest tool (L9, L61–63).
- A-share, TDX-exported bars; backtest = bar-by-bar replay, "验证自己的交易策略统计胜率" (L63).
- 三类买卖点 "严格定义，每个中枢单发判定（首个回抽确认）" (L23); 背驰 split strong/weak via MACD area + DIF/DEA (L21).
- **Explicitly addresses lookahead**: bar-by-bar incremental push, "条件在当下满足即确认，确认后不回改，也不引用未来K线" (L27);
  also claims backtest and live use the same engine (L27).
- **No published results** — no win rate, no equity curve in README; tool is commercial (3-day trial, license key, L65).
- Take-away: strongest methodological statement of the five, but zero verifiable numbers (binaries only).

### 4. JY-lc/chanlun-postmortem — 1★ (`README.md`) — the anti-hype repo

- Deliberate **negative result**: a "20.65% annual / Sharpe 2.00" chan strategy re-tested (L1, L4, L16).
- Market A-share, daily, single-instrument timing, sample 2016–2026 (L123); buy signals generic 买点, trade counts 103/127/189 (L46–47).
- Numbers: 20.65%→16.79% after data/cost fixes→**2.05% annual, Sharpe −0.15** after removing lookahead; equal-weight β = 2.01% (L16–18).
- Root cause named precisely: **确认滞后型未来函数** — 分型 needs right-side bar, 笔 endpoint keeps being replaced, so
  `signal.date = 笔终点日` is not tradable that day (L37); 24.8% of buy points are "幻影信号" (L42).
- Also reports 11 improvement paths all failing (L56–57) and 6 acceptance gates incl. β control, Monte Carlo (L74–82).
- Take-away: **best available evidence that chan buy points add no edge on A-share daily**; use as the baseline to beat.

### 5. lbh-banboo/chanlun-v051 — 1★ (`README.md`) — honest paper-trading system

- A-share, **30-minute** 线段 engine: 分型→笔→线段→中枢→一买/二买/三买/三卖/趋势一卖 (L7, L43); "无前视" headline (L5).
- Published numbers, incl. bad ones: unselected全市场 13,495 trades avg **−0.116R** (L114); hand-picked watchlist 403 trades
  **+0.62R** but account 7.4%/yr vs buy-and-hold 23.8% (L115–117); regime filter: −5.2%→+7.9% (L119).
- Explicitly says the edge comes from **stock picking, not the buy points** (L23, L113).
- Lookahead handled by design + test: 截断重算 0 差异 (L36, L43); `test_engine_truncation.py` truncates data and
  requires already-emitted signals to match (L169); mutation testing found "背驰面积偷看未来" (L175).
- Entry rule is tradable-by-construction: 30-min close confirm → next bar open, +10bp slippage (L45, L80).
- Limitation admitted: only one 6-year window, 2015–2020 unverifiable (L189).
