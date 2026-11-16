# AdvancingStone/stock-learning

## Identity
- URL: https://github.com/AdvancingStone/stock-learning
- Last commit: 2025-06-27T02:01:44Z (066e8b9 "Update README.md"); verified via `git log -1`
- Stars: 743; license: `null` in GitHub API; README shows an "MIT" badge (README.md:7) but there is **no LICENSE file** in the repo (`ls LICENSE*` empty)
- Main language: HTML (gh api)
- Clone: `git clone --depth 1` into D:/Desktop/rnd-scratch-2026-11-16/w1/stock-learning (29 MB, dominated by `picture/`)

## What it is (max 3 lines)
A Chinese A-share **reading collection**, not a code repo: 16 Markdown articles (K-line, trend, 江恩/Elliott, chip theory, 量价, 庄家/主力 analysis, 通达信 indicator docs) plus HTML mirrors and ~200 screenshots.
Only "code" assets are three encrypted Tongdaxin indicator files under `指标公式/` (`乾坤启动点.tn6`, `庄影追踪.tni`, `百日地量.tni`); they are binary (`file` => "data"), not readable source.
No Python/JS/notebooks/CSV/JSON exist (`find ... -name '*.py' -o '*.js' -o '*.ipynb' ...` => empty). This is a docs repo, not a system.

## Chan / Wyckoff coverage
- `grep -ril -e chan -e 缠 -e 中枢 -e 背驰 -e wyckoff -e spring -e accumulation` => no hits except the character 缠 inside `picture/image-20200321214112306.png` (an image, unreadable text).
- `笔` hits are ordinary Chinese ("一笔成交量/一笔大资金"), not Chan 笔: e.g. article/11.量价理论及实盘解析.md:79, article/4.筹码理论.md:102.
- No Wyckoff terminology at all (no Spring/Upthrust/SOS/LPS/Phase). Closest analogue is discretionary 主力 phase talk: 吸筹/洗盘/拉升/出货 in article/7.庄家分析.md:27, article/11.量价理论及实盘解析.md:19-53.

## Strategy rules as implemented
Nothing is implemented in executable form; rules below are prose prescriptions only.
- Accumulation / volume dry-up: 地量 = extreme volume contraction, typical at 熊市末期吸筹 or 二次吸筹 (article/11.量价理论及实盘解析.md:108). "百日低量" = new lows with shrinking volume until volume cannot shrink more, a bear-market-end precursor (article/12.百日地量操盘绝技.md:5).
- Volume expansion: 温和放量 = staircase +10%–15% volume, at breakout of resistance after the doldrums or after a washout (article/11.量价理论及实盘解析.md:111).
- Selection rule (百日地量超级选股): today's 涨停 (limit-up) AND within the last 5 trading days volume was below the indicator's green "芝麻线", AND today's volume breaks the white 60-day average volume line (article/13.百日地量超级选股.md:11).
- Entry: buy on a red/negative candle, low open, on pullback, not on a green gap-up ("买阴不买阳，买低开不买高开，回踩买入") (article/13.百日地量超级选股.md:43).
- Exit: hold 1–2 weeks, max two weeks, or take profit at +20% (article/13.百日地量超级选股.md:43).
- MACD entry (乾坤启动点): signal only below MACD zero axis; yellow bar/hand = first buy; green bar disappearing and red bar (golden cross) = second buy (article/15.乾坤启动点.md:5-9).
- Stop rule: only a discretionary note — if the signal fails, stop out and re-buy when the red bar appears (article/15.乾坤启动点.md:21); no numeric stop.
- Leader-follower positioning (庄影追踪): red-chip cluster = institutional absorption; purple 庄家 line rising = accumulation, falling = distribution; cyan 散户 line rising = retail chasing; entry when the purple line crosses up through the red absorption signal (article/14.庄影追踪.md:29-39).
- Washout-end heuristic: 地量 also marks the end of 庄家震仓洗盘 and pre-markup lock-up (article/12.百日地量操盘绝技.md prose, lines 9-13 block).

## Signal timing
- No executable code, so there is no bar-level timing to quote and no `REF(`/look-ahead question to settle. All rules are discretionary text plus screenshots.
- The only quasi-formal trigger is the limit-up-day screen: it reads volume `<` 60-day average **on the signal day** and scans back 5 bars for the dry-up bar (article/13.百日地量超级选股.md:11) — this is a same-bar/backward window, so it does not look ahead; forward 1–2 week holding is prescribed at article/13.百日地量超级选股.md:43.
- Whether the encrypted `.tn6/.tni` formulas repaint or use future bars is **NOT VERIFIED** — the files are binary/obfuscated (`file` => data; GBK decode fails); no source is published. Do not assume.
- 乾坤启动点 is described as a MACD-parameter optimization with fewer signals, explicitly "below zero axis only" (article/15.乾坤启动点.md:1,9); exact MACD parameters are **NOT VERIFIED** (only in the encrypted file).

## Results claimed
- none found. No backtest, no equity curve, no win rate, no sample size, no period.
- Only marketing/anecdotal claims with screenshots: "每次吸筹都要涨一波" (article/14.庄影追踪.md:41-49) and example tickers 中国软件/华谊兄弟/证通电子 (article/14.庄影追踪.md:45-53).

## Usefulness for a US-stock daily-bar Chan+Wyckoff system (OPINION)
- OPINION: Low. It contains zero Chan structure code (no 笔/线段/中枢/背驰) and zero Wyckoff event detectors, so nothing can be ported or validated.
- OPINION: Salvageable only as vocabulary for volume-dry-up-in-accumulation ("百日地量") and limit-up + volume-expansion entry, which already exist in standard Wyckoff/relative-volume literature with better rigor.
- OPINION: Treat this repo as untrusted marketing prose (A-share limit-up rules do not transfer to US daily bars; +20%/2-week exit is arbitrary), not as a source of signal logic.