# w6 — moomoo / Futu OpenAPI quote limits

Sources: full English Python doc at `https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md` (downloaded 2026-11-16, HTTP 200, 30,042 lines; cited as `doc.md:<line>`), rendered site `https://openapi.moomoo.com/moomoo-api-doc/en/`, SDK clone `https://github.com/FutunnOpen/py-futu-api` (cited as `open_quote_context.py:<line>`). Futu mirror `https://openapi.futunn.com/mds/Futu-API-Doc-en-Python.md` was checked for differences.

## Q1 Subscription quota & minimum hold

Answer: Assigned automatically by tier, using total assets and/or recent trading activity (monthly filled orders, monthly volume), not only assets. Per-stock/per-data-type subscription = 1 quota; unsubscribe releases it. Minimum hold is 1 minute — unsubscribe earlier is rejected, and closing the connection does not release it early.

Quote: "Total asset < 10,000 HKD (Including registered users)." / "Total asset >= 10,000 HKD." / "Satisfy 1 of the items following: 1. Total asset > 500,000 HKD; 2. The number of monthly filled orders > 200; 3. Monthly trading volume > 2 million HKD." / "Satisfy 1 of the items following: 1. Total asset > 5 million HKD; 2. The number of monthly filled orders > 2000; 3. Monthly trading volume > 20 million HKD." The same rows give Subscription Quota 100 / 300 / 1000 / 2000 and Option Subscription Quota 20 / 60 / 200 / 400. "You can unsubscribe after subscribing after at least one minute."

Source: `https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md:536-574` (tiers), `:589-592` (1 stock/data type = 1), `:2508` and `:28020` (1-minute rule), `:28029-28031` (closing connection early does not release before 1 minute).

## Q2 get_cur_kline: max bars; historical quota?

Answer: Maximum 1000 bars per call, and only the nearest 1000 are available. It requires a prior subscription. The docs define the Historical Candlestick Quota as applying to `request_history_kline`; `get_cur_kline` is real-time and requires a subscription (which occupies subscription quota), not the historical quota. There is no explicit sentence "get_cur_kline does not consume historical quota" — that negative is inferred from the quota's stated scope (see Unanswered).

Quote: "num|int|The number of candlesticks.  (Up to 1000.)" / "This interface is to obtain real-time candlestick, which can obtain the nearest 1000 at most." / "Historical Candlestick Quota applies to the Get Historical Candlesticks interface."

Source: `https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md:3722`, `:3800`, `:595`; SDK docstring "最多1000根" at `py-futu-api/futu/quote/open_quote_context.py:1345`.

## Q3 request_history_kline: quota, tiers, rate limit

Answer: Quota is distinct symbols per rolling 7 days, NOT 30 days (30 was the old cycle, shortened on 2026-04-16). Each new symbol = 1 quota; repeats of the same symbol within 7 days, and different periods of the same symbol, do not add. Tiers are the same as Q1: 100 / 300 / 1000 / 2000 symbols. Rate limit is 60 requests per 30 seconds; for paged requests only the first page call counts toward the limit. `max_count` defaults to 1000 per page.

Quote: "Within the last 7 days, each request for a stock's historical candlesticks occupies 1 Historical Candlestick Quota. Repeated requests for the same stock's historical candlesticks within the last 7 days will not be counted repeatedly." / "A maximum of 60 requests per 30 seconds. Note: If you obtain data by page, this frequency limit rule is only applicable to the first time calling the interface, and subsequent pages request frequency is unlimited." / "Historical candlestick quota reset cycle shortened from 30 days to 7 days."

Source: `https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md:595`, `:4584-4585`, `:29998` (changelog), tier values `:542-573`.

## Q4 get_market_snapshot: symbols per call, rate limit

Answer: Up to 400 symbols per call; 60 requests per 30 seconds.

Quote: "Request up to 60 snapshots every 30 seconds" / "For each request, the maximum number of stock codes supported by the parameter *code_list* is 400."

Source: `https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md:3463-3464`; also stated at `:533`.

## Q5 get_stock_filter: rate limit, max results per page

Answer: 10 requests per 30 seconds; at most 200 results per page (default `num=200`). Docs also recommend <=250 filter conditions and <=10 identical cumulative/custom conditions.

Quote: "A maximum of 10 requests per 30 seconds" / "At most 200 filter results are returned per page".

Source: `https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md:12197-12203`; signature `num=200` at `:11900`.

## Q6 get_rehab: limits

Answer: Only an interface frequency limit is documented: 60 requests per 30 seconds. No per-call symbol limit and no subscription/historical quota is mentioned for this API.

Quote: "A maximum of 60 requests per 30 seconds"

Source: `https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md:4697-4699`.

## Q7 US market data: delayed vs real-time (LV1/LV2/LV3)

Answer: Documented US stock depth by level: LV1 = 1 price level, LV2 = 60 levels, LV3 = Nasdaq 60 + Arca 60 (Nasdaq Basic + TotalView plus NYSE ArcaBook); US options only LV1. Since 2026-04-16, US real-time stock data is free during the promotion, so LV1/LV2/LV3 do not need a purchase. The docs say quote levels differ in time delay as well as depth, but they do not give a delayed-vs-real-time table for US stocks; any specific delayed tier or delay duration is NOT VERIFIED. BMP is the no-subscription permission, so real-time pushes are unavailable under BMP.

Quote: "US stocks (including ETF)|1|60|Nasdaq 60 Quotes + Arca 60 Quotes|/" / "US real-time market data now free during the promotional period." / "BMP (subscription is not supported for this permission)" / "Different levels correspond to different time delay, order book levels, and the permission to use the interface."

Source: `https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md:28081` (level table), `:454` (LV3 = Nasdaq Basic + TotalView + ArcaBook), `:29996` (free real-time), `:20103` and `:2296-2297` (BMP), `:422` (delay differs by level).

## Unanswered

- US delayed quote duration and whether OpenAPI serves delayed US stock quotes at all: NOT VERIFIED; no page in either mirror defines it (only `is_delay` for warrant screening, `doc.md:10580`, HK/SG/MY warrants).
- Explicit statement that `get_cur_kline` does not consume Historical Candlestick Quota: NOT VERIFIED directly; inferred from the quota being scoped to `request_history_kline` only (`doc.md:595`).
- Whether `get_cur_kline(num>1000)` errors or is truncated to 1000: NOT VERIFIED; docs only state "Up to 1000".