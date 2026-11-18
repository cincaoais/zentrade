// Generate deterministic synthetic data for the strategy replay page.
//
//   node docs/research/viz/tools/make_sample.mjs
//
// Writes docs/research/viz/data/sample.js (window.ZT_VIZ = {...}) matching the
// data contract in docs/superpowers/plans/2026-11-18-strategy-viz.md.
// Same seed -> byte-identical output. No network, no data dir access.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(HERE, '../data/sample.js');

const SEED = 42;
const SLOTS = 10;
const N_DAYS = 260;
const FIRST_DAY = '2025-11-17';
const N_TRADES = 60;
const BAR_LOOKBACK_DAYS = 180;

const SYMBOLS = [
  'AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'AMD', 'NFLX', 'AVGO',
  'CRM', 'ORCL', 'ADBE', 'COST', 'PEP', 'JPM', 'V', 'UNH', 'XOM', 'WMT',
];

// ------------------------------------------------------------------ helpers

let state = SEED >>> 0;
function rnd() {
  // Numerical Recipes LCG; deterministic on every platform.
  state = (Math.imul(1664525, state) + 1013904223) >>> 0;
  return state / 4294967296;
}
function randInt(a, b) {
  return a + Math.floor(rnd() * (b - a + 1));
}
function pick(arr) {
  return arr[Math.floor(rnd() * arr.length)];
}
function round4(x) {
  return Math.round(x * 1e4) / 1e4;
}
function round6(x) {
  return Math.round(x * 1e6) / 1e6;
}

const pad = (n) => String(n).padStart(2, '0');
function iso(dt) {
  return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}`;
}
function parseIso(s) {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
function addDays(s, n) {
  const dt = parseIso(s);
  dt.setUTCDate(dt.getUTCDate() + n);
  return iso(dt);
}
function isWeekday(s) {
  const wd = parseIso(s).getUTCDay();
  return wd >= 1 && wd <= 5;
}
function nextWeekday(s) {
  let x = s;
  while (!isWeekday(x)) x = addDays(x, 1);
  return x;
}
function weekdays(from, to) {
  const out = [];
  let x = nextWeekday(from);
  while (x <= to) {
    out.push(x);
    x = nextWeekday(addDays(x, 1));
  }
  return out;
}

// ------------------------------------------------------------------ calendar

const days = [];
{
  let x = FIRST_DAY;
  while (days.length < N_DAYS) {
    if (isWeekday(x)) days.push(x);
    x = addDays(x, 1);
  }
}
const barStart = addDays(days[0], -BAR_LOOKBACK_DAYS);
const barDates = weekdays(barStart, days[days.length - 1]);

// ------------------------------------------------------------------ bars

function makeBars(dates, startPrice, vol) {
  const o = [];
  const h = [];
  const l = [];
  const c = [];
  const v = [];
  let prev = startPrice;
  for (let i = 0; i < dates.length; i++) {
    const op = Math.max(1, prev * (1 + (rnd() - 0.5) * 0.012));
    const ret = (rnd() - 0.5) * 2 * vol + 0.00025;
    const cl = Math.max(1, op * (1 + ret));
    const hi = Math.max(op, cl) * (1 + rnd() * vol * 0.7);
    const lo = Math.min(op, cl) * (1 - rnd() * vol * 0.7);
    o.push(round4(op));
    h.push(round4(hi));
    l.push(round4(Math.max(0.01, lo)));
    c.push(round4(cl));
    v.push(Math.round((0.4 + rnd() * 2.4) * 1e6));
    prev = cl;
  }
  return { d: dates.slice(), o, h, l, c, v };
}

const bars = {};
const barAt = {};
for (const symbol of SYMBOLS) {
  const startPrice = 25 + rnd() * 320;
  const vol = 0.012 + rnd() * 0.022;
  bars[symbol] = makeBars(barDates, startPrice, vol);
  barAt[symbol] = new Map(bars[symbol].d.map((d, i) => [d, i]));
}
bars.SPY = makeBars(barDates, 420, 0.007);
barAt.SPY = new Map(bars.SPY.d.map((d, i) => [d, i]));

// ------------------------------------------------------------------ trades

const laneNextFree = new Array(SLOTS).fill(0);
const rawTrades = [];
let cursor = 5;
for (let k = 0; k < N_TRADES; k++) {
  // Leave at least 2 trading days between this entry and the next one.
  const maxHere = 235 - 2 * (N_TRADES - 1 - k);
  if (cursor > maxHere) cursor = maxHere;
  if (k > 0 && cursor <= rawTrades[k - 1].entryIdx) cursor = rawTrades[k - 1].entryIdx + 1;

  let entryIdx = cursor;
  let slot = -1;
  for (let s = 0; s < SLOTS; s++) {
    if (laneNextFree[s] <= entryIdx) {
      slot = s;
      break;
    }
  }
  if (slot < 0) {
    // All lanes busy: delay this entry until the earliest lane frees up.
    entryIdx = Math.min(...laneNextFree);
    for (let s = 0; s < SLOTS; s++) {
      if (laneNextFree[s] <= entryIdx) {
        slot = s;
        break;
      }
    }
  }

  const symbol = pick(SYMBOLS);
  const hold = randInt(3, 30);
  const exitIdx = Math.min(entryIdx + hold, days.length - 1);
  const signalIdx = entryIdx - 1;
  laneNextFree[slot] = exitIdx;

  rawTrades.push({ entryIdx, signalIdx, exitIdx, slot, symbol, hold: exitIdx - entryIdx });
  cursor = entryIdx + randInt(2, 5);
}

// Sanity: ascending entries, valid exits, lanes never overlap.
for (let i = 0; i < rawTrades.length; i++) {
  const t = rawTrades[i];
  if (t.exitIdx <= t.entryIdx) throw new Error(`bad exit for trade ${i}`);
  if (i && t.entryIdx <= rawTrades[i - 1].entryIdx) throw new Error(`entry order broke at ${i}`);
}
{
  const lanes = new Map();
  for (const t of rawTrades) {
    if (!lanes.has(t.slot)) lanes.set(t.slot, []);
    lanes.get(t.slot).push(t);
  }
  for (const [slot, lane] of lanes) {
    lane.sort((a, b) => a.entryIdx - b.entryIdx);
    for (let i = 1; i < lane.length; i++) {
      if (lane[i - 1].exitIdx > lane[i].entryIdx) {
        throw new Error(`slot ${slot} overlaps at trade ${i}`);
      }
    }
  }
}

const trades = rawTrades.map((t, id) => {
  const symbol = t.symbol;
  const b = bars[symbol];
  const map = barAt[symbol];
  const signalDate = days[t.signalIdx];
  const entryDate = days[t.entryIdx];
  const exitDate = days[t.exitIdx];

  const entryPrice = b.o[map.get(entryDate)];
  const exitPrice = b.c[map.get(exitDate)];
  const ret = round6(exitPrice / entryPrice - 1);

  const riskPct = 0.04 + rnd() * 0.04;
  const invalidation = round4(entryPrice * (1 - riskPct));
  const r = round6(ret / riskPct);

  // Three Chan leg vertices, all strictly before the signal date.
  const sIdx = map.get(signalDate);
  const lastEndIdx = sIdx - randInt(1, 3);
  const prevEndIdx = lastEndIdx - randInt(2, 6);
  const prevStartIdx = prevEndIdx - randInt(5, 15);
  const legs = [
    [b.d[prevStartIdx], b.l[prevStartIdx]],
    [b.d[prevEndIdx], b.h[prevEndIdx]],
    [b.d[lastEndIdx], b.l[lastEndIdx]],
  ];

  return {
    id,
    symbol,
    slot: t.slot,
    signal_date: signalDate,
    entry_date: entryDate,
    exit_date: exitDate,
    entry_price: entryPrice,
    exit_price: exitPrice,
    ret,
    r,
    exit_reason: ret >= 0 ? 'target' : 'invalidation',
    hold_days: t.hold,
    score: round6(rnd()),
    raw_score: round6(rnd() * 100),
    invalidation,
    legs,
    _entryIdx: t.entryIdx,
    _exitIdx: t.exitIdx,
  };
});

// ------------------------------------------------------------------ signals

const tradeBySignal = new Map();
for (const t of trades) {
  const key = `${t.symbol}|${t.signal_date}`;
  if (!tradeBySignal.has(key)) tradeBySignal.set(key, t);
}

const signals = [];
for (let i = 0; i < days.length; i++) {
  const date = days[i];
  const items = new Map();

  const n = randInt(5, 20);
  for (let k = 0; k < n; k++) {
    const symbol = pick(SYMBOLS);
    const score = round6(rnd());
    items.set(symbol, {
      symbol,
      score,
      raw_score: round6(score * 100 + rnd() * 10),
      taken: false,
    });
  }

  // Contract: 5-25 items, so top up if duplicate picks left us short.
  let guard = 0;
  while (items.size < 5 && guard++ < 100) {
    const symbol = pick(SYMBOLS);
    if (!items.has(symbol)) {
      const score = round6(rnd());
      items.set(symbol, {
        symbol,
        score,
        raw_score: round6(score * 100 + rnd() * 10),
        taken: false,
      });
    }
  }

  // Every trade's entry row must be present and taken.
  for (const t of trades) {
    if (t.signal_date !== date) continue;
    items.set(t.symbol, {
      symbol: t.symbol,
      score: round6(0.92 + rnd() * 0.079),
      raw_score: round6(rnd() * 100),
      taken: true,
    });
  }

  // Contract says taken === a trade exists for (symbol, date).
  for (const it of items.values()) {
    it.taken = tradeBySignal.has(`${it.symbol}|${date}`);
  }

  let list = [...items.values()].sort((a, b) => b.score - a.score);
  if (list.length > 25) {
    const taken = list.filter((x) => x.taken);
    const rest = list.filter((x) => !x.taken).slice(0, Math.max(0, 25 - taken.length));
    list = [...taken, ...rest].sort((a, b) => b.score - a.score);
  }
  if (list.length < 5) throw new Error(`too few signals on ${date}`);
  signals.push({ date, items: list });
}

// ------------------------------------------------------------------ series

const equity = [1];
for (let i = 1; i < days.length; i++) {
  let contrib = 0;
  for (const t of trades) {
    if (t._entryIdx <= i && i <= t._exitIdx) {
      const b = bars[t.symbol];
      const map = barAt[t.symbol];
      const ci = map.get(days[i]);
      if (i === t._entryIdx) {
        contrib += b.c[ci] / t.entry_price - 1;
      } else {
        const pi = map.get(days[i - 1]);
        contrib += b.c[ci] / b.c[pi] - 1;
      }
    }
  }
  equity.push(equity[i - 1] * (1 + contrib / SLOTS));
}

const spy0 = bars.SPY.c[barAt.SPY.get(days[0])];
const spy = days.map((d) => round6(bars.SPY.c[barAt.SPY.get(d)] / spy0));

const slotsUsed = days.map((_, i) =>
  trades.filter((t) => t._entryIdx <= i && i < t._exitIdx).length);

// ------------------------------------------------------------------ stats

const daily = [];
for (let i = 1; i < equity.length; i++) daily.push(equity[i] / equity[i - 1] - 1);
const mean = daily.reduce((a, b) => a + b, 0) / daily.length;
const variance = daily.reduce((a, b) => a + (b - mean) ** 2, 0) / daily.length;
const sd = Math.sqrt(variance);

let peak = -Infinity;
let maxDd = 0;
for (const v of equity) {
  if (v > peak) peak = v;
  const dd = v / peak - 1;
  if (dd < maxDd) maxDd = dd;
}

const wins = trades.filter((t) => t.ret > 0).length;
const avgR = trades.reduce((a, t) => a + t.r, 0) / trades.length;
const years = (days.length - 1) / 252;
const cagr = Math.pow(equity[equity.length - 1] / equity[0], 1 / years) - 1;
const spyCagr = Math.pow(spy[spy.length - 1] / spy[0], 1 / years) - 1;

const meta = {
  variant: 'SAMPLE',
  base: 'SAMPLE',
  rank: 'sample',
  slots: SLOTS,
  period: 'sample',
  start: days[0],
  end: days[days.length - 1],
  generated_at: '2026-11-18T00:00:00Z',
  holdout: false,
  stats: {
    cagr: round6(cagr),
    sharpe: round6(sd === 0 ? 0 : (mean / sd) * Math.sqrt(252)),
    max_drawdown: round6(maxDd),
    trades: trades.length,
    win_rate: round6(wins / trades.length),
    avg_r: round6(avgR),
    spy_cagr: round6(spyCagr),
  },
};

// ------------------------------------------------------------------ output

const cleanTrades = trades.map(({ _entryIdx, _exitIdx, ...t }) => t);
const payload = {
  meta,
  days,
  equity: equity.map(round6),
  spy,
  slots_used: slotsUsed,
  trades: cleanTrades,
  signals,
  bars,
};

fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, 'window.ZT_VIZ = ' + JSON.stringify(payload) + ';\n', 'utf8');

const size = fs.statSync(OUT).size;
console.log(
  `wrote ${path.relative(process.cwd(), OUT)}: ${days.length} days ` +
  `${days[0]}..${days[days.length - 1]}, ${trades.length} trades, ` +
  `${signals.length} signal days, ${size} bytes`,
);