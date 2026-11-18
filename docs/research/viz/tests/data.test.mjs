import test from 'node:test';
import assert from 'node:assert/strict';
import { deepEqual as looseDeepEqual } from 'node:assert';
import { loadScripts } from './helpers.mjs';

const ctx = loadScripts(['js/data.js']);
const ZT = ctx.ZT;

function addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + n);
  return dt.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------- aggregate

test('aggregate("D") returns the input unchanged', () => {
  const bars = {
    d: ['2026-01-05', '2026-01-06'],
    o: [1, 2], h: [2, 3], l: [0.5, 1.5], c: [1.5, 2.5], v: [10, 20],
  };
  assert.equal(ZT.data.aggregate(bars, 'D'), bars);
});

test('truncate then aggregate("W") keeps only the partial current week', () => {
  const bars = {
    d: ['2026-01-05', '2026-01-06', '2026-01-07', '2026-01-08', '2026-01-09',
        '2026-01-12', '2026-01-13', '2026-01-14', '2026-01-15'],
    o: [10, 11, 12, 13, 14, 20, 21, 22, 23],
    h: [10.5, 11.5, 12.5, 13.5, 14.5, 20.5, 21.5, 22.5, 999],
    l: [9.5, 10.5, 11.5, 12.5, 13.5, 19.5, 20.5, 21.5, 0.1],
    c: [10, 11, 12, 13, 14, 20, 21, 22, 23],
    v: [1, 2, 3, 4, 5, 6, 7, 8, 9],
  };
  const cut = ZT.data.truncate(bars, '2026-01-14');
  const w = ZT.data.aggregate(cut, 'W');

  looseDeepEqual(w.d, ['2026-01-09', '2026-01-14']);
  // full prior week
  assert.equal(w.o[0], 10);
  assert.equal(w.h[0], 14.5);
  assert.equal(w.l[0], 9.5);
  assert.equal(w.c[0], 14);
  assert.equal(w.v[0], 15);
  // Wednesday-only partial week: Mon-Wed only, no Thursday bar
  assert.equal(w.o[1], 20);
  assert.equal(w.h[1], 22.5);
  assert.equal(w.l[1], 19.5);
  assert.equal(w.c[1], 22); // Wednesday's close
  assert.equal(w.v[1], 21);
});

test('aggregate("M") buckets across a year boundary', () => {
  const bars = {
    d: ['2025-12-30', '2025-12-31', '2026-01-02', '2026-01-05'],
    o: [10, 11, 20, 21],
    h: [10.5, 11.5, 20.5, 21.5],
    l: [9.5, 10.5, 19.5, 20.5],
    c: [11, 12, 20, 21],
    v: [1, 2, 3, 4],
  };
  const m = ZT.data.aggregate(bars, 'M');
  looseDeepEqual(m.d, ['2025-12-31', '2026-01-05']);
  looseDeepEqual(m.o, [10, 20]);
  looseDeepEqual(m.h, [11.5, 21.5]);
  looseDeepEqual(m.l, [9.5, 19.5]);
  looseDeepEqual(m.c, [12, 21]);
  looseDeepEqual(m.v, [3, 7]);
});

test('ISO weeks put Mon 2025-12-29 and Fri 2026-01-02 in one bucket', () => {
  const bars = {
    d: ['2025-12-26', '2025-12-29', '2026-01-02'],
    o: [5, 10, 20],
    h: [5.5, 10.5, 20.5],
    l: [4.5, 9.5, 19.5],
    c: [6, 15, 21],
    v: [1, 2, 3],
  };
  const w = ZT.data.aggregate(bars, 'W');
  looseDeepEqual(w.d, ['2025-12-26', '2026-01-02']);
  assert.equal(w.o[1], 10);
  assert.equal(w.h[1], 20.5);
  assert.equal(w.l[1], 9.5);
  assert.equal(w.c[1], 21);
  assert.equal(w.v[1], 5);
});

// ---------------------------------------------------------------- truncate

test('truncate keeps bars through iso and does not mutate the input', () => {
  const bars = {
    d: ['2026-01-05', '2026-01-06', '2026-01-07'],
    o: [1, 2, 3], h: [1, 2, 3], l: [1, 2, 3], c: [1, 2, 3], v: [1, 2, 3],
  };
  const cut = ZT.data.truncate(bars, '2026-01-06');
  looseDeepEqual(cut.d, ['2026-01-05', '2026-01-06']);
  assert.equal(cut.c.length, 2);
  assert.equal(bars.d.length, 3);
});

// ---------------------------------------------------------------- dayIndex

test('dayIndex is -1 before start and maps between trading days backwards', () => {
  const data = { days: ['2026-01-05', '2026-01-07', '2026-01-08'] };
  assert.equal(ZT.data.dayIndex(data, '2026-01-04'), -1);
  assert.equal(ZT.data.dayIndex(data, '2026-01-05'), 0);
  assert.equal(ZT.data.dayIndex(data, '2026-01-06'), 0); // between trading days
  assert.equal(ZT.data.dayIndex(data, '2026-01-07'), 1);
  assert.equal(ZT.data.dayIndex(data, '2026-01-09'), 2);
});

// ---------------------------------------------------------------- tradesAsOf

test('tradesAsOf filters by entry and flags open trades', () => {
  const data = {
    days: ['2026-01-05', '2026-01-06', '2026-01-07'],
    trades: [
      { id: 0, symbol: 'A', entry_date: '2026-01-05', exit_date: '2026-01-06' },
      { id: 1, symbol: 'B', entry_date: '2026-01-06', exit_date: '2026-01-07' },
      { id: 2, symbol: 'C', entry_date: '2026-01-07', exit_date: null },
      { id: 3, symbol: 'D', entry_date: '2026-01-08', exit_date: null },
    ],
  };
  const got = ZT.data.tradesAsOf(data, 1); // days[1] = 2026-01-06
  looseDeepEqual(got.map((t) => t.id), [0, 1]);
  assert.equal(got[0].open, false); // exited on 2026-01-06
  assert.equal(got[1].open, true); // exits 2026-01-07
  assert.equal(data.trades[0].open, undefined); // no mutation

  const late = ZT.data.tradesAsOf(data, 2);
  assert.equal(late.find((t) => t.id === 2).open, true); // null exit stays open
});

// ---------------------------------------------------------------- signalsOn

test('signalsOn returns that day items or an empty array', () => {
  const items = [{ symbol: 'A', score: 1, raw_score: 1, taken: true }];
  const data = {
    days: ['2026-01-05', '2026-01-06'],
    signals: [{ date: '2026-01-05', items }],
  };
  assert.equal(ZT.data.signalsOn(data, 0), items);
  looseDeepEqual(ZT.data.signalsOn(data, 1), []);
  looseDeepEqual(ZT.data.signalsOn(data, 99), []);
});

// ---------------------------------------------------------------- drawdown

test('drawdown is value / running max - 1', () => {
  looseDeepEqual(ZT.data.drawdown([100, 120, 90, 108, 81]).map((v) => Math.round(v * 1e6) / 1e6), [
    0, 0, -0.25, -0.1, -0.325,
  ]);
  looseDeepEqual(ZT.data.drawdown([]), []);
});

// ---------------------------------------------------------------- fmtPct

test('fmtPct formats signed percentages and em-dash for null', () => {
  assert.equal(ZT.data.fmtPct(null), '—');
  assert.equal(ZT.data.fmtPct(0.1234), '+12.3%');
  assert.equal(ZT.data.fmtPct(-0.04), '-4.0%');
  assert.equal(ZT.data.fmtPct(0), '+0.0%');
  assert.equal(ZT.data.fmtPct(0.123456, 2), '+12.35%');
  assert.equal(ZT.data.fmtPct(-1, 0), '-100%');
});

// ---------------------------------------------------------------- sample data

test('data/sample.js honours the data contract', () => {
  const sample = loadScripts(['js/data.js', 'data/sample.js']).ZT_VIZ;
  assert.ok(sample, 'window.ZT_VIZ must be set by data/sample.js');

  const { meta, days, equity, spy, slots_used, trades, signals, bars } = sample;
  assert.equal(meta.variant, 'SAMPLE');
  assert.equal(meta.holdout, false);
  assert.equal(meta.slots, 10);
  assert.equal(days.length, 260);
  assert.equal(equity.length, 260);
  assert.equal(spy.length, 260);
  assert.equal(slots_used.length, 260);
  assert.equal(equity[0], 1);
  assert.equal(slots_used[0], 0);
  assert.equal(days[0], '2025-11-17');

  // trading days: ascending, weekdays only
  for (let i = 0; i < days.length; i++) {
    const wd = new Date(`${days[i]}T00:00:00Z`).getUTCDay();
    assert.ok(wd >= 1 && wd <= 5, `day ${days[i]} is a weekday`);
    if (i) assert.ok(days[i - 1] < days[i], 'days ascend');
  }

  const dayIndex = new Map(days.map((d, i) => [d, i]));

  // trades: ~60, ids by entry order, legs strictly before the signal
  assert.equal(trades.length, 60);
  const slotLanes = new Map();
  for (let i = 0; i < trades.length; i++) {
    const t = trades[i];
    assert.equal(t.id, i);
    assert.ok(Number.isInteger(t.slot) && t.slot >= 0 && t.slot < 10, 'slot in range');
    assert.ok(dayIndex.has(t.signal_date), 'signal_date is a day');
    assert.ok(dayIndex.has(t.entry_date), 'entry_date is a day');
    assert.ok(t.signal_date < t.entry_date, 'entry after signal');
    assert.ok(dayIndex.get(t.signal_date) === dayIndex.get(t.entry_date) - 1,
      'entry fills on the next trading day');
    if (t.exit_date !== null) {
      assert.ok(dayIndex.has(t.exit_date), 'exit_date is a day');
      assert.ok(t.exit_date > t.entry_date, 'exit after entry');
      assert.ok(t.exit_price !== null);
      assert.equal(t.ret, Math.round((t.exit_price / t.entry_price - 1) * 1e6) / 1e6);
    } else {
      assert.equal(t.exit_price, null);
      assert.equal(t.ret, null);
    }
    assert.ok(Array.isArray(t.legs) && t.legs.length === 3, 'three leg vertices');
    for (const [iso, price] of t.legs) {
      assert.ok(iso < t.signal_date, `leg ${iso} is before signal ${t.signal_date}`);
      assert.ok(Number.isFinite(price) && price > 0);
    }
    if (i) assert.ok(trades[i - 1].entry_date <= t.entry_date, 'entry order');
    if (!slotLanes.has(t.slot)) slotLanes.set(t.slot, []);
    slotLanes.get(t.slot).push(t);
  }

  // lanes never overlap: [entry, exit)
  for (const [slot, lane] of slotLanes) {
    lane.sort((a, b) => (a.entry_date < b.entry_date ? -1 : 1));
    for (let k = 1; k < lane.length; k++) {
      const prev = lane[k - 1];
      const prevEnd = prev.exit_date || days[days.length - 1];
      assert.ok(prevEnd <= lane[k].entry_date,
        `slot ${slot}: ${prev.id} ends ${prevEnd} after ${lane[k].id} starts ${lane[k].entry_date}`);
    }
  }

  // signals: every day, 5-25 items, sorted by score, taken joins trades
  const tradeKeys = new Set(trades.map((t) => `${t.symbol}|${t.signal_date}`));
  const signalDates = new Set();
  for (const s of signals) {
    assert.ok(dayIndex.has(s.date), `signal date ${s.date} is a day`);
    assert.ok(!signalDates.has(s.date), 'one signals element per day');
    signalDates.add(s.date);
    assert.ok(s.items.length >= 5 && s.items.length <= 25, `${s.date}: 5-25 items (got ${s.items.length})`);
    for (let k = 0; k < s.items.length; k++) {
      const it = s.items[k];
      if (k) assert.ok(s.items[k - 1].score >= it.score, `${s.date}: items sorted desc`);
      assert.equal(it.taken, tradeKeys.has(`${it.symbol}|${s.date}`),
        `${s.date} ${it.symbol}: taken matches trades`);
      assert.ok(Number.isFinite(it.score) && Number.isFinite(it.raw_score));
    }
  }
  assert.equal(signalDates.size, days.length);
  for (const t of trades) {
    const s = signals.find((x) => x.date === t.signal_date);
    assert.ok(s, `signals for trade ${t.id}`);
    const it = s.items.find((x) => x.symbol === t.symbol);
    assert.ok(it && it.taken === true, `taken item for trade ${t.id}`);
  }

  // bars: every traded symbol + SPY, 180 calendar days before day 0
  const barStart = addDays(days[0], -180);
  for (const symbol of [...new Set(trades.map((t) => t.symbol)), 'SPY']) {
    const b = bars[symbol];
    assert.ok(b, `bars for ${symbol}`);
    const n = b.d.length;
    assert.ok(n > 0);
    for (const key of ['o', 'h', 'l', 'c', 'v']) assert.equal(b[key].length, n, `${symbol}.${key}`);
    assert.equal(b.d[0], barStart, `${symbol} bars start 180 days earlier`);
    assert.ok(b.d[n - 1] >= days[days.length - 1], `${symbol} bars cover the period`);
    for (let k = 1; k < n; k++) assert.ok(b.d[k - 1] < b.d[k], `${symbol} bars ascend`);
  }

  // meta stats contract
  assert.equal(meta.stats.trades, trades.length);
  for (const key of ['cagr', 'sharpe', 'max_drawdown', 'trades', 'win_rate', 'avg_r', 'spy_cagr']) {
    assert.ok(key in meta.stats, `stats.${key}`);
  }
});