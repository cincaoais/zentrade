// ZT.data — pure helpers shared by the timeline, candles and replay modules.
// Classic script: works from file:// with no build step.
(function () {
  'use strict';

  const ZT = (window.ZT = window.ZT || {});

  const DAY_MS = 86400000;

  // ISO-8601 week key: Monday starts the week, the week's year is the year of
  // its Thursday (so 2025-12-29 and 2026-01-02 both map to 2026-W01).
  function isoWeekKey(iso) {
    const parts = iso.split('-');
    const dt = new Date(Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2])));
    const weekday = dt.getUTCDay() || 7; // Mon=1 .. Sun=7
    dt.setUTCDate(dt.getUTCDate() + 4 - weekday); // Thursday of this week
    const year = dt.getUTCFullYear();
    const yearStart = new Date(Date.UTC(year, 0, 1));
    const week = Math.ceil(((dt - yearStart) / DAY_MS + 1) / 7);
    return year + '-W' + String(week).padStart(2, '0');
  }

  function bucketKey(iso, tf) {
    return tf === 'M' ? iso.slice(0, 7) : isoWeekKey(iso);
  }

  // Aggregate daily bars. "D" returns the input unchanged; "W" buckets by ISO
  // week (Mon-Sun), "M" by calendar month. A bucket is dated by its last bar.
  function aggregate(bars, tf) {
    if (!bars || tf === 'D') return bars;
    if (tf !== 'W' && tf !== 'M') throw new Error('ZT.data.aggregate: unknown timeframe ' + tf);

    const out = { d: [], o: [], h: [], l: [], c: [], v: [] };
    let key = null;
    let last = -1;
    for (let i = 0; i < bars.d.length; i++) {
      const k = bucketKey(bars.d[i], tf);
      if (k !== key) {
        key = k;
        out.d.push(bars.d[i]);
        out.o.push(bars.o[i]);
        out.h.push(bars.h[i]);
        out.l.push(bars.l[i]);
        out.c.push(bars.c[i]);
        out.v.push(bars.v[i]);
        last = out.d.length - 1;
      } else {
        out.d[last] = bars.d[i];
        if (bars.h[i] > out.h[last]) out.h[last] = bars.h[i];
        if (bars.l[i] < out.l[last]) out.l[last] = bars.l[i];
        out.c[last] = bars.c[i];
        out.v[last] += bars.v[i];
      }
    }
    return out;
  }

  // Point-in-time cut: only bars dated on or before iso. Returns new arrays.
  function truncate(bars, iso) {
    if (!bars) return bars;
    let n = 0;
    while (n < bars.d.length && bars.d[n] <= iso) n++;
    return {
      d: bars.d.slice(0, n),
      o: bars.o.slice(0, n),
      h: bars.h.slice(0, n),
      l: bars.l.slice(0, n),
      c: bars.c.slice(0, n),
      v: bars.v.slice(0, n),
    };
  }

  // Index of the last trading day <= iso; -1 when iso is before the period.
  function dayIndex(data, iso) {
    const days = data.days || [];
    let lo = 0;
    let hi = days.length - 1;
    let ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (days[mid] <= iso) {
        ans = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return ans;
  }

  // Trades already entered at day i, each flagged open (not exited yet).
  function tradesAsOf(data, i) {
    const day = (data.days || [])[i];
    if (day === undefined) return [];
    const out = [];
    for (const t of data.trades || []) {
      if (t.entry_date <= day) {
        out.push(Object.assign({}, t, { open: t.exit_date == null || t.exit_date > day }));
      }
    }
    return out;
  }

  // That day's ranked signal items, or [] when the day has no signals.
  function signalsOn(data, i) {
    const day = (data.days || [])[i];
    if (day === undefined) return [];
    for (const s of data.signals || []) {
      if (s.date === day) return s.items;
    }
    return [];
  }

  // Drawdown series relative to the running peak.
  function drawdown(equity) {
    const out = [];
    let peak = -Infinity;
    for (const v of equity) {
      if (v > peak) peak = v;
      out.push(peak === 0 ? 0 : v / peak - 1);
    }
    return out;
  }

  // "+12.3%" / "-4.0%" / "—".
  function fmtPct(x, digits) {
    const d = digits == null ? 1 : digits;
    if (x == null || !Number.isFinite(x)) return '—';
    const pct = x * 100;
    return (pct >= 0 ? '+' : '-') + Math.abs(pct).toFixed(d) + '%';
  }

  ZT.data = {
    aggregate: aggregate,
    truncate: truncate,
    dayIndex: dayIndex,
    tradesAsOf: tradesAsOf,
    signalsOn: signalsOn,
    drawdown: drawdown,
    fmtPct: fmtPct,
  };
})();