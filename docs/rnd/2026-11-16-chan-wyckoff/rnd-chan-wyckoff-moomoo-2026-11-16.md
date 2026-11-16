# R&D: Wyckoff + 缠论 for US stocks on daily bars, with moomoo OpenD as data and broker
Date: 2026-11-16 · Verdict: **Pursue: build the test harness first, not the strategy** · Confidence: medium on the plan, low that any variant has an edge

## Bottom line

No public source shows that automated 缠论 or Wyckoff signals make money on stocks after honest testing. The two
public post-mortems that tested Chan honestly both found **no edge** once a signal is only counted on the bar
where it could really be known. So the first thing to build is not a strategy. It is a **replay harness**
that makes hindsight impossible, plus a fixed list of hypotheses scored against buy-and-hold of the same stocks.

Combining the two methods is a reasonable hypothesis, and practitioners describe the same split:
Wyckoff for context, 缠论 for timing. But nobody publishes code-level fusion with results. The LLM can only
be a forward-tested reviewer: three papers show LLMs remember past US stock returns, which makes any LLM
backtest on past data invalid.

The moomoo side is feasible with known limits (section 6).

## What already exists

- **Chan engines** are mature: `waditu/czsc`, `Vespa314/chan.py`, and your own DSA engine (`daily_stock_analysis/src/chan/engine.py`,
  built on czsc, already marks `confirmed` vs forming strokes). The open problem is the clock, not the
  geometry: when is a structure known? Your earlier measurements on XAUUSD (Desktop/trading/rnd/kb/SYN-CHAN-2.md D34-D37):
  czsc finished-笔 lag median +3 bars, p90 +10, max +42; 43.2% of M15 笔 endpoints change later.
- **Chan honesty tools** now exist in two small repos: a *truncation test* (cut the data at every bar,
  recompute, require identical past signals) and a *phantom signal rate* (signals that exist in full-history
  analysis but never appear in day-by-day replay).
- **Wyckoff detectors** are mostly heuristics: SC/BC, AR, spring/UTAD, SOS/SOW on daily bars, with no
  published backtest you can reproduce. Wyckoff Analytics (a primary school) published a spring rule set,
  without test results.
- **Fusion** of the two exists only as prompts or skills (Lasigebo-T, DamonWu302, byronwang2005), all 0-5 stars, no results.

## The hypotheses to test (several directions, as requested)

Each one is a separate strategy run through the same harness. "Confirmed" means the signal is
emitted on the first bar the structure is known in day-by-day replay, and the fill is the next bar's open.

| ID | Hypothesis | Entry | Stop / exit | Why it is on the list |
|---|---|---|---|---|
| B0 | Baselines | equal-weight buy-and-hold of the same universe; random entry with the same hold and stop distribution; SPY | same | Both Chan post-mortems failed against buy-and-hold. This is the bar to clear. |
| H1 | Chan only | confirmed 2B or 3B on daily | stop = `invalidation_price`; exit on confirmed sell point or 20-bar cap | Reference. Expected to fail per evidence; measures phantom rate on US stocks |
| H2 | Wyckoff only | spring or LPS inside a detected trading range (SC→AR→ST), volume rules from Wyckoff Analytics' spring definition | stop below the spring low; exit on UT/SOW or 20-bar cap | US stocks have real volume, unlike your CFD work. This is the method's fair test |
| H3 | Wyckoff where × Chan when | Chan 2B/3B only while Wyckoff state is accumulation phase C/D or early markup | Chan invalidation | The split practitioners describe (CSDN 2025-10-13) and the one I proposed |
| H4 | Chan 中枢 breakout judged by Wyckoff effort/result | 3B only if the stroke leaving the 中枢 has expanding volume (SOS-like) and the pullback has shrinking volume (LPS-like) | ZG of the 中枢 | Zhihu snippet; DamonWu302 codes the same idea (volume ≥1.5×, pullback within 0.5 ATR/2% on lower volume) |
| H5 | Two independent witnesses | a Chan buy and a Wyckoff buy fire independently within N bars, same direction | tighter of the two stops | Lasigebo-T "双证" cross-validation; keeps 中枢 ≠ trading range and spring ≠ 一买 |
| H6 | Market regime overlay on H3-H5 | above + SPY above its 200-day MA (ZenTrade already has a direction gate) | same | Cheap filter, tested as an add-on, not a separate strategy |
| L1 | LLM reviewer (forward only) | the best of H1-H6 + an LLM veto given the structure snapshot | same | Can only be judged on simulated live data after the model's training cutoff |

The variant budget is fixed in advance (proposal: ≤ 24 runs in total, including parameter variants), and every run is logged,
so the deflated Sharpe can correct for how many were tried.

## How "works" is judged

1. **Harness gates (must pass before any result counts):** truncation test with 0 differences; phantom rate reported per hypothesis; costs charged; next-bar-open fills.
2. **Split:** moomoo gives ~1,000 daily bars (~4 years) per symbol. Proposal: the first ~3 years for development and the last ~1 year untouched until a single final run.
3. **Pass to simulated account (proposal; your call):** on the untouched year, after costs, beats both same-universe buy-and-hold and random entry; ≥ 100 trades; positive average R; deflated Sharpe probability ≥ 0.9.
4. **Simulated account (months to 1 year, your plan):** track live signal count, win rate and R distribution against the backtest's numbers, and flag drift. Real money is your decision after that.

## Reference repos and frameworks

| Repo | What to take from it | Stars | Last push | Licence | Maturity | Evidence |
|---|---|---|---|---|---|---|
| `waditu/czsc` | Structure engine already used by your DSA engine | (studied in S1) | see S1-czsc.md | Apache-2.0 per S1 | production | Desktop/trading/rnd/00-study/S1-czsc.md |
| your `daily_stock_analysis/src/chan` | Port target: 1B/2B/3B with `invalidation_price`, `confirmed` flag | private | 2026-11 | yours | usable | src/chan/engine.py:1-40, :57 |
| `JY-lc/chanlun-postmortem` | The audit method: confirm-lag lookahead, phantom rate, 4 illusions checklist | 1 | 2026-09-10 | unknown | experimental | README L4, L16-18, L37-47 (read directly) |
| `lbh-banboo/chanlun-v051` | Truncation test design (`test_engine_truncation.py`), honest per-trade R reporting | 1 | 2026-11-08 | unknown | experimental | README L114-117, L169 (read directly) |
| `systematic1/wyckoff-scanner` | Daily-stock Wyckoff event heuristics (SC/BC, AR, spring/UTAD, SOS/SoW) | 0 | 2026-11-11 | unknown | experimental | notes/w4 README:74-93 |
| `Ju571nK/ChartNagari` | Wyckoff accumulation/distribution/spring/upthrust detection logic (Go) | 28 | 2026-11-15 | unknown | experimental | notes/w4 |
| `YoungCan-Wang/WyckoffTradingAgent` | Spring/SOS/LPS/UTAD thresholds on daily stocks; LLM as narrator, not decider | studied in S5 | 2026-10-31 | AGPL-3.0 (do not copy code into MIT ZenTrade) | usable | Desktop/trading/rnd/00-study/S5 |
| your `Desktop/GitHub/wyckoff` | Your own detectors (XAUUSD) and calibration notes | private | 2026 | yours | experimental | git log fc14299 |
| `Lasigebo-T/analyze-stocks-chan-wyckoff` | The Chan-vs-Wyckoff non-equivalence table, for H5 | low | unknown | unknown | experimental (skill/prompt) | notes/w3b |
| `DamonWu302/ChanWyckoff-Mainline` | Coded 3B state machine with volume rules, for H4 | low | unknown | unknown | experimental, no results | notes/w3b |

Checked and not useful: `popbo/alphas` (A-share factor library, no Chan/Wyckoff), `AdvancingStone/stock-learning`
(article collection, encrypted TDX indicators only), `cincaoais/chanlun-pro` (closed-source, licence-gated).

## Evidence

| Claim | Source | Date | Tier |
|---|---|---|---|
| Chan strategy 20.65%/Sharpe 2.00 → 2.05%/−0.15 after removing confirm-lag lookahead; buy-and-hold 2.01%; 24.8% phantom buy signals | github.com/JY-lc/chanlun-postmortem README L4, L16-18, L42 | 2026-09-10 | practitioner (read directly) |
| Chan engine with 0-diff truncation: 13,495 unselected trades at −0.116R; curated pool 7.4%/yr vs 23.8% buy-and-hold | github.com/lbh-banboo/chanlun-v051 README L114-117 | 2026-11-08 | practitioner (read directly) |
| A 中枢 from A to C cannot be known at A, only after the turn at C | sohu.com/a/442588520_120754333 | 2021-01-05 | practitioner |
| czsc 笔 confirmation lag median +3 bars, p90 +10 (XAUUSD) | Desktop/trading/rnd/kb/SYN-CHAN-2.md D34-D37 | 2026 | your own measurement (CFD, not stocks) |
| LLM backtests contaminated by memorised returns; anonymising helps | Glasserman & Lin, arXiv 2309.17322 | 2023 | primary |
| LLM recall of daily stock returns: avg 10% correlation, >40% for some blue chips; S&P monthly ~100% | Economics Letters, S0165176525004392 | 2025 | primary |
| LLM trading returns collapse after the model's training cutoff (DeepSeek ~−22pp) | Benhenda, arXiv 2601.13770, via financealliance.io | 2026-01-22 | primary (secondary report; paper not read in full) |
| No rigorous public evidence VSA is profitable on its own | luxalgo.com/library/concept/volume-spread-analysis | fetched 2026-11-16 | practitioner |
| Wyckoff spring algorithm rules (lookback low, penetrate without close below, volume, tail) with no results | wyckoffanalytics.com/identifying-wyckoff-springs-with-algorithmic-trading-strategies | 2025-06-03 | primary school, no data |
| Wyckoff = cycle, 缠论 = buy/sell points, order flow = confirmation | blog.csdn.net/weixin_42480337/article/details/153217041 (summary only) | 2025-10-13 | anecdotal |
| Selecting the best of N backtests inflates Sharpe; deflate by N | Bailey & López de Prado, SSRN 2460551 | 2014 | primary |

## Disagreements

- **Combining helps vs. adds nothing.** Practitioners claim the combination raises win rate, but publish no tests. The only honest Chan tests
  found that adding MACD or Bollinger confirmation (10 variants) did not help (chanlun-postmortem L63-64). Adding filters to a
  signal with no edge often just cuts the trade count. H3-H5 must beat H1 *and* B0, not only H1.
- **wyckoff-validation** reports daily CAGR 12.98%, Sharpe 0.76 on 4,276 signals, but its detection engine is proprietary
  and absent, so the number cannot be checked. Treated as unverified.
- **moomoo docs conflict** on pre/after-market support for US simulated trading (w7 Q1). Test it on your account before relying on it.

## What would change the verdict

- A phantom rate near 0% on US daily bars **and** H3/H4/H5 beating same-universe buy-and-hold on the untouched year → pursue to the simulated account.
- All hypotheses failing to beat random entry → stop and do not take these signals to simulated trading. Use the structure only as LLM context or for display.
- If the history-bar quota or subscription rotation turns out to be against moomoo's terms or throttled in practice, the universe has to shrink to about 100 symbols, and that cuts the trade count below the test threshold.

## Moomoo OpenD findings (data and auto-trade)

Verified on your OpenD (read-only probe, 2026-11-16): quotes and history bars work; REAL and SIMULATE US margin
accounts are both ACTIVE; history quota 2 used / 98 left; subscription-based `get_cur_kline` returned 1,000 daily bars
(2022-11-18 → 2026-11-13) without using the history quota; screener returned 1,481 US stocks with market cap ≥ $10B; snapshot has P/E, market cap, turnover, volume ratio.

From the docs (w6, w7 notes, with line citations):
- History-bar quota counts **distinct symbols per 7 days** (changed from 30 days on 2026-04-16). Tiers 100/300/1,000/2,000 by assets or monthly activity (e.g. >200 filled orders/month → 1,000).
- Subscriptions must be held ≥ 1 minute. Snapshot 400 symbols/call, 60 calls/30 s. Screener 10 calls/30 s, 200 results/page.
- Simulated US trading: limit and market orders only, DAY orders only, fills at best bid/ask, no published slippage model.
  **No fill (deal) push or query on simulated**, so fills must be read from order updates (`dealt_qty`, `dealt_avg_price`). That path works on both accounts.
- `place_order` 15 per 30 s per account; modify/cancel 20 per 30 s; order queries 10 per 30 s.
- REAL needs `unlock_trade` (REAL only), plus moomoo's API questionnaire and disclaimer.

## Coverage gaps

- Zhihu full answers (login wall), Reddit (scraper unsupported), X, and Bilibili/Douyin videos were not read. They might add practitioner
  combination rules but are unlikely to add tested results.
  - Retry 2026-11-16: the CloakBrowser helper (cloakbrowser 0.7.2) now needs a licence key claimed by email. Downgraded to 0.6.5
    (free v146 binary, needed playwright==1.57.0): Reddit (www and old) returned "Prove your humanity" captcha / login wall,
    Zhihu returned 403 captcha, headless and headed alike. Gap stands.
    Reddit's JSON search returned HTTP 403 to plain curl. A web search restricted to reddit.com returned 10 snippets: anecdotes
    ("does anyone successfully daytrade using wyckoff", a TradingView auto-Wyckoff indicator), with no backtest numbers.
    Hacker News (Algolia API, 52 hits for "wyckoff") had nothing on the method.
- No study of Chan or Wyckoff on **US** stocks was found. All honest Chan tests are on A-shares; the market behaviour differs (T+1, price limits).
- Moomoo screener lists only current stocks, so delisted names are missing (survivorship bias). It must be stated in every result.
- The moomoo simulated fill model and the `remark` field's idempotency behaviour were not verified.

## Next step

Smallest first step: port the DSA czsc engine into ZenTrade as a **day-by-day replay** engine, with the truncation test and
phantom-rate report, then run H1 (Chan-only) on ~50 US symbols pulled from moomoo. Success = truncation test 0 differences; a phantom
rate and confirmation-lag histogram reported for US daily bars. No P&L claim yet.

### Claude Code handoff prompt

```
In D:\Desktop\GitHub\zentrade, read docs/rnd/2026-11-16-chan-wyckoff/rnd-chan-wyckoff-moomoo-2026-11-16.md.
Port the Chan structure engine from D:\Desktop\GitHub\daily_stock_analysis\src\chan\engine.py into
backend/app/strategy/ as a day-by-day replay engine: at each bar t it may only see bars <= t, and it emits a
signal only on the first bar where the structure is confirmed. Add a pytest truncation test (recompute at every
cut point; past signals must be identical) and a report of phantom rate and confirmation lag in bars.
Use moomoo OpenD at 127.0.0.1:11111 via subscription + get_cur_kline for 50 US symbols (read-only, no orders).
Do not change the Alpaca code yet. Report the commands run and their output.
```
