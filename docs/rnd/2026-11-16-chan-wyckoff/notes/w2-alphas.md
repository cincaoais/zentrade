# popbo/alphas

## Identity
- url: https://github.com/popbo/alphas
- last commit: 2023-04-14 (`git log -1`: 620594a, 2023-04-14 21:26:32 +0800); pushed_at 2023-04-14T13:26:33Z
- stars: 604; license: none (GitHub API `license.spdx_id` = null, no LICENSE file in `git ls-files`); language: Jupyter Notebook
- source: `gh api repos/popbo/alphas --jq {stars,pushed,license,lang}`

## What it is (max 3 lines)
Chinese A-share cross-sectional factor research toolkit: downloads unadjusted daily bars (akshare/baostock), computes WorldQuant Alpha101 and GTJA Alpha191 factors, evaluates them with a vendored alphalens 0.4.0, and backtests a top-quantile portfolio in backtrader.

## Strategy rules as implemented
- Chan / Wyckoff: none. `grep -ril -E "缠|中枢|背驰|wyckoff|spring"` over all tracked files returns no matches; "signal" only appears in vendored alphalens docstrings/comments (e.g. `alphalens/utils.py:121`).
- Universe: current HS300 constituents from baostock at the start of each year (`datas.py:94-118`), alternatively ZZ500 (`datas.py:68-92`).
- Data: unadjusted prices (`data_bfq`) — `datas.py:20`, read at `alphas.py:45`; vwap computed as amount/volume/100 (`alphas.py:73`).
- Indicator/signal: Alpha101 formulas only; each `alphaNNN` method returns a cross-sectional score per date (`alphas101.py:207-210` for alpha001; 83 methods total). The notebook demo uses Alpha#83 `(rank(delay((high-low)/(sum(close,5)/5),2)) * rank(rank(volume))) / (((high-low)/(sum(close,5)/5))/(vwap-close))` (`alphas101.py:637-639`).
- Ranking: percentile rank across stocks on each date row (`rank` at `alphas101.py:141-148`).
- Entry: keep only factor_quantile 5 rows, fixed weight `1/60`, rebalance on every factor date (`2.backtrader.ipynb` cell 5, lines 27-33); `order_target_percent(target=w*0.95)` in `next()` (`2.backtrader.ipynb` cell 8, next()).
- Exit: `self.close()` any holding absent from the new long list, and cancel unfilled orders (`2.backtrader.ipynb` cell 8, next()).
- Stop / take-profit: none — no `stop`, 止损 or 止盈 in any repo `.py` file (grep), and no stop orders in the strategy.
- Survivorship: fixed year-start index membership is used for the whole year (`datas.py:102`; `2.backtrader.ipynb` cell 3).

## Signal timing
- Factor at bar t reads bars up to and including t: `inner = self.close.copy()` (`alphas101.py:208`) and rolling ops such as `ts_sum(...,5)` (`alphas101.py:638`); no `.shift(-1)` anywhere in `alphas101.py`/`alphas191.py` (grep `shift\(-` only hits `alphalens/utils.py:293`).
- Output is written per date row (`alphas.py:18`, `alphas.py:117`), and the factor date itself is reused as `trade_date` (`2.backtrader.ipynb` cell 5, line 31: `trade_date` rename) with no lag.
- Orders are market orders submitted in `next()`; backtrader's default is next-bar-open fill because the notebook never sets `cheat_on_open` (`2.backtrader.ipynb` cell 10) — NOT VERIFIED from repo code, framework default only.
- Repaint: none in the factor code — all rolling windows are backward-looking (`alphas101.py:33`, `141-148`, `159-162`), cross-sectional rank uses only the same date row, and there is no `bfill`/`backfill` (grep).
- Mild future-data use in evaluation only: alphalens drops factor dates whose forward return is missing (`alphalens/utils.py:293-294`), so the last available bar is excluded; quantiles themselves are computed from factor values only (`alphalens/utils.py:166-170`).

## Results claimed
- 2020 HS300 (300 names), Alpha083 Q5 portfolio: annual return 0.176225 (17.62%), mean daily 0.000725, annualized 0.200516 (20.05%), max drawdown -14.850403%, Sharpe 0.854941 (`2.backtrader.ipynb` cell 12 output table).
- Cost assumptions: commission line is commented out, i.e. zero fees and no slippage (`2.backtrader.ipynb` cell 10); cash set to 100000.0 although the comment says 100,000,000 (same cell).
- No other numeric metric claims found in scanned notebook outputs (1.research / 3.factorycompare); these contain alphalens charts and factor rank tables, not IC values. "none found" beyond the above.

## Usefulness for a US-stock daily-bar Chan+Wyckoff system (max 3 lines)
OPINION: Near-zero direct value — there is no Chan or Wyckoff logic here, only Alpha101/191 formulas on Chinese A-share data.
OPINION: Useful only as a reference for building a wide cross-sectional factor panel and alphalens evaluation harness; its factor ideas could serve as confirmation filters, not structural signals.
OPINION: Do not reuse its backtest as-is: unadjusted prices, year-fixed universe, no fees, one-year single-fold result.