#!/usr/bin/env bash
# Generates w1..w8 worker specs. Run from the specs folder.
set -e
N=D:/Desktop/GitHub/zentrade/docs/rnd/2026-11-16-chan-wyckoff/notes
C="$(cat _common.txt)"

read -r -d '' REPO_TPL <<'EOF' || true
TEMPLATE:
# <owner/repo>
## Identity
url, last commit date, stars, license, main language. Get them with:
  gh api repos/OWNER/REPO --jq "{stars:.stargazers_count,pushed:.pushed_at,license:.license.spdx_id,lang:.language}"
## What it is (max 3 lines)
## Strategy rules as implemented
One bullet per rule (Wyckoff event / Chan structure / indicator / entry / exit / stop), each with file:line.
## Signal timing
At which bar does a signal fire? Does any rule read bars after the signal bar, or redraw past structure (repaint)? Quote the code lines that decide this.
## Results claimed
Any backtest numbers: metric, value, market, period, file:line. If none: "none found".
## Usefulness for a US-stock daily-bar Chan+Wyckoff system (max 3 lines, label it OPINION)
EOF

repo_spec() { # id repo slug
  cat > "$1.txt" <<EOF
$C

TASK $1: study the GitHub repo https://github.com/$2
Clone it (git clone --depth 1) into D:/Desktop/rnd-scratch-2026-11-16/$1/. Find files about 缠论/Chan, Wyckoff, or trading signals and read only those (max ~10 files). If the repo is large, use grep -ril for: chan 缠 笔 中枢 背驰 wyckoff spring accumulation signal backtest.
Write notes to: $N/$1-$3.md

$REPO_TPL
EOF
}
repo_spec w1 AdvancingStone/stock-learning stock-learning
repo_spec w2 popbo/alphas alphas

cat > w3.txt <<EOF
$C

TASK w3: find GitHub repos that COMBINE Wyckoff and 缠论 (Chan theory) in one system or strategy.
Run these searches (gh CLI is logged in), each with: --limit 15 --json fullName,stargazersCount,pushedAt,description,url
  gh search repos "wyckoff chan"
  gh search repos "wyckoff 缠论"
  gh search repos "威科夫 缠论"
  gh search repos "wyckoff chanlun"
  gh search repos "威科夫"
For the up-to-5 most relevant hits that really combine both methods, read ONLY the README:
  gh api repos/OWNER/REPO/readme --jq .content | base64 -d
Record how they combine the two methods.
Write notes to: $N/w3-combined-repos.md
TEMPLATE:
# Repos combining Wyckoff + 缠论
## Search log (each query + number of hits)
## Candidates table: repo | stars | last push | one-line what | combines both? (yes/no/partly)
## How the real combiners combine them (per repo, max 6 lines, cite README lines)
## Nothing found? say so plainly
EOF

cat > w4.txt <<EOF
$C

TASK w4: find GitHub repos that DETECT Wyckoff events (spring, selling climax, upthrust, accumulation/distribution phases, SOS, LPS) in code, preferably on stocks, preferably with a backtest.
Already studied, skip: YoungCan-Wang/WyckoffTradingAgent.
Searches, each with: --limit 20 --json fullName,stargazersCount,pushedAt,description,url
  gh search repos "wyckoff" --sort stars
  gh search repos "wyckoff backtest"
  gh search repos "wyckoff spring"
  gh search repos "wyckoff accumulation"
Skip repos that are only courses, drawings or LLM prompts with no detection code (list which you skipped).
For the top 5 by relevance+stars, read the README only and note: events coded, timeframe, whether a backtest with numbers is shown (copy numbers + README line).
Write notes to: $N/w4-wyckoff-detectors.md
TEMPLATE:
# Wyckoff detection repos
## Search log
## Table: repo | stars | last push | events coded | timeframe | backtest numbers? (cite)
## Notes on the top 5 (max 6 lines each, cite README lines)
## Repos skipped and why (one line each)
EOF

cat > w5.txt <<EOF
$C

TASK w5: find GitHub repos that BACKTEST 缠论 (Chan theory) buy/sell points (一买/二买/三买, 背驰) and publish results.
Already studied, exclude: waditu/czsc, zengbin93/czsc_skills, Vespa314/chan.py, chanlun-engine-skill, chanstock-skill, JustinGongjj/chan-structure-workstation, stockServ/chzhshch-108-plus, cincaoais/chanlun-pro.
Searches, each with: --limit 20 --json fullName,stargazersCount,pushedAt,description,url
  gh search repos "缠论 回测"
  gh search repos "chanlun backtest"
  gh search repos "缠论 策略"
  gh search repos "chan theory trading"
For the top 5 by relevance+stars, read the README only. Record: market (A-share/US/crypto/futures), timeframe, which buy points, whether results are shown (copy numbers + README line), and whether the README mentions 未来函数 / repaint / lookahead / 确认.
Write notes to: $N/w5-chan-backtest-repos.md
TEMPLATE:
# Chan backtest repos
## Search log
## Table: repo | stars | last push | market | timeframe | buy points | results shown? | mentions lookahead?
## Notes on the top 5 (max 6 lines each, cite README lines)
EOF

cat > w6.txt <<EOF
$C

TASK w6: read the moomoo / Futu OpenAPI documentation about QUOTE limits.
Docs: https://openapi.moomoo.com/moomoo-api-doc/en/  (Futu mirror: https://openapi.futunn.com/futu-api-doc/en/)
Fetch pages with curl -sL into your scratch folder and grep them. If a page is JS-only and empty, try the other mirror, or the SDK source on GitHub: https://github.com/FutunnOpen/py-futu-api (docstrings in futu/quote/open_quote_context.py).
Answer each with a URL or file:line:
 Q1. Subscription quota: how it is set (tiers by assets/volume?), and the MINIMUM time a subscription must be held before unsubscribing.
 Q2. get_cur_kline: max bars returned; does it consume the historical K-line quota?
 Q3. request_history_kline: quota rule (distinct symbols per 30 days?), tiers, rate limit (requests per 30s).
 Q4. get_market_snapshot: max symbols per call, rate limit.
 Q5. get_stock_filter: rate limit, max results per page.
 Q6. get_rehab: any limit.
 Q7. US market data: what differs between delayed and real-time (LV1/LV2/LV3) for US stocks over OpenAPI.
Write notes to: $N/w6-moomoo-quote-limits.md
TEMPLATE: one section per question (## Q1 ... ## Q7): answer, exact quote, source. Then ## Unanswered.
EOF

cat > w7.txt <<EOF
$C

TASK w7: read the moomoo / Futu OpenAPI documentation about TRADING.
Docs: https://openapi.moomoo.com/moomoo-api-doc/en/  (Futu mirror: https://openapi.futunn.com/futu-api-doc/en/)
Fallback: https://github.com/FutunnOpen/py-futu-api (futu/trade/*.py docstrings). Fetch with curl -sL into your scratch folder and grep.
Answer each with a URL or file:line:
 Q1. Simulated trading (TrdEnv.SIMULATE) for US stocks: supported order types, how fills are simulated, known differences vs real, short selling, extended hours.
 Q2. unlock_trade: required for REAL only? how the password is supplied; how long the unlock lasts.
 Q3. place_order: rate limit (orders per 30s); required params for US limit and market orders; remark / client-order-id field usable for idempotency.
 Q4. Order and fill updates: push handlers (TradeOrderHandlerBase, TradeDealHandlerBase) and query functions (order_list_query, deal_list_query, history_*).
 Q5. modify_order / cancel: rate limits.
 Q6. Documented restrictions for API trading on REAL accounts (terms to accept, account types).
Write notes to: $N/w7-moomoo-trading.md
TEMPLATE: ## Q1 ... ## Q6: answer, exact quote, source. Then ## Unanswered.
EOF

cat > w8.txt <<EOF
$C

TASK w8: summarise local files only (no download needed).
 a) D:/Desktop/GitHub/daily_stock_analysis/.claude/reviews/rnd/02-chanlun-pro.md (a review of the repo cincaoais/chanlun-pro).
 b) Each D:/Desktop/GitHub/daily_stock_analysis/strategies/chan_*.yaml, 2-3 lines each: entry condition, exit, invalidation.
Write notes to: $N/w8-chanlun-pro-and-dsa-strategies.md
TEMPLATE:
# chanlun-pro review + DSA chan strategies
## chanlun-pro: what it is, strategy rules, signal timing / repaint findings, results claimed (cite line numbers of the review file)
## DSA strategies (one subsection per yaml, cite file:line)
## Open questions the review leaves
EOF
