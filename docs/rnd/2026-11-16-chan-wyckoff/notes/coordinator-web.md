# Coordinator web findings (2026-11-16)

## Keyword map
- EN: Wyckoff spring/SOS/LPS/UTAD, effort vs result, VSA, Wyckoff algorithmic, Chan theory, chanlun, repaint, lookahead, confirmation lag, deflated Sharpe, PBO, LLM look-ahead bias, memorization
- ZH: 缠论 量化 回测, 未来函数, 确认, 威科夫 缠论 结合, 中枢 放量突破, 三买, 弹簧
- Named: czsc, chan.py, WyckoffTradingAgent, Wyckoff Analytics, VSA/TradeGuider, Look-Ahead-Bench

## LLM look-ahead bias (tier: papers)
- Glasserman & Lin 2023, arXiv 2309.17322: GPT sentiment backtests biased by look-ahead + "distraction effect"; anonymising company names helps. https://arxiv.org/abs/2309.17322
- Economics Letters 2025 (S0165176525004392): asking LLM for a ticker's return on a date, no context: avg 10% correlation with real daily returns, >40% for some blue chips; S&P500 monthly ~100%. https://www.sciencedirect.com/science/article/pii/S0165176525004392
- Benhenda 2026, arXiv 2601.13770 (Look-Ahead-Bench), via financealliance.io 2026-01-22: Llama 3.1 / DeepSeek >44% returns on 2021 stocks, DeepSeek drops ~22pp on mid-2024 post-cutoff data. Secondary source; paper not read in full.
- CFA UK community summary 2026-02-25: ~37% of apparent stock-return predictive effect from memorization; vanishes out-of-sample.
=> Conclusion: LLM decisions cannot be honestly backtested on past US large caps. Only forward (post-cutoff, live/sim) tests count.

## Wyckoff automation evidence
- LuxAlgo VSA page (undated, fetched 2026-11-16): "no rigorous public evidence that VSA is profitable on its own"; thresholds differ between authors. https://www.luxalgo.com/library/concept/volume-spread-analysis/
- paperswithbacktest Wyckoff page: struggles = subjective pattern recognition, hard to backtest, volume quantification; no direct Wyckoff backtest cited. https://paperswithbacktest.com/strategies/wyckoff-trading-strategy
- Wyckoff Analytics 2025-06-03 "Identifying Wyckoff Springs with Algorithms": spring = penetrate support (lookback lowest low 15/20/25 bars) without closing below, volume on penetration, bar must not close at its low (tail matters); caveat: if consecutive lows >10% apart stop placement impossible; NO test results given. https://www.wyckoffanalytics.com/identifying-wyckoff-springs-with-algorithmic-trading-strategies/
- Zhihu p/721687139 snippet: Wyckoff "主观性太强…不可能量化" (practitioner view).

## Chan automation evidence
- Sohu 2021-01-05 (间隙投研/琛实量化): 中枢 A→C cannot be known at A, only after turn at C; their fix: complete classification + recursion to confirm 走势完成 + multi-level resonance. No results shown. https://www.sohu.com/a/442588520_120754333
- Prior user R&D (Desktop/trading/rnd/kb/SYN-CHAN-2.md D34-D37): czsc finished-笔 lag +3 bars median, p90 +10, max +42; 43.2% of 笔 change endpoint (M15, XAUUSD). Measured on CFD, not stocks.
- Academic: only a deep-learning + 缠论 turning-point paper (scribd, unverified venue). No rigorous public Chan performance study found.

## Combining Wyckoff + Chan (practitioner, low tier)
- CSDN 2025-10-13 (body not retrievable, summary only): Wyckoff = market cycle/background, 缠论 = precise structural buy/sell points, order flow = real-time confirmation. https://blog.csdn.net/weixin_42480337/article/details/153217041
- Zhihu q/413598776 (SNIPPET ONLY, page behind login wall): "在缠论中枢这个'能量池'的边界上，用能量力度判断突破的真伪。放量突破代表能量爆发…对应威科夫SOS/JOC" => hypothesis: Chan 中枢 boundary + Wyckoff volume (effort/result) to validate 三买 breakouts.
- Zhihu answer (snippet): "缠论1买到2买的位置就是很好…" (win-rate framing).

## Multiple testing
- Bailey & López de Prado 2014, Deflated Sharpe Ratio (SSRN 2460551); Bailey et al. PBO (SSRN 2326253). Expected max Sharpe grows with N trials; track N.

## Coverage gaps
- Zhihu full answers (login wall), Reddit (firecrawl unsupported), X, Bilibili/Douyin videos not watched.
