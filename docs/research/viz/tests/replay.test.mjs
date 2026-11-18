import test from 'node:test';
import assert from 'node:assert/strict';
import { loadScripts } from './helpers.mjs';

const ctx = loadScripts(['js/data.js', 'js/replay.js']);
const ZT = ctx.ZT;

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

const DAYS = weekdays('2024-01-01', 15);
const I = 4;

// equity has a dip at index 5 so drawdown is non-trivial
const EQUITY = DAYS.map((_, i) => 1 + 0.01 * i + (i === 5 ? -0.03 : 0));
const SPY = DAYS.map((_, i) => 1 + 0.004 * i);
const SLOTS_USED = DAYS.map((_, i) => (i < 3 ? 0 : i % 3));

const TRADES = [
  // closed on days[I] exactly => not open at I
  {
    id: 0,
    symbol: 'AAA',
    slot: 0,
    signal_date: DAYS[0],
    entry_date: DAYS[1],
    exit_date: DAYS[I],
    entry_price: 100,
    exit_price: 105,
    ret: 0.05,
    r: 1,
  },
  // enters after days[I] => not visible/open
  {
    id: 1,
    symbol: 'BBB',
    slot: 1,
    signal_date: DAYS[I],
    entry_date: DAYS[I + 1],
    exit_date: null,
    entry_price: 50,
    exit_price: null,
    ret: null,
    r: null,
  },
  // open at I, closed later
  {
    id: 2,
    symbol: 'CCC',
    slot: 0,
    signal_date: DAYS[1],
    entry_date: DAYS[2],
    exit_date: DAYS[7],
    entry_price: 30,
    exit_price: null,
    ret: null,
    r: null,
  },
  // still open at period end
  {
    id: 3,
    symbol: 'DDD',
    slot: 1,
    signal_date: DAYS[0],
    entry_date: DAYS[0],
    exit_date: null,
    entry_price: 20,
    exit_price: null,
    ret: null,
    r: null,
  },
];

const BARS = {
  AAA: {
    d: DAYS.slice(),
    o: DAYS.map(() => 100),
    h: DAYS.map(() => 110),
    l: DAYS.map(() => 90),
    c: DAYS.map((_, i) => 100 + i),
    v: DAYS.map(() => 10),
  },
  BBB: {
    d: DAYS.slice(),
    o: DAYS.map(() => 50),
    h: DAYS.map(() => 55),
    l: DAYS.map(() => 45),
    c: DAYS.map((_, i) => 50 + i),
    v: DAYS.map(() => 10),
  },
  CCC: {
    d: DAYS.slice(),
    o: DAYS.map(() => 30),
    h: DAYS.map(() => 40),
    l: DAYS.map(() => 20),
    // close at DAYS[I] is 35 => ret vs entry 30 is +16.67%
    c: DAYS.map((_, i) => 31 + i),
    v: DAYS.map(() => 10),
  },
  DDD: {
    d: DAYS.slice(),
    o: DAYS.map(() => 20),
    h: DAYS.map(() => 30),
    l: DAYS.map(() => 10),
    // close at DAYS[I] is 24 => ret vs entry 20 is +20%
    c: DAYS.map((_, i) => 20 + i),
    v: DAYS.map(() => 10),
  },
};

// 20 signals on days[I], deliberately shuffled and unsorted
const ITEMS = [];
for (let k = 0; k < 20; k += 1) {
  ITEMS.push({
    symbol: `S${String(k).padStart(2, '0')}`,
    score: 20 - k,
    raw_score: (20 - k) / 2,
    taken: k < 2,
  });
}
ITEMS.reverse();

const DATA = {
  meta: {
    variant: 'TEST',
    slots: 2,
    stats: { cagr: 0.1, sharpe: 987654.321, max_drawdown: -0.2222 },
  },
  days: DAYS,
  equity: EQUITY,
  spy: SPY,
  slots_used: SLOTS_USED,
  trades: TRADES,
  signals: [
    { date: DAYS[I], items: ITEMS },
    {
      date: DAYS[I + 1],
      items: [{ symbol: 'ZZZ', score: 999, raw_score: 999, taken: false }],
    },
  ],
  bars: BARS,
};

test('_panel at mid-period day is point-in-time', () => {
  const p = ZT.replay._panel(DATA, I);

  assert.equal(p.date, DAYS[I]);
  assert.equal(p.equity, EQUITY[I]);
  assert.equal(p.spy, SPY[I]);
  assert.equal(p.dd, ZT.data.drawdown(EQUITY)[I]);
  assert.equal(p.used, SLOTS_USED[I]);

  // signals: only days[I], sorted score desc, max 15
  assert.equal(p.signals.length, 15);
  assert.deepEqual(
    p.signals.map((s) => s.symbol),
    Array.from({ length: 15 }, (_, k) => `S${String(k).padStart(2, '0')}`),
  );
  for (let k = 1; k < p.signals.length; k += 1) {
    assert.ok(p.signals[k - 1].score >= p.signals[k].score, 'sorted by score desc');
  }
  assert.ok(!p.signals.some((s) => s.symbol === 'ZZZ'), 'no future-day signal');
  assert.equal(p.signals[0].taken, true);
  assert.equal(p.signals[14].taken, false);

  // open: closed on days[I] and later-entry trades excluded
  const symbols = Array.from(p.open.map((o) => o.symbol)).sort();
  assert.deepEqual(symbols, ['CCC', 'DDD']);

  const ccc = p.open.find((o) => o.symbol === 'CCC');
  assert.ok(Math.abs(ccc.ret - (35 / 30 - 1)) < 1e-12, 'ret uses close at days[I]');
  const ddd = p.open.find((o) => o.symbol === 'DDD');
  assert.ok(Math.abs(ddd.ret - (24 / 20 - 1)) < 1e-12);
});

test('_panel never exposes meta.stats', () => {
  const p = ZT.replay._panel(DATA, I);
  assert.equal(Object.prototype.hasOwnProperty.call(p, 'stats'), false);
  assert.ok(!JSON.stringify(p).includes('987654'), 'stats values are not in the panel');
});