import test from 'node:test';
import assert from 'node:assert/strict';
import { loadScripts } from './helpers.mjs';

const ctx = loadScripts(['js/data.js', 'js/candles.js']);
const ZT = ctx.ZT;

// --- synthetic, deterministic daily bars (600 weekdays) ---------------------
function weekdays(startIso, count) {
  const out = [];
  const d = new Date(startIso + 'T00:00:00Z');
  while (out.length < count) {
    const wd = d.getUTCDay();
    if (wd !== 0 && wd !== 6) out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}

const DATES = weekdays('2023-01-02', 600);
const BARS = {
  d: DATES.slice(),
  o: DATES.map((_, i) => i + 1),
  h: DATES.map((_, i) => i + 1.5),
  l: DATES.map((_, i) => i + 0.5),
  c: DATES.map((_, i) => i + 1),
  v: DATES.map((_, i) => 100 + (i % 7)),
};

const weekdayOf = (iso) => new Date(iso + 'T00:00:00Z').getUTCDay();
// a Wednesday near the end so there are plenty of weeks/months available
const WI = DATES.findIndex((iso, idx) => idx > 540 && weekdayOf(iso) === 3);
const ISO = DATES[WI];

test('fixtures are sane', () => {
  assert.ok(WI > 0, 'found a Wednesday fixture date');
  assert.equal(weekdayOf(ISO), 3);
});

test('_view at a Wednesday: weekly candle is cut at iso, daily lands on iso', () => {
  // BARS after ISO contain higher closes; using any of them would change the
  // weekly candle close from (WI + 1) to a later value.
  const w = ZT.candles._view(BARS, ISO, 'W', 20);
  assert.ok(w.candles.d.length > 0);
  const wLast = w.candles.d.length - 1;
  assert.equal(w.candles.c[wLast], WI + 1);
  assert.equal(w.candles.d[wLast], ISO);
  assert.ok(w.candles.d.every((d) => d <= ISO), 'no weekly bucket after iso');

  const d = ZT.candles._view(BARS, ISO, 'D', 20);
  const dLast = d.candles.d.length - 1;
  assert.equal(d.candles.d[dLast], ISO);
  assert.equal(d.candles.c[dLast], WI + 1);
  assert.ok(d.candles.d.every((iso) => iso <= ISO), 'no daily bar after iso');
});

test('_view respects n for D/W/M', () => {
  const truncated = ZT.data.truncate(BARS, ISO);
  const availD = truncated.d.length;
  const availW = ZT.data.aggregate(truncated, 'W').d.length;
  const availM = ZT.data.aggregate(truncated, 'M').d.length;

  const cases = [
    ['D', 35, Math.min(availD, Math.max(20, 35))],
    ['D', 5, Math.min(availD, 20)],
    ['W', 250, Math.min(availW, Math.max(20, Math.ceil(250 / 5)))],
    ['W', 30, Math.min(availW, Math.max(20, Math.ceil(30 / 5)))],
    ['M', 462, Math.min(availM, Math.max(20, Math.ceil(462 / 21)))],
    ['M', 63, Math.min(availM, Math.max(20, Math.ceil(63 / 21)))],
  ];

  // when the fixture is long enough, the formula is exercised exactly
  assert.ok(availW >= 50, `need 50 weeks available, have ${availW}`);
  assert.ok(availM >= 22, `need 22 months available, have ${availM}`);

  for (const [tf, n, expected] of cases) {
    const view = ZT.candles._view(BARS, ISO, tf, n);
    assert.equal(
      view.candles.d.length,
      expected,
      `tf=${tf} n=${n} should show ${expected} candles`,
    );
    assert.equal(view.candles.o.length, expected);
    assert.equal(view.candles.h.length, expected);
    assert.equal(view.candles.l.length, expected);
    assert.equal(view.candles.c.length, expected);
  }
});

test('_view includes trade legs and invalidation in lo/hi, ignores future legs', () => {
  const plain = ZT.candles._view(BARS, ISO, 'D', 35);
  const candleMin = Math.min(...plain.candles.l);
  const candleMax = Math.max(...plain.candles.h);

  const trade = {
    signal_date: DATES[WI - 5],
    entry_date: DATES[WI - 2],
    exit_date: DATES[WI + 2],
    legs: [
      [DATES[WI - 3], 12], // below every visible candle
      [DATES[WI - 1], 5000], // above every visible candle
      [DATES[WI + 2], 99999], // after iso => must not affect lo/hi
    ],
    invalidation: 3,
  };

  assert.ok(candleMin > 12, 'leg low is outside the candle range');
  assert.ok(candleMax < 5000, 'leg high is outside the candle range');

  const withTrade = ZT.candles._view(BARS, ISO, 'D', 35, trade);
  assert.equal(withTrade.lo, 3, 'invalidation is the lowest point');
  assert.equal(withTrade.hi, 5000, 'past leg high is included');
  assert.ok(withTrade.hi < 99999, 'leg dated after iso is excluded');
});
test('_headerStats is point-in-time: open trade uses close at iso, never the final ret/r', () => {
  const trade = {
    entry_date: '2026-06-18', exit_date: '2026-07-20', signal_date: '2026-06-17',
    entry_price: 20, exit_price: 30, invalidation: 15, ret: 0.5, r: 2,
  };
  // before the fill: nothing about the trade's outcome is known
  const pending = ZT.candles._headerStats(trade, '2026-06-17', 19);
  assert.equal(pending.status, 'pending');
  assert.equal(pending.ret, null);
  assert.equal(pending.r, null);
  // open: ret and R from the close at iso, not from exit_price
  const open = ZT.candles._headerStats(trade, '2026-06-24', 22);
  assert.equal(open.status, 'open');
  assert.ok(Math.abs(open.ret - 0.1) < 1e-12);
  assert.ok(Math.abs(open.r - 0.4) < 1e-12);
  // closed: the stored final values
  const closed = ZT.candles._headerStats(trade, '2026-07-20', 99);
  assert.equal(closed.status, 'closed');
  assert.equal(closed.ret, 0.5);
  assert.equal(closed.r, 2);
  // open with no usable risk (invalidation missing or >= entry): R unknown
  const noRisk = ZT.candles._headerStats({ ...trade, invalidation: null }, '2026-06-24', 22);
  assert.equal(noRisk.r, null);
});
