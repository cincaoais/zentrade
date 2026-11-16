# Repos combining Wyckoff + 缠论

## Search log (each query + number of hits)
`gh search repos <q> --limit 15` on 2026-11-16 (hits returned, capped at 15):
- `wyckoff chan` — 0
- `wyckoff 缠论` — 0
- `威科夫 缠论` — 0
- `wyckoff chanlun` — 0
- `威科夫` — 15 (limit reached)
Supplements (not in brief, run because all combined queries returned 0):
- `缠论 威科夫`, `chanlun wyckoff`, `wyckoff chan theory`, `chan wyckoff trading` — 0 each
- API `search/repositories?q=wyckoff chanlun in:name,description,readme` — 6
- API `search/repositories?q=威科夫 缠论 in:readme` — 33

## Candidates table
Stars/last push from `gh api repos/OWNER/REPO` metadata, 2026-11-16. Repo descriptions quoted/cited below.

| repo | stars | last push | one-line what | combines both? |
|---|---:|---|---|---|
| Lasigebo-T/analyze-stocks-chan-wyckoff | 0 | 2026-08-01 | Codex skill: Chan structure + Wyckoff volume evidence + risk-bounded T-trading | yes |
| DamonWu302/ChanWyckoff-Mainline | 1 | 2026-05-29 | "缠威主线系统": Chan structure + Wyckoff volume-price + A-share themes | yes |
| byronwang2005/a-share-signal | 5 | 2026-05-06 | A-share signal skill using chips, resonance, KDJ, Wyckoff and Chan | yes (claimed) |
| baizhiwei9/techAnly-summary | 0 | 2026-11-16 | Python TA report: two Chan variants, SMC, VWAP; Wyckoff accumulation as filter | partly |
| yadinae/stock-mcp-server | 0 | 2026-11-06 | Stock MCP server: Wyckoff funnel screener + separate Chan module | partly |
| wjjbw/techchart-pro | 0 | 2026-06-20 | 7-school pattern recognition incl. 缠论 and 威科夫, as parallel lenses | partly |
| SigmaPilotAI/sigmapilot-mcp | 0 | 2026-06-13 | MCP with separate Wyckoff and Chan Theory analyzers | partly |
| MobiusQuant/OpenMobius-skill | 726 | 2026-11-12 | Knowledge skill; Chan/Wyckoff only Q&A lenses, no native analyzer | no |

## How the real combiners combine them (per repo, max 6 lines, cite README lines)
**Lasigebo-T/analyze-stocks-chan-wyckoff** — README.md
- Framework "一锚三层、双证三景、一表一账"; 双证 = Chan structural evidence and Wyckoff volume-price evidence judged independently, then cross-validated (L9, L13).
- Explicit non-equivalences: 中枢 ≠ Wyckoff trading range, Spring ≠ 一买; no automatic price-zone-to-order translation (L18).
- Deliverable is a trigger/action/size/invalidation table plus T-trading cost ledger; boundary rules live in `references/chan-wyckoff-boundaries.md` (L15-16, L74-81).
- This is the only repo found whose core method is explicitly a Chan+Wyckoff cross-validation protocol.

**DamonWu302/ChanWyckoff-Mainline** — README.md + systems/chanwyckoff-mainline/README.md
- Repo's first system is "ChanWyckoff Mainline / 缠威主线系统", "基于缠论结构、威科夫量价关系和 A 股题材主线" (root README L3, L9).
- Pipeline: ingest → market regime → theme strength → trend capacity core → 30-minute 三买 scan → backtest → daily desk (system README L8).
- Combination is at strategy level: Chan 三买 as the entry trigger, Wyckoff volume-price as context/filter, theme mainline as selection (system README L3, L8); code not inspected.

**byronwang2005/a-share-signal** — README.md
- Claims analysis "基于筹码分布、三周期共振、优化KDJ、威科夫与缠论等框架" in one Skill (L16).
- Signals described as breakout/volume/golden-cross quality plus "主力洗盘 / 出货倾向与阶段状态" (Wyckoff-like phase reading) (L23-24).
- No detail on how Chan and Wyckoff are weighted or fused in the README — mechanism NOT VERIFIED.

**baizhiwei9/techAnly-summary** — README.md
- One report runs four lenses: 缠论分型版 + 缠论均线版 + SMC + VWAP; separate strategy table adds 威科夫吸筹 (L3, L108-117).
- Wyckoff accumulation is used as a filter layer (weekly/daily), not as a standalone Chan signal: "威科夫吸筹 | 盘中（过滤层周/日） ... misc/engine/wyckoff_buypoint_v2.py" (L117).
- Combined only at report/pipeline level (`misc/strategies/run_level.py`); no documented Chan-Wyckoff fusion rule (L107-117).

**yadinae/stock-mcp-server** — README.md
- Two co-located engines: a 4-stage Wyckoff funnel screener (L204, L229) and a "缠论模块：笔 / 段 / 中枢 / 背驰 / 买卖点信号" (L236).
- Backtest lists "5 种内置 + 5 种 Wyckoff 策略 + 缠论" as separate strategy families (L18).
- No README statement that Chan signals are filtered/confirmed by Wyckoff or vice versa — co-existence, not documented fusion.

## Nothing found? say so plainly
Exact combined queries in both English and Chinese returned zero; GitHub search only surfaced combiners through broader `威科夫` or `in:readme` searches. Real, documented Chan+Wyckoff fusion is rare and mostly prompt/skill-level (Lasigebo-T, DamonWu302, byronwang2005); code-level fusion was not found. The niche is dominated by 0-5 star repos, so quality is unverified.