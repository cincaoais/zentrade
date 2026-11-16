# Wyckoff detection repos

## Search log
- `gh search repos "wyckoff" --limit 20 --json fullName,stargazersCount,pushedAt,description,url --sort stars` → 20 hits; ~11 trading, 4 crystallography (Wyckoff *positions*), rest prompts/ML/old.
- `gh search repos "wyckoff backtest" --limit 20 --json ...` → 1 hit (RinNguyens/rinng, 0★).
- `gh search repos "wyckoff spring" --limit 20 --json ...` → 1 hit (Lhutor, 0★).
- `gh search repos "wyckoff accumulation" --limit 20 --json ...` → 4 hits, all 0★.
- Cloned 10 candidates `--depth 1` into `D:/Desktop/rnd-scratch-2026-11-16/task_7ddecf6480be/`; READMEs read there.
- Noise: "wyckoff" also means crystallographic Wyckoff positions (4 repos) — filtered out.

## Table: repo | stars | last push | events coded | timeframe | backtest numbers? (cite)
| Repo | ★ | Last push | Events coded | Timeframe | Backtest numbers? |
|---|---|---|---|---|---|
| srlcarlg/srl-ctrader-indicators | 96 | 2026-09-05 | None named; Weis wave effort/result only (README:8; no spring/climax in `*.cs`) | Any cTrader TF | No |
| srlcarlg/srl-python-indicators | 63 | 2026-01-01 | None named; Weis wave volume/effort-result metrics (README:218-223; `weis_wyckoff_system.py:113-136`) | Any OHLCV; 1m examples (README:10-14) | No |
| Ju571nK/ChartNagari | 28 | 2026-11-15 | Accumulation, Distribution, Spring, Upthrust, Volume anomaly (`internal/methodology/wyckoff/`) | Any chart TF + MTF (README:3) | No numbers; "run backtests" only (README:16,18) |
| mrhustlex/Trading-Masters-Strategies | 28 | 2026-08-17 | Range + Spring only in code (`wyckoff.py:31-37`); SC/AR/ST/SOS/LPS prose only | Weekly (README:29; `wyckoff.py:13`) | No |
| systematic1/wyckoff-scanner | 0 | 2026-11-11 | SC/BC, AR, Acc/Dist box, Spring/UTAD, SOS/SoW (README:74-79,88-93) | Daily (README:3) | No; "heuristics" (README:98) |
| loopofM/wyckoff-indicator | 10 | 2024-12-14 | BC, SC, Accumulation, Distribution (README:3,12-14; code:48-58) | Chart TF + MTF (README:8-10) | No |
| Musyimi97/phase404 | 19 | 2026-04-07 | Wyckoff AMD phase + SMC (README:14,144) | MT5 / Pine | No; only target thresholds (README:399-402) |
| signal-validation/wyckoff-validation | 0 | 2026-05-14 | n/a — proprietary engines, no detection code (README:4-6) | Daily + Weekly | YES: Daily CAGR 12.98%, Sharpe 0.762, MaxDD -22.30%, 4,276 signals; Weekly 13.48%, 0.94, -22.74%, 2,533 (README:11-14) |

## Notes on the top 5 (max 6 lines each, cite README lines)
### 1. srlcarlg/srl-python-indicators (63★) — Weis waves, not event rules
- README only exposes `WeisWyckoffSystem` + ZigZag (README:218-223); no event list.
- Code adds 17 wave columns: end_wave, wave_volume, wave_effort_result, comparisons (weis_wyckoff_system.py:113-136).
- No spring/SC/UT/SOS/LPS/phase detector in the 633-line module (grep: zero hits).
- Timeframe: any OHLCV; examples use 1-minute parquet (README:10-14). No backtest, no numbers.
- cTrader repo is the C# twin (srl-ctrader README:5,8); same conclusion, no named events in `*.cs`.
- Useful as an effort-vs-result feature library, not as a Wyckoff event detector.

### 2. Ju571nK/ChartNagari (28★) — real Go rule engine, no published results
- GitHub description says "ICT/Wyckoff signal detector — 30+ rules"; README itself never names Wyckoff events (0 grep hits).
- Five Wyckoff rules in `internal/methodology/wyckoff/`: accumulation, distribution, spring, upthrust, volume_anomaly.
- Spring: 5-bar low pierces SWING_LOW, close back above, recovery vol ≥1.5× MA20 (spring.go:11-23).
- Accumulation: close<EMA50, 20-bar range<8%, vol<MA20 → LONG (accumulation.go:12-21).
- TF: per-analysis TF with multi-TF penalty; stocks/FX/crypto (README:3). Scores+cooldowns, alerts.
- Backtest: README:16,18 says backtests/paper trades exist; no numbers or equity curve published.

### 3. mrhustlex/Trading-Masters-Strategies (28★) — weekly stock spring scanner
- Wyckoff is 1 of 12 masters; script `skills/wyckoff/references/wyckoff.py` (README:29).
- Coded: 30-week range <25% + spring = 8-week low <0.98×support, close back >support, on lower volume (wyckoff.py:31-37).
- SKILL.md describes PS/SC/AR/ST, Spring, SOS/LPS, UT/UTAD (SKILL.md:22-25,31) but as prose, not code.
- TF weekly, 2y yfinance (`interval="1wk"`, wyckoff.py:13-20); stocks.
- No backtest, no numbers; README:101 only warns about free 1-min limits.
- Low detection depth: only range+spring, no phase/event state machine.

### 4. systematic1/wyckoff-scanner (0★) — most complete event chain found
- Daily Yahoo OHLCV → PostgreSQL → scans accumulation AND distribution sequences (README:3-4).
- Numeric chain: SC/BC (≥15% move, ≥1.3× vol), AR (≥6%), 10-bar box, Spring/UTAD (0.3-6% pierce), SOS/SoW (README:74-79).
- Emits flags: likely progressing, Spring, SoS, Upthrust, SoW (README:88-93).
- TF daily only (README:3); C# .NET 8; all thresholds in `AnalyzerOptions`.
- No backtest; explicitly "rule-of-thumb heuristics for screening" (README:98); `WyckoffTester` is a manual quote checker.
- Best rule spec to copy; 0★/no tests is the risk.

### 5. loopofM/wyckoff-indicator (10★) — Pine, BC/SC only
- Pine v5 detects BC and SC plus accumulation/distribution phase (README:3,12-14).
- BC/SC = volume spike at swing high/low, filtered by EMA trend + MTF; labels/alerts (README:74-79; code:51-58,66-70).
- Accumulation = close<SMA, ATR<SMA, uptrend; distribution mirrored (code:48-49).
- TF: chart TF with MTF trend confirmation (README:8-10).
- No backtest, no numbers; last push 2024-12-14.
- Simple, readable, but no Spring/UT/SOS/LPS.

## Repos skipped and why (one line each)
- YoungCan-Wang/WyckoffTradingAgent — already studied, skipped per task.
- douchunzhou/Wyckoff-Reader (67★) — LLM report bot; main.py only sets "You are Richard D. Wyckoff" prompts (main.py:370,679), no event detection.
- XMall0808/Wyckoff-1Min-Reader (19★) — same LLM bot/fork for 1-minute A-shares.
- YoungCan-Wang/wyckoff_skill (48★) — prompt/skill text, no detection code.
- Eesita/Wyckoff-AI-Assistant (27★) — LLM assistant, no rules.
- huawenmumu/wyckoff_multi_agent_analysis (11★) — multi-agent LLM wrapper.
- wangshouren000/wyckoffTrade (18★) — books/notes only, 7-line README, no code.
- AlexMGalvez/wyckoff-ai-model (9★) — LSTM time-series model, no Wyckoff event rules.
- SymmetryAdvantage/WyckoffTransformer, httk/wyckoffdiff, RaymondZhurm/WyCryst, LiuQH-lab/FindSpinGroup — crystallography, not trading.
- MurrayBakerWebDeveloper/Wyckoff-Trader (12★) — 2015 forex charting toy, no detection/backtest.
- zslava/wyck (10★) — 2018 project notes; not reviewed (NOT VERIFIED).
- Lhutor/indicadorr-trend-wychoff (0★) — README is MA trend indicator; Wyckoff only in description (NOT VERIFIED in code).
- gamaulia-cpu/mangkok-nasi-screener, INDDAP/PhaseSniper, RinNguyens/rinng (0★) — not reviewed; 0 stars, no signal (NOT VERIFIED).
