# Prior R&D summary

Scope: local notes only (`D:/Desktop/trading/rnd/`). All cites are `path:line` relative to that root.
Sources read: S1 (czsc), S3 (Chan canon), S5 (WyckoffTradingAgent), S6 (wyckoff-own + course), kb/DECISIONS.md.

## Chan definitions + confirmation timing

- **笔 (czsc implementation).** `fx_a`→`fx_b` with opposite 分型, `!ab_include`, `bars_a.len() >= min_bi_len` (default 6 merged bars) (`00-study/S1-czsc.md:26`). Knowable when `fx_b` (the opposite 分型) supplies its 3rd bar, i.e. at `fx_b.elements[2].dt`, once length and non-inclusion hold (`00-study/S1-czsc.md:131-133`). 分型 itself is emitted one bar after its anchor (`00-study/S1-czsc.md:48-49`, anchor `k2` = t−1 at `:88`).
- **笔 (canon).** Adjacent 顶 and 底 joined, one K line belonging to neither fractal (`00-study/S3-chan-canon.md:22,46`). Confirmation = the third bar of the *opposite* 分型 satisfying adjacency + independent-K-line + direction (`00-study/S3-chan-canon.md:59`). The text never states a total minimum bar count; the 5/7-bar readings are inferences from an uninspected raster figure (`00-study/S3-chan-canon.md:22,102`).
- **线段.** Not implemented in czsc at all (`00-study/S1-czsc.md:36-37,92`). Canon: ≥3 笔 whose first three overlap (`00-study/S3-chan-canon.md:24,48`); ends at the 特征序列 分型 (`00-study/S3-chan-canon.md:48,61`); a gap between elements 1–2 requires the *reverse* 特征序列 分型 before the segment is confirmed (`00-study/S3-chan-canon.md:48,61`).
- **中枢.** Canon: overlap of ≥3 consecutive *completed* sub-level 走势类型 (`00-study/S3-chan-canon.md:25,37`); zone `ZG=min(g1,g2)`, `ZD=max(d1,d2)` (`00-study/S3-chan-canon.md:49`). Knowable only when the third sub-move completes (`00-study/S3-chan-canon.md:62`); ZG/ZD then frozen, but the 中枢 can later be re-labelled a higher-level one (`00-study/S3-chan-canon.md:62`). czsc: `zg`/`zd` from the first three 笔, frozen (`00-study/S1-czsc.md:30,93`), but `zs_list` is rebuilt on every read and includes 1- and 2-笔 pseudo-centres (`00-study/S1-czsc.md:71-74,149`).
- **背驰.** "Weaker force than the previous same-direction leg" (`00-study/S3-chan-canon.md:27`). The measure is left open: 均线-encircled area vs MACD area of A vs C, no MACD parameters given (`00-study/S3-chan-canon.md:27,51,105`). Knowable at C's completion (`00-study/S3-chan-canon.md:64`); the text's "×2 partial area" shortcut changes as the segment grows, i.e. repaints (`00-study/S3-chan-canon.md:64,95`). czsc recomputes 背驰 every bar with no anchor/freeze, label may flip (`00-study/S1-czsc.md:96`).
- **1B/2B/3B.** 1买 = completed decline + 背驰; 2买 = end of the first sub-level pullback; 3买 = end of the sub-level 回试 that stays above ZG (`00-study/S3-chan-canon.md:65`, also `:28`). 3买 inherits the 回试's own completion requirement (a reply amends this, `00-study/S3-chan-canon.md:65`); 1买/2买 are voided if the trend extends to a new extreme (`00-study/S3-chan-canon.md:65`). "必须是第一次" is asserted with no test (`00-study/S3-chan-canon.md:28`).
- **General knowability caveat.** The text claims 当下 analysis equals hindsight (`00-study/S3-chan-canon.md:68`), but every structure's *last* instance is revisable; only finished instances are stable (`00-study/S3-chan-canon.md:68`).

## Lag/repaint measurements

- **NOT VERIFIED:** these five files contain **no** measured czsc 笔 lag median/p90/max and **no** percentage of 笔 endpoints that change. S1 states the repaint *count* is undetermined and proposes the replay diff to get it (`00-study/S1-czsc.md:158-161`). Structural verdicts only are recorded (`00-study/S1-czsc.md:85-98`).
- czsc structural repaint verdicts: 合并K线/分型 tail-only; 笔 (first and provisional last) repaint; 中枢 `edt/edir/gg/dd` repaint; 背驰 and 买卖点 repaint; higher-级别 bucket bar is lookahead (`00-study/S1-czsc.md:87-98`). `zg`/`zd` are no-lookahead (`00-study/S1-czsc.md:93`).
- `finished_bis` gates the last 笔 behind `bars_ubi.len() < 5` (a bar counter, not a 分型 test), so a "confirmed" 笔 appears **1 笔 later** than `bi_list` (`00-study/S1-czsc.md:68-69,91,134`).
- Only concrete measured lag in DECISIONS: canon settled clock enters C1-07/08, C2-07/10, ED-05 a **median 253–278 bars** after the signal (~22 h on M5, 2.7 days on M15); for C1-08 the signal leg's own extreme was broken before entry in **54–58%** of trades (`kb/DECISIONS.md:28`).
- Wyckoff latencies: 3-bar pivot confirmation (`00-study/S6-wyckoff-own-and-notes.md:46`); ST confirmation ≤3-bar gap (`:50`); UT label unavailable until the fall-back close (`:56`); EVR 1-bar latency (`00-study/S5-wyckoff-trading-agent.md:86`); Markup crossover re-fires up to 10 days (`00-study/S5-wyckoff-trading-agent.md:90`); chart-label cluster thinning repaints (display only) (`00-study/S5-wyckoff-trading-agent.md:91`).

## Wyckoff definitions + quantification

- **SC.** Panic high-volume drop, usually largest volume (`00-study/S6-wyckoff-own-and-notes.md:21`). Code: down context (`C < EMA50`, 60-bar fall ≥8 ATR), 60-bar low, range ≥1.5 ATR, RV ≥2 or 60-bar RV max → `sc_candidate` (`00-study/S6-wyckoff-own-and-notes.md:34`); a lower low within 2 bars extends the cluster (`:34`).
- **AR.** Rebound after supply exhaustion; its high fixes TR_hi (`00-study/S6-wyckoff-own-and-notes.md:22`). Code: move ≥3 ATR off the extreme plus pivot high confirmed at `a_idx+3`, or a 33% retrace (`:34`).
- **ST.** Retest of SC low on visibly lower volume, holding above it; fixes TR_lo (`00-study/S6-wyckoff-own-and-notes.md:23`). Code: sits in `[SC_low, SC_low+1 ATR]`, lower low over 3 bars or bounce ≥1 ATR, max RV on AR→ST leg ≤0.7×RV_SC; TR then frozen (`:34`).
- **Spring.** Low-volume false break below TR_lo, quick reclaim, close inside (`00-study/S6-wyckoff-own-and-notes.md:24`). Code: first close back within `j=2`, `spring` if CLV ≥0.5 else `weak_close`; >1.5 ATR deep = `too_deep`; volume gate max RV outside / RV_SC ≤0.7 and break RV <1.5; one Phase C per range (`:36`). WTA legacy: support `[-62:-2]`, close back above, volume ≥1.3× 5-day mean and ≥1.15× yesterday (`00-study/S5-wyckoff-trading-agent.md:49-53`).
- **SOS/JAC.** Wide bull bar through TR_hi on expanding volume, long body, close high, no return (`00-study/S6-wyckoff-own-and-notes.md:25`). Code: close > TR_hi, body ≥0.8 ATR, CLV ≥0.7, mean RV ≥1.5, 1–3 bar aggregate, after a spring or `B_min`; failure = close < TR_hi − 0.25 ATR within 10 bars (`:36`). WTA: ≥6% day change, volume ≥3× 20-day mean **and** ≥60-day 95th pct, close ≥60-day high×0.99 or MA50 cross (`00-study/S5-wyckoff-trading-agent.md:26,55-56`).
- **LPS.** Low-volume small-step pullback to TR_hi after SOS (`00-study/S6-wyckoff-own-and-notes.md:26`). Code: ≥3 small-step bars, low in `[TR_hi−0.25, TR_hi+1] ATR`, close above their max high, mean RV ≤0.8 (`:36`). WTA: rising MA20, low within 2% of MA20 or 0.382–0.618 retrace of a ≥20% swing, 3-day median volume ≤0.65× prior 60-day median (`00-study/S5-wyckoff-trading-agent.md:27,58-61`).
- **UT/UTAD.** False break above TR_hi, back inside in 1–3 bars, next rally fails (`00-study/S6-wyckoff-own-and-notes.md:28`). Code: `ut` = high > TR_hi with fall-back close < TR_hi within `J_u=3`; `utad_confirmed` needs a lower pivot high confirmed at `x+3` then a close below `L[x+1]` (`:38`). WTA: high-level false breakout closing back below prior resistance (`00-study/S5-wyckoff-trading-agent.md:29`).
- **Phase logic.** WTA: close ≥ resistance → Markup; spring or LPS hit → Accum_C; close in bottom 35% → Accum_B; else Accum_A; **no D or E in code** (`00-study/S5-wyckoff-trading-agent.md:45-47`). wyckoff-own: SC→AR→ST builds a frozen TR, then spring (Phase C) → SOS → LPS (Phase D); distribution runs the same machine on negated prices (`00-study/S6-wyckoff-own-and-notes.md:34-38`).
- **Volume quantification.** `RV = V_i / median(previous 20 valid bars)`; AE = residual of `log V` on `log(range/ATR_prev)` over 250 bars; intraday de-seasonalised by clock slot; only ratios compared across bars (`00-study/S6-wyckoff-own-and-notes.md:40`). WTA TR: median of last 5 swing lows/highs in a 90-bar window, else 10%/90% quantile (`00-study/S5-wyckoff-trading-agent.md:24`).

## Prior decisions

- KB-U3: no time cap; 缠论 canon has no price stop, exits at the same-level sell point (`kb/DECISIONS.md:9`).
- KB-U4 / KB-U9: short trades in scope; 缠论 sell points become separate short rows/trials (`kb/DECISIONS.md:10,15`).
- KB-U8: multi-level mappings H1/M15/M5 and M15/M5/M1, each its own cell (`kb/DECISIONS.md:14`).
- KB-H1: by the canon there is no 中枢 below 线段 (L064:26), so chan.py's default 笔-level points are 类 points; 线段-level list CP-S01..S06 added (`kb/DECISIONS.md:18`).
- KB-H2: canon variants bounded by structure counts are built on M5 only; H1/M15 cells recorded "thin by bound" (`kb/DECISIONS.md:19`).
- KB-H3: emit/settled matching rule = same anchor ±2 bars, same timeframe/direction, on first-seen bar (`kb/DECISIONS.md:20`).
- KB-U12 / freeze: duplicate only if each set ≥80% inside the other; K = 114 (`kb/DECISIONS.md:21,31-58`).
- KB-U15: 二买 invalidation implemented both ways (last 中枢 dd vs 1买 low), both count in Holm K (`kb/DECISIONS.md:26`).
- KB-H6: settled-clock lag 253–278 bars and other Chan representation defects (segment adapter stamps 笔 中枢, CZ-10 uses 笔 stop vs source's 50 BP) to be fixed in S1-A2 (`kb/DECISIONS.md:28`).
- Stage 1: 0 keeps of 120; no pre-cost edge; best Holm LB net4 −16.12 bps (`kb/DECISIONS.md:75-94`). S1-A2: 0 keeps of 192 (`kb/DECISIONS.md:106-124`).
- KB-U18: continue with Chan and Wyckoff; next = detection-fidelity review + RND-02 on intended H4/D1 (`kb/DECISIONS.md:125`).
- KB-U19: Dukascopy H1 candles approved for RND-02b (`kb/DECISIONS.md:129`).
- RND-02a 0/27 (`kb/DECISIONS.md:136`); RND-02b 0/30 (`kb/DECISIONS.md:144`); cumulative 369 versions, none kept (`kb/DECISIONS.md:150`).
- Owner after RND-02b: no fixes now, observe on demo first (`kb/DECISIONS.md:155-160`).
- Data seal: 2023+ gold ticks are on disk but the seal is a rule — every reader must filter by date (`kb/DECISIONS.md:165-166`).

## Pitfalls

- Do not use `finished_bis`'s `bars_ubi.len() < 5` as a *definition* of a confirmed 笔; it is a bar counter (`00-study/S1-czsc.md:145-146`).
- Do not run a higher-级别 CZSC on the unclosed bucket bar (`00-study/S1-czsc.md:147-148`); do not treat `zs_list` as a stable structure list (`:149-150`); do not read `bi_list[-1]` without a `bars_ubi` gate (`:151-152`); the 1.618×mean(笔力度) constant is unexplained (`:153-154`).
- Do not carry canon "100%安全"/"绝对" claims into SPEC — unfalsifiable (`00-study/S3-chan-canon.md:94`); do not adopt the ×2 MACD-area estimate, it repaints by construction (`:95`).
- Do not use the owner-notes 2买 price-pattern paraphrase (`00-study/S3-chan-canon.md:96`); do not cite the 阿娇 "新笔定义" appendix as 原文 (`:97`); do not prefer reply authors over lesson bodies (`:98`).
- Score-returning detectors cannot carry anchor/direction/confirmation/invalidation (`00-study/S5-wyckoff-trading-agent.md:150-152`); tuned constants (6%, 3×, 95th pct) are one author's A-share sample (`:153-154`); phases as prose import an unconfirmed label (`:155-156`); unfrozen TR levels let an event vanish next day (`:157-159`); cluster thinning by max score uses later bars (`:160-161`).
- Do not treat the owner's ATR/RV defaults as the author's (`00-study/S6-wyckoff-own-and-notes.md:91`); the 3–10 ATR range qualifier is the owner's construction, not Wyckoff (`:92`); do not key on the SC bar alone — the TR may use a deeper cluster low (`:93`); `video-notes/` is md5-identical to `notes/` (`:95`).
- Course labels SC/AR/ST/spring/UTAD/LPSY are hindsight: a later bar decides validity, no confirmation bar stated (`00-study/S6-wyckoff-own-and-notes.md:60`).

## Open questions

- czsc: does tail-笔 revision propagate past the last 笔? repaint rate under intrabar vs closed-bar updates? does `zg`/`zd` from the first three 笔 match the lineage? does `bars_raw` trimming shorten MACD warm-up? (`00-study/S1-czsc.md:158-164`).
- 笔 minimum total bar count unresolved; whether the 阿娇 appendix is blog or compiler text; whether 走势类型 完成 is decidable without the next bar; MACD parameters; the 级别 set (`00-study/S3-chan-canon.md:102-106`).
- wyckoff-own: do detectors replay cleanly (tests not run); the course's exact confirmation bar per event; tick volume vs traded gold volume; provenance/licence of `wyckoff-own` (`00-study/S6-wyckoff-own-and-notes.md:99-103`).
- WTA: HK/US volume units; whether SOS/LPS are enabled in any deployed config; whether an intraday run would revise a label (`00-study/S5-wyckoff-trading-agent.md:165-173`).
- Untested exploratory lead: canonical/repo short sell points beat random shorts gross (t 1.8–2.5) but lose net; owner decision pending (`kb/DECISIONS.md:151-152`).