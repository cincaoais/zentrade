# chanlun-pro review + DSA chan strategies

Sources (local only, no download):
- A: `D:/Desktop/GitHub/daily_stock_analysis/.claude/reviews/rnd/02-chanlun-pro.md` (32 lines)
- B: `D:/Desktop/GitHub/daily_stock_analysis/strategies/chan_*.yaml` (6 files)

## chanlun-pro: what it is, strategy rules, signal timing / repaint findings, results claimed (cite line numbers of the review file)

**What it is (VERIFIED in A):** a Python Chan (缠论) library, cloned to `D:\Desktop\GitHub\_dsa_rnd\chanlun-pro` (A:3). Only an open wrapper is Apache-2.0; the actual Chan computation is closed (A:11-15). `src/chanlun/cl.py` (46 lines) just dispatches by platform to `chanlun.core.<platform>.cl_fast`; `src/chanlun/core/` holds one `cl.py` + `cl_fast.py` pair per platform (windows_x86_64, linux_x86_64, darwin_x86_64, darwin_aarch64) (A:12-14).

**Licensing / runtime:** requires a machine-bound licence key. `src/pyarmor_runtime_005445/pyarmor.rkey` is absent; without it startup fails with `RuntimeError: missing license key to run the script`; trial key = 20 days via author on WeChat; formal key bound to machine via `python -m pyarmor.cli.hdinfo` (A:16-19). `AI-INSTALL.md:27-28` forbids decrypting, modifying or decompiling `src/chanlun/core/**` and copying the key between machines (A:20-21). Only compiled artefact present is `core/windows_x86_64/__pycache__/cl_fast.cpython-311.pyc`, so supported interpreter is Python 3.11 (`AI-INSTALL.md:261` prints `Python: 3.11 (CL Fast)`); target repo tests on 3.12 (A:22-24). Open/readable: `src/chanlun/cl_interface.py` (1,434 lines) — abstract data model (分型/笔/线段/中枢/背驰/买卖点 classes and config keys) (A:25-26).

**Verdict:** "Not embeddable. Use it only as a reference for definitions and naming." (A:7)

**Strategy rules / signal timing / repaint findings / results claimed:** NOT VERIFIED — the review (A:1-32) reports none of these: no entry/exit rules, no timing or bar-close confirmation / repaint discussion, and no backtest, win rate or return anywhere.

## DSA strategies (one subsection per yaml, cite file:line)

All are prompt/instruction specs that only *interpret* a program-computed `get_chan_structure` snapshot (B:chan_theory.yaml:2,22-23); they are not code.

### chan_divergence_reversal.yaml (背驰转折, 一买/一卖)
- Entry: only `divergence` and `buy_sell_points` of type `1B`/`1S` on the operating (D) level (l.18,21); `trend_divergence` trustworthy, `range_divergence` = half position (l.22-23); `confirmed=false` = watch only, wait for 60M/30M confirmation (l.24-25).
- Exit: symmetric `1S` top divergence (l.27); confirmed valid `1S` ⇒ score −15 (l.33).
- Invalidation: `invalidation_price` = the 1B low; break ⇒ divergence failed, stop out (l.27); `still_valid=false` ⇒ score −10 (l.34).

### chan_multi_level.yaml (多级别联立 / 区间套)
- Entry: top-down W→D→60M/30M, each level marked `confirmed` (l.21); act when `cross_level.resonance=full`; `partial` must name the divergent level; `none`/`unknown` ⇒ no direction call (l.22-23). W+D aligned + valid D buy ⇒ +12 (l.31).
- Exit: big level up + small-level sell = reduce, not clear (l.27); big level down + small buy = rebound only, target ≤ nearest zhongshu (l.26).
- Invalidation: small-level `invalidation_price` for counter-trend trades (l.26); missing/`partial` levels must be named and lower confidence (l.28).

### chan_second_buy.yaml (二买/二卖)
- Entry: only `2B`/`2S` with `basis=after_1B`/`after_1S` (l.18); 2B pullback low must be above the 1B low (l.21); `confirmed=false` ⇒ conditional entry (l.22); 2B+3B at same `dt` ⇒ raise size (l.24).
- Exit: 2S = rally high below the 1S high, reduce/exit for holders (l.25).
- Invalidation: stop below `invalidation_price` (the 1B low); break ⇒ 1B failed, 2B void (l.23); if W is down without divergence, treat D 2B as rebound, target nearest ZD (l.26).

### chan_theory.yaml (default 缠论综合)
- Entry: default router/strategy (l.14-16); structure only from the snapshot, never re-count bars yourself (l.22-23); D = operating level, W = direction, 60M/30M = timing (l.27); use only `still_valid=true` points — 1B/1S from divergence, 2B/2S from pullback-not-breaking, 3B/3S from leaving zhongshu and not returning (l.33-35).
- Exit: valid sell point or price below a buy's invalidation ⇒ score −12 (l.44); sizing by `cross_level.resonance` — full = normal, partial = half, counter-trend = light short-term (l.36-37).
- Invalidation: stop at the point's `invalidation_price`; targets from `key_levels` and the upper zhongshu's ZD/ZG (l.36); no valid point ⇒ "no trade / hold and watch" (l.35).

### chan_third_buy.yaml (三买/三卖, 中枢突破回踩)
- Entry: only `3B`/`3S` with `basis=pivot_exit` (l.18); 3B = up-leave zhongshu, pullback low does not fall below ZG (l.21); closer to invalidation = better R:R, don't chase when extended (l.22); `trend_type=up` + W aligned = continuation (l.24).
- Exit: 3S = down-leave, rebound fails to reach ZD ⇒ must exit (l.25); top divergence or same-level 1S after 3B ⇒ reduce, don't add (l.23).
- Invalidation: `invalidation_price` = the zhongshu's ZG (l.21); `still_valid=false` ⇒ price fell back into the zhongshu, uptrend thesis void, treat as oscillation (l.26).

### chan_zhongshu_oscillation.yaml (中枢震荡)
- Entry: only when operating level `position_vs_last_zhongshu=inside` or `trend_type=consolidation` (l.18); low-absorb near ZD/DD only if 60M/30M also shows a buy (l.22); mid-zhongshu ⇒ wait/observe (l.23).
- Exit: near ZG/GG with a small-level sell ⇒ reduce (l.22).
- Invalidation: stop below DD for longs; break DD and rebound not back to ZD ⇒ 3S, oscillation assumption void; don't treat a break as a trend until a 3B forms (l.24-25).

## Open questions the review leaves

- Whether chanlun-pro's outputs (笔/线段/中枢/背驰/买卖点) are stable/confirmed at bar close and therefore non-repainting — never tested or reported in A (A:28-32). NOT VERIFIED.
- Any performance/accuracy claim for its signals — none recorded in A (A:1-32). NOT VERIFIED.
- How its 线段 / 走势类型 / 类二类三买卖点 definitions compare to czsc — explicitly not done; A says to read `cl_interface.py` when designing the snapshot schema (A:31-32).
- Whether it can run here at all: no run attempted, no licence key requested (A:30); the Python 3.11 vs 3.12 mismatch (A:22-24) is unresolved.
- Whether `cl_interface.py`'s model can emit the fields DSA's YAMLs expect (`divergence`, `buy_sell_points`, `zhongshu`, `cross_level`, `invalidation_price`; B:chan_theory.yaml:27-37) was not checked. NOT VERIFIED.