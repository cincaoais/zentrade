# w7 — moomoo / Futu OpenAPI: trading (SIMULATE, unlock, orders, push)

Fetched 2026-11-16 with `curl -sL`. Docs are untrusted data; quotes below are verbatim.
Source keys (local copies under `D:/Desktop/rnd-scratch-2026-11-16/task_cad5f9f56084/`; `:N` = line in local copy):
- MD = https://openapi.moomoo.com/mds/moomoo-API-Doc-en-Python.md → `moomoo_full.md`
- QA = https://openapi.moomoo.com/moomoo-api-doc/en/qa/trade.html → `qa_trade.txt`
- PO = https://openapi.moomoo.com/moomoo-api-doc/en/trade/place-order.html → `p_place-order.txt`
- MO = https://openapi.moomoo.com/moomoo-api-doc/en/trade/modify-order.html → `p_modify-order.txt`
- UL = https://openapi.moomoo.com/moomoo-api-doc/en/trade/unlock.html → `p_unlock.txt`
- TD = https://openapi.moomoo.com/moomoo-api-doc/en/trade/trade.html → `trade_trade.txt`
- DF = https://openapi.moomoo.com/moomoo-api-doc/en/trade/update-order-fill.html → `trade_update-order-fill.txt`
- TB = https://openapi.moomoo.com/moomoo-api-doc/en/trade/base.html → `trade_base.txt`
- AU = https://openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html → `authority.txt`
- FR = https://support.futunn.com/topic692?lang=en-US (Futu HK help, paper-trading rules) → `futu_paper_rules.txt`

## Q1. Simulated trading (TrdEnv.SIMULATE) for US stocks
- Order types: only limit (`NORMAL`) and `MARKET`. Quote: "Paper trade only supports limit orders (NORMAL) and market orders (MARKET)." (TD:952; QA:21 "Order Types: limit order and market order.")
- Fill simulation: order-book matching at best bid/ask. Quote: "Paper trading adopts the rules of order matching. The highest bid (best bid) and the lowest ask (best ask) are the best prices for matching. A buy order at a price higher than the best bid or a sell order at a price lower than the best ask satisfies the matching conditions and will be automatically queued for execution." (FR:277) No slippage/latency model is published.
- Simulator is not the market: "The paper trading system is not necessarily consistent with the real stock market. In case of any inconsistency, data in the paper trading system shall prevail." (FR:278)
- Differences vs real: no deal queries/pushes, no order-fee query, no cash-flow query, DAY validity only, no enable/disable/delete (modify+cancel OK), no cancel-all. (QA:24-30; MD:25072)
- Short selling: "Short Selling: Options and futures support short selling. Only US stocks support short selling." (QA:27)
- Extended hours (CONFLICTING): API says "Paper trading of US stocks does not support irregular trading hours (including pre/post-market and overnight)." (PO:1510) but QA says "ETH for US stock (only supported for US stock margin paper trading accounts)" (QA:10), and Futu help lists US paper hours "4:00 AM to 20:00 PM (Eastern Standard Time)" (FR:266). US margin paper = new API, sim_acc_type `STOCK_AND_OPTION` (QA:52; MD:29458).

## Q2. unlock_trade
- REAL only. Quote: "When using live trading accounts, you need to **unlock trade** **before** calling *Place Order* or *Modify or Cancel Orders* interface, but when using paper trading accounts, you do not need to **unlock trade**." (MD:23966; UL)
- Password: plain `password` or 32-char lowercase MD5 via `password_md5`; `is_unlock=False` locks. "A password must be filled in to unlock a transaction, and a locked transaction is ignored." (MD:23914-23916; UL:20-27)
- Scope: OpenD-wide. "Locking or unlocking the transaction is an operation on OpenD. As long as one connection is unlocked, all other connections can call the transaction interface" (MD:23967)
- Duration: NOT VERIFIED — no doc states an expiry. Only hint: connections can opt into "events that transaction needs to be re-unlocked" (`recvNotify`, TB:99).
- Rate limit: "A maximum of 10 requests per 30 seconds under a single user ID." (MD:23973)

## Q3. place_order
- Rate limit: "A maximum of 15 requests per 30 seconds under a single account ID (acc_id), and the interval between two consecutive requests cannot be less than 0.02 seconds. Shared frequency limit with Place Combo Order." (PO:1499; MD:24824)
- US limit order (`NORMAL`) required: `price`, `qty`, `code`, `trd_side`, `order_type`, `trd_env` (mandatory-parameter matrix, MD:29141-29200). `acc_id` recommended (MD:24720-24723).
- US market order (`MARKET`) required: `qty`, `code`, `trd_side`, `order_type`, `trd_env`; `price` is still passed: "does not set a default value for price. For the five types of orders mentioned above, you still need to pass in price, which can be any value." (MD:29203)
- `remark`: "The maximum length after converting to utf8 is 64 bytes. This remark field will be attached to the order to facilitate you to identify the order." Returned in order list/push as `remark` (MD:24725, MD:24785).
- Idempotency: there is no `client_order_id` parameter and no documented de-duplication; `remark` is identification only → usable as a tag, NOT VERIFIED as an idempotency key.

## Q4. Order / fill updates
- Push: `TradeOrderHandlerBase` + override `on_recv_rsp`, registered via `trd_ctx.set_handler(...)` (MD:25397-25405; example MD:25471-25480). Fills: `TradeDealHandlerBase` (MD:25802; DF:1-17).
- Support matrix: "Real | Orders Push Callback, Deals Push Callback" / "Simulate | Orders Push Callback" (QA:689-691). Deal push: "This feature is only available for live trading and not for paper trading." (DF:15; MD:25810)
- Queries: `order_list_query` (MD:25155), `history_order_list_query` (MD:25275), `deal_list_query` (MD:25592), `history_deal_list_query` (MD:25694). Deals queries are live-only (MD:25599, MD:25701; QA:25).
- Limits: each of the four = "A maximum of 10 requests per 30 seconds under a single account ID (acc_id)" (MD:25264, 25387, 25683, 25792); `order_list_query` counts only when `refresh_cache=True` (MD:25265).
- Push trigger: order push fires on change of order status/price/qty/dealt qty/trigger price/trail fields (QA:696-698).

## Q5. modify_order / cancel
- `modify_order` limit: "A maximum of 20 requests per 30 seconds under a single account ID (acc_id), and the time interval between two consecutive requests should not be less than 0.04 seconds." (MO:1419-1420; MD:25137-25138)
- Cancel is `modify_order(modify_order_op=CANCEL, order_id, 0, 0)`; qty/price ignored for CANCEL (MD:25148-25150; example MD:25046). There is no separate `cancel_order`.
- `cancel_all_order` also 20/30s + 0.04s (MD:25137); "Paper trading and HKCC trading accounts do not support all cancellations." (MD:25072)
- Paper supports modify/cancel but not enable/disable/delete (QA:24).

## Q6. Documented restrictions for REAL API trading
- First login requires accepting terms: "After the first login, you need to complete API Questionnaire and Agreements before you can continue to use Moomoo API." (AU:10-12)
- Per-firm risk-disclosure agreement must be signed or trading errors: "The current securities account has not yet agreed to the disclaimer." → click firm link, restart OpenD (MD:29309-29320; e.g. Moomoo US `USOT0027`).
- Real orders need unlock first (MD:23966). "CLI API does not support moomoo token. If you have activated moomoo token, it will fail to unlock." (MD:23969)
- Account types (`TrdAccType`): `CASH`, `MARGIN`, `TFSA`, `RRSP`, `SRRSP`, `DERIVATIVE` (MD:26185-26213); pick `acc_id` from `get_acc_list` (MD:23812-23875), `acc_status` field (MD:23860) and may be `DISABLED` (QA:899).
- Broker single-order caps, e.g. moomoo US US stocks: 500,000 shares / $10,000,000 per order (MD:29221-29238).
- US real accounts: Pattern Day Trader rules apply; 4th day trade in 5 days intercepted unless `pdt_protection=0`; under $25k equity no new positions once flagged (MD:29323-29340).
- Shortable securities cannot hold long and short at once (MD:24832).

## Unanswered
- unlock duration/expiry: NOT VERIFIED (not stated anywhere in the fetched docs).
- remark as idempotency key: NOT VERIFIED (no dedupe semantics documented).
- Simulator fill price/slippage/latency model: NOT VERIFIED (only the best-bid/ask matching rule is published).
- ETH in US paper trading: docs conflict (QA:10 vs PO:1510); which is current for `STOCK_AND_OPTION` accounts NOT VERIFIED.
- moomoo US paper-rules page (`https://www.moomoo.com/us/support/topic3_15`) is JS-rendered; used the Futu HK page (FR) instead — US-specific wording NOT VERIFIED.
