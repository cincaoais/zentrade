# Strategy Replay Page (three.js) Implementation Plan

> Executed through Orca: pi workers implement one task each; the coordinator reviews and commits each task.

**Goal:** A page that opens straight from disk and replays one frozen research variant (default `H1-2B~retr`) day by day over a period: a 3D slot timeline with equity vs SPY, a candlestick view of the selected trade with daily/weekly/monthly candles, and a panel showing that day's ranked signals and which ones got a slot.

**Approved by the user (2026-11-18):** "let's do it. LGTM" for the layout in the coordinator's message (3D timeline, candles with D/W/M, replay panel, point-in-time only).

## Global Constraints

- Point-in-time: at replay day `i` the page draws nothing dated after `days[i]` (no future candles, no unclosed trade's exit, no later signals). Legs are the ones stored in the Chan point on its signal date.
- The page works from `file://` with no build step: our code is **classic scripts** (no `import`/`export`) that attach to `window.ZT`; only `index.html` contains one inline `<script type="module">` that imports three.js from a CDN via an import map and then calls `ZT.main()`.
- three.js `0.170.0` via `https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js` and `.../examples/jsm/controls/OrbitControls.js` (import map keys `three` and `three/addons/`). Exposed as `window.THREE` and `window.ZT.OrbitControls`.
- Candlestick chart uses Canvas 2D (text labels); the timeline uses three.js.
- No npm installs. JS tests: `node --test docs/research/viz/tests/` (Node 22, built-in runner), loading classic scripts with `vm.runInContext` on a fresh context where `window = globalThis`.
- Python: no OpenD, no network in tests, tests never write the real data dir.

## Files

```
backend/app/research/viz_export.py          V1  pure payload builder
backend/scripts/research/export_viz.py      V1  CLI (runs the backtest)
backend/tests/research/test_viz_export.py   V1
docs/research/viz/index.html                V2
docs/research/viz/viz.css                   V2
docs/research/viz/js/data.js                V2  ZT.data helpers
docs/research/viz/js/main.js                V2  wiring
docs/research/viz/tools/make_sample.mjs     V2  writes data/sample.js
docs/research/viz/data/sample.js            V2  synthetic, generated
docs/research/viz/tests/helpers.mjs         V2  loadScripts(paths) -> window
docs/research/viz/tests/data.test.mjs       V2
docs/research/viz/js/timeline.js            V3  ZT.timeline
docs/research/viz/tests/timeline.test.mjs   V3
docs/research/viz/js/candles.js             V4  ZT.candles
docs/research/viz/js/replay.js              V4  ZT.replay
docs/research/viz/tests/candles.test.mjs    V4
docs/research/viz/tests/replay.test.mjs     V4
```

## Data contract (`window.ZT_VIZ`)

`index.html` loads `data/current.js` with a classic script; on `onerror` it loads `data/sample.js`. Each file is `window.ZT_VIZ = {...};`.

```
meta:   {variant, base, rank, slots, period, start, end, generated_at, holdout: bool,
         stats: {cagr, sharpe, max_drawdown, trades, win_rate, avg_r}, spy_cagr}
days:   [iso date, ...]                  trading days in the period, ascending
equity: [float]                          portfolio value / first value; len == days
spy:    [float|null]                     SPY close / SPY close on days[0]; len == days
slots_used: [int]                        open positions at that day's close; len == days
trades: [{id, symbol, slot, signal_date, entry_date, exit_date, entry_price, exit_price,
          ret, r, exit_reason, hold_days, score, raw_score, invalidation, legs}]
          id = 0..n-1 by entry order; slot = 0..slots-1 (lowest free lane at entry);
          exit_date/exit_price null if still open at period end; ret = exit/entry - 1 (null if open);
          legs = [[iso, price], [iso, price], [iso, price]] or null
signals: [{date: iso, items: [{symbol, score, raw_score, taken: bool}]}]
          one element per day that has entry rows, items sorted by score desc;
          taken = a trade has (symbol, signal_date == date)
bars:   {SYMBOL: {d: [iso], o: [], h: [], l: [], c: [], v: []}}   every traded symbol and "SPY"
```

Prices rounded to 4 decimals, ratios to 6. Trade fill: signal on day t, fill at open of t+1 (`entry_fill="open_t+1"`), so `entry_date > signal_date`.

**Legs:** the Chan point for the trade is the row in `<data_dir>/research/chan/<engine>/points/<SYMBOL>.parquet` with `type` in the base's types and `<date_column> == signal_date` (`hypotheses._RETR_SPECS[base]` gives types and date column; the largest retr wins on ties, first row otherwise). Vertices: `leg_prev_start`, `leg_prev_end` (== `leg_last_start` normally), `leg_last_end`. A leg is "up" when `high[end] >= high[start]`: its start vertex is the bar low at start and its end vertex the bar high at end; a down leg uses high at start and low at end. Shared vertices take the price from the leg that ends there. Non-Chan bases or missing legs: `legs = null`.

**Bars window:** from `min(period.start - 180 calendar days, earliest leg date)` to `period.end`.

## JS module interfaces (all on `window.ZT`)

**V2 `ZT.data`** (pure, no DOM):
- `aggregate(bars, tf)`: `tf` in `"D"|"W"|"M"`; returns `{d,o,h,l,c,v}`. W buckets by ISO week (Mon–Sun), M by calendar month; bucket date = its last bar's date; o first, h max, l min, c last, v sum. `"D"` returns the input unchanged.
- `truncate(bars, iso)`: bars with `d <= iso` (point-in-time cut, applied **before** `aggregate`, so the current week's candle is partial).
- `dayIndex(data, iso)`: index of the last `days` entry `<= iso`, `-1` if before start.
- `tradesAsOf(data, i)`: trades with `entry_date <= days[i]`, each with `open: bool` (`exit_date` null or `> days[i]`).
- `signalsOn(data, i)`: items for `days[i]` or `[]`.
- `drawdown(equity)`: array, `value / running max - 1`.
- `fmtPct(x, digits=1)`: `"+12.3%"` / `"-4.0%"` / `"—"` for null.

**V2 `ZT.main()`:** reads `window.ZT_VIZ`, builds the layout's three regions, creates `timeline`, `candles` and `replay` below, and wires: replay `onDay(i)` → `timeline.setDay(i)`, `candles.setDay(i)`; timeline `onSelect(trade)` → `candles.show(trade.symbol, trade.id)`; replay `onTimeframe(tf)` → `candles.setTimeframe(tf)`; replay `onZoom(n)` → `timeline.setWindow(n)` and `candles.setWindow(n)` (`n` trading days: 21, 63, 126, 252). Starts at day 0, paused, showing the first trade's symbol (or SPY).

**V3 `ZT.timeline.create(container, data) -> {setDay(i), setWindow(n), select(id), onSelect(cb), resize(), dispose()}`**
- Scene: x = day index, z = slot lane (0..slots-1), y = height. Each trade with `entry_date <= days[i]` is a box from entry index to `min(exit index, i)`; height `0.2 + 4 * |ret_so_far|` where `ret_so_far` uses `exit_price` once closed, else the symbol's close at `days[i]` vs `entry_price`; green `#22c55e` if ≥ 0 else red `#ef4444`; open trades semi-transparent with a bright edge.
- Back wall (z = -1.5): equity line (white) and SPY line (grey) up to `i`, scaled to a fixed y range over the whole period.
- A vertical translucent plane at x = i. Camera follows so the last `n` days (setWindow) are framed. OrbitControls.
- Raycast click on a box → `onSelect(trade)`; selected box gets an outline.
- Pure helper for tests: `ZT.timeline._boxes(data, i) -> [{id, x0, x1, z, h, color, open}]`.

**V4 `ZT.candles.create(canvas, data) -> {show(symbol, tradeId|null), setDay(i), setTimeframe(tf), setWindow(n), resize()}`**
- Draws `aggregate(truncate(bars[symbol], days[i]), tf)`, last `ceil(n / k)` candles (k = 1, 5, 21 for D/W/M; min 20 candles), price axis right, date ticks bottom.
- If a trade is shown and its `signal_date <= days[i]`: legs polyline (amber), invalidation dashed line (red), entry marker ▲ at entry; exit marker ▼ only if `exit_date <= days[i]`; header text `SYMBOL · entry → exit · ret · R`.
- Pure helpers: `ZT.candles._view(bars, i_iso, tf, n) -> {candles, lo, hi}` (lo/hi include legs and invalidation when given via a 5th arg `trade`).

**V4 `ZT.replay.create(root, data) -> {setDay(i), play(), pause(), setSpeed(x), onDay(cb), onTimeframe(cb), onZoom(cb)}`**
- Controls: play/pause, range slider 0..days-1, speed 1x/5x/20x (days per second), timeframe D/W/M, zoom 1M/3M/6M/1Y.
- Panel for day i: date; equity, SPY, drawdown (`ZT.data`); slots used `k/slots`; that day's signals top 15 by score with taken ✓ and "skipped (slots full)" when not taken; open positions list with unrealised return; footer with `meta.stats` (shown as "full-period result" only once `i == days.length-1`, so it is not shown before the replay reaches it).
- Pure helper: `ZT.replay._panel(data, i) -> {date, equity, spy, dd, used, signals:[{symbol, score, taken}], open:[{symbol, ret}]}`.

## Review Focus

1. Lookahead in the page: any candle, exit marker, signal or equity point dated after `days[i]` is drawn. Each pure helper has a test at a mid-period `i`.
2. Weekly/monthly candle of the current, unfinished week/month must be built only from bars up to `days[i]`.
3. Exporter `taken` flags and trade↔signal join on `(symbol, signal_date)`; a trade with no matching entry row is an error, not silently dropped.
4. Slot lanes never overlap: two trades in one lane never have overlapping `[entry_date, exit_date)`.
5. `file://` loading: no `fetch`, no ES module files besides the inline one.
