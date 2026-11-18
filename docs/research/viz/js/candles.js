// ZT.candles — Canvas 2D candlestick view of one symbol at replay day i.
// Classic script: works from file:// with no build step.
(function () {
  'use strict';

  const ZT = (window.ZT = window.ZT || {});

  const GREEN = '#22c55e';
  const RED = '#ef4444';
  const AMBER = '#f59e0b';
  const GRID = '#1e293b';
  const MUTED = '#94a3b8';
  const DIM = '#64748b';
  const TEXT = '#e2e8f0';
  const BG = '#0b1220';

  const TIMEFRAME_K = { D: 1, W: 5, M: 21 };

  // Last index in a sorted date array whose date is <= iso; -1 if none.
  function indexAtOrBefore(dates, iso) {
    let lo = 0;
    let hi = dates.length - 1;
    let ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (dates[mid] <= iso) {
        ans = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return ans;
  }

  function formatPrice(x, span) {
    if (x == null || !Number.isFinite(x)) return '—';
    const abs = Math.abs(x);
    if (span < 0.5 || abs < 1) return x.toFixed(4);
    if (span < 20) return x.toFixed(2);
    return x.toFixed(abs >= 1000 ? 0 : 2);
  }

  // Pure point-in-time view. Returns the visible candles plus the price range,
  // extended by the shown trade's legs/invalidation when they are dated <= iso.
  function _view(bars, iso, tf, n, trade) {
    const data = ZT.data;
    const empty = { candles: { d: [], o: [], h: [], l: [], c: [], v: [] }, lo: 0, hi: 1 };
    if (!bars || !Array.isArray(bars.d) || !bars.d.length || iso == null) return empty;

    const truncated = data.truncate(bars, iso);
    const agg = data.aggregate(truncated, tf || 'D');
    if (!agg || !Array.isArray(agg.d) || !agg.d.length) return empty;

    const k = TIMEFRAME_K[tf] || 1;
    const count = Math.max(20, Math.ceil((n || 20) / k));
    const start = Math.max(0, agg.d.length - count);
    const candles = {
      d: agg.d.slice(start),
      o: agg.o.slice(start),
      h: agg.h.slice(start),
      l: agg.l.slice(start),
      c: agg.c.slice(start),
      v: agg.v.slice(start),
    };

    let lo = Infinity;
    let hi = -Infinity;
    for (let i = 0; i < candles.d.length; i++) {
      if (candles.l[i] < lo) lo = candles.l[i];
      if (candles.h[i] > hi) hi = candles.h[i];
    }

    if (trade) {
      if (Array.isArray(trade.legs)) {
        for (const leg of trade.legs) {
          if (!leg || leg[0] == null || leg[1] == null) continue;
          if (leg[0] > iso) continue; // point-in-time: ignore future vertices
          if (leg[1] < lo) lo = leg[1];
          if (leg[1] > hi) hi = leg[1];
        }
      }
      if (trade.invalidation != null) {
        if (trade.invalidation < lo) lo = trade.invalidation;
        if (trade.invalidation > hi) hi = trade.invalidation;
      }
    }

    if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
      lo = 0;
      hi = 1;
    }
    if (hi <= lo) hi = lo + 1;
    return { candles: candles, lo: lo, hi: hi };
  }

  function triangle(ctx, x, y, dir, size, color) {
    ctx.beginPath();
    if (dir === 'up') {
      ctx.moveTo(x, y - size);
      ctx.lineTo(x - size, y + size);
      ctx.lineTo(x + size, y + size);
    } else {
      ctx.moveTo(x, y + size);
      ctx.lineTo(x - size, y - size);
      ctx.lineTo(x + size, y - size);
    }
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  }

  function create(canvas, data) {
    if (!canvas) throw new Error('ZT.candles.create: canvas is required');
    if (!data) throw new Error('ZT.candles.create: data is required');

    const state = {
      symbol: 'SPY',
      tradeId: null,
      i: 0,
      tf: 'D',
      n: 21,
      hover: -1,
    };
    if (!data.bars || !data.bars[state.symbol]) {
      const keys = data.bars ? Object.keys(data.bars) : [];
      state.symbol = keys.length ? keys[0] : null;
    }

    const ctx = canvas.getContext ? canvas.getContext('2d') : null;
    let layout = null;

    function findTrade() {
      if (state.tradeId == null || !Array.isArray(data.trades)) return null;
      for (const t of data.trades) {
        if (t.id === state.tradeId) return t;
      }
      return null;
    }

    function currentIso() {
      const days = data.days || [];
      if (!days.length) return null;
      const i = Math.max(0, Math.min(state.i, days.length - 1));
      return days[i];
    }

    function draw() {
      if (!ctx) return;
      const cssW = canvas.clientWidth || canvas.__ztW || 900;
      const cssH = canvas.clientHeight || canvas.__ztH || 480;
      canvas.__ztW = cssW;
      canvas.__ztH = cssH;
      const dpr = window.devicePixelRatio || 1;
      const pxW = Math.max(1, Math.round(cssW * dpr));
      const pxH = Math.max(1, Math.round(cssH * dpr));
      if (canvas.width !== pxW) canvas.width = pxW;
      if (canvas.height !== pxH) canvas.height = pxH;
      if (ctx.setTransform) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      ctx.clearRect(0, 0, cssW, cssH);
      ctx.fillStyle = BG;
      ctx.fillRect(0, 0, cssW, cssH);

      const iso = currentIso();
      const bars = state.symbol && data.bars ? data.bars[state.symbol] : null;
      if (!iso || !bars) {
        layout = null;
        ctx.fillStyle = MUTED;
        ctx.font = '13px ui-sans-serif, system-ui, sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText('No candle data', 16, 16);
        return;
      }

      let trade = findTrade();
      // Point-in-time: a trade whose signal has not happened yet is not shown.
      if (trade && trade.signal_date && trade.signal_date > iso) trade = null;

      const view = _view(bars, iso, state.tf, state.n, trade);
      const candles = view.candles;
      const count = candles.d.length;
      if (!count) {
        layout = null;
        ctx.fillStyle = MUTED;
        ctx.font = '13px ui-sans-serif, system-ui, sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText('No candle data', 16, 16);
        return;
      }

      const padL = 10;
      const padR = 66;
      const padT = 38;
      const padB = 26;
      const plotL = padL;
      const plotR = Math.max(padL + 1, cssW - padR);
      const plotW = plotR - plotL;
      const plotBottom = cssH - padB;
      const totalH = plotBottom - padT;
      const volH = Math.max(18, totalH * 0.15);
      const priceTop = padT;
      const priceBottom = Math.max(priceTop + 1, plotBottom - volH - 6);
      const priceH = priceBottom - priceTop;
      const step = plotW / count;
      const bodyW = Math.max(1, Math.min(14, step * 0.62));
      const lo = view.lo;
      const hi = view.hi;
      const span = hi - lo;

      const xOf = (j) => plotL + step * (j + 0.5);
      const yOf = (p) => priceBottom - ((p - lo) / span) * priceH;

      // --- price grid + right axis (6 ticks) ---
      const ticks = 6;
      ctx.lineWidth = 1;
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      for (let t = 0; t < ticks; t++) {
        const p = lo + (span * t) / (ticks - 1);
        const y = yOf(p);
        ctx.strokeStyle = GRID;
        ctx.beginPath();
        ctx.moveTo(plotL, y);
        ctx.lineTo(plotR, y);
        ctx.stroke();
        ctx.fillStyle = MUTED;
        ctx.fillText(formatPrice(p, span), plotR + 8, y);
      }

      // --- bottom date ticks ---
      const tickCount = Math.min(6, count);
      const denom = Math.max(1, tickCount - 1);
      ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      for (let t = 0; t < tickCount; t++) {
        const j = Math.round(((count - 1) * t) / denom);
        const x = xOf(j);
        ctx.strokeStyle = '#334155';
        ctx.beginPath();
        ctx.moveTo(x, priceBottom + 2);
        ctx.lineTo(x, priceBottom + 5);
        ctx.stroke();
        const label = String(candles.d[j]).slice(5);
        const tx = Math.min(plotR - 16, Math.max(plotL + 16, x));
        ctx.fillStyle = DIM;
        ctx.fillText(label, tx, plotBottom + 4);
      }

      // --- candles + volume (bottom 15%) ---
      let vmax = 0;
      for (let j = 0; j < count; j++) {
        const v = candles.v[j];
        if (Number.isFinite(v) && v > vmax) vmax = v;
      }
      for (let j = 0; j < count; j++) {
        const o = candles.o[j];
        const h = candles.h[j];
        const l = candles.l[j];
        const c = candles.c[j];
        const color = c >= o ? GREEN : RED;
        const x = xOf(j);

        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, yOf(h));
        ctx.lineTo(x, yOf(l));
        ctx.stroke();

        const yOpen = yOf(o);
        const yClose = yOf(c);
        const top = Math.min(yOpen, yClose);
        const bodyH = Math.max(1, Math.abs(yClose - yOpen));
        ctx.fillStyle = color;
        ctx.fillRect(x - bodyW / 2, top, bodyW, bodyH);

        const vol = candles.v[j];
        if (vmax > 0 && Number.isFinite(vol)) {
          const vh = (vol / vmax) * volH * 0.9;
          ctx.globalAlpha = 0.5;
          ctx.fillStyle = color;
          ctx.fillRect(x - bodyW / 2, plotBottom - vh, bodyW, vh);
          ctx.globalAlpha = 1;
        }
      }

      // --- trade overlay: legs, invalidation, entry/exit markers ---
      if (trade) {
        if (Array.isArray(trade.legs)) {
          const pts = [];
          for (const leg of trade.legs) {
            if (!leg || leg[0] == null || leg[1] == null || leg[0] > iso) continue;
            let j = indexAtOrBefore(candles.d, leg[0]);
            if (j < 0) j = 0;
            if (j >= count) j = count - 1;
            pts.push({ x: xOf(j), y: yOf(leg[1]) });
          }
          if (pts.length) {
            ctx.strokeStyle = AMBER;
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            for (let k = 0; k < pts.length; k++) {
              if (k === 0) ctx.moveTo(pts[k].x, pts[k].y);
              else ctx.lineTo(pts[k].x, pts[k].y);
            }
            ctx.stroke();
            ctx.fillStyle = AMBER;
            for (const p of pts) {
              ctx.beginPath();
              ctx.arc(p.x, p.y, 2.5, 0, Math.PI * 2);
              ctx.fill();
            }
          }
        }

        if (trade.invalidation != null) {
          const y = yOf(trade.invalidation);
          ctx.save();
          ctx.setLineDash([6, 4]);
          ctx.strokeStyle = RED;
          ctx.lineWidth = 1.2;
          ctx.beginPath();
          ctx.moveTo(plotL, y);
          ctx.lineTo(plotR, y);
          ctx.stroke();
          ctx.restore();
        }

        if (trade.entry_date && trade.entry_date <= iso) {
          let j = indexAtOrBefore(candles.d, trade.entry_date);
          if (j < 0) j = 0;
          if (j >= count) j = count - 1;
          const y = Math.min(plotBottom - 8, yOf(candles.l[j]) + 14);
          triangle(ctx, xOf(j), y, 'up', 5, GREEN);
        }
        if (trade.exit_date && trade.exit_date <= iso) {
          let j = indexAtOrBefore(candles.d, trade.exit_date);
          if (j < 0) j = 0;
          if (j >= count) j = count - 1;
          const y = Math.max(priceTop + 8, yOf(candles.h[j]) - 14);
          triangle(ctx, xOf(j), y, 'down', 5, RED);
        }
      }

      // --- header ---
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.font = 'bold 13px ui-sans-serif, system-ui, sans-serif';
      ctx.fillStyle = TEXT;
      let header = state.symbol + ' · ' + state.tf;
      if (trade) {
        const stats = _headerStats(trade, iso, count > 0 ? candles.c[count - 1] : null);
        const entry =
          stats.status === 'pending' || trade.entry_price == null
            ? '—'
            : formatPrice(trade.entry_price, span);
        const exit =
          stats.status === 'closed'
            ? trade.exit_price == null
              ? '—'
              : formatPrice(trade.exit_price, span)
            : stats.status;
        const ret = stats.ret == null ? '—' : ZT.data.fmtPct(stats.ret);
        const r = stats.r == null ? '—' : Number(stats.r).toFixed(2) + 'R';
        header += ' · ' + entry + ' → ' + exit + ' · ' + ret + ' · ' + r;
      }
      ctx.fillText(header, padL, 10);

      // --- crosshair on the hovered candle ---
      if (state.hover >= 0 && state.hover < count) {
        const j = state.hover;
        const x = xOf(j);
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = '#64748b';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, priceTop);
        ctx.lineTo(x, plotBottom);
        ctx.stroke();
        ctx.restore();

        const o = candles.o[j];
        const h = candles.h[j];
        const l = candles.l[j];
        const c = candles.c[j];
        const label =
          'O ' +
          formatPrice(o, span) +
          '  H ' +
          formatPrice(h, span) +
          '  L ' +
          formatPrice(l, span) +
          '  C ' +
          formatPrice(c, span) +
          '  ' +
          String(candles.d[j]);
        ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
        const tw = ctx.measureText(label).width;
        const bx = Math.min(plotR - tw - 12, Math.max(plotL + 4, x - tw / 2 - 6));
        ctx.fillStyle = 'rgba(15, 23, 42, 0.92)';
        ctx.fillRect(bx, priceTop + 4, tw + 12, 20);
        ctx.strokeStyle = '#334155';
        ctx.strokeRect(bx, priceTop + 4, tw + 12, 20);
        ctx.fillStyle = TEXT;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(label, bx + 6, priceTop + 14);
      }

      layout = { x0: plotL, step: step, count: count };
    }

    function onMouseMove(ev) {
      if (!layout) return;
      const rect = canvas.getBoundingClientRect
        ? canvas.getBoundingClientRect()
        : { left: 0 };
      const x = ev.clientX - rect.left;
      const j = Math.floor((x - layout.x0) / layout.step);
      const next = j >= 0 && j < layout.count ? j : -1;
      if (next !== state.hover) {
        state.hover = next;
        draw();
      }
    }

    function onMouseLeave() {
      if (state.hover !== -1) {
        state.hover = -1;
        draw();
      }
    }

    if (canvas.addEventListener) {
      canvas.addEventListener('mousemove', onMouseMove);
      canvas.addEventListener('mouseleave', onMouseLeave);
    }

    function show(symbol, tradeId) {
      if (symbol) state.symbol = symbol;
      state.tradeId = tradeId == null ? null : tradeId;
      state.hover = -1;
      draw();
    }

    function setDay(i) {
      const days = data.days || [];
      const next = Number.isFinite(i) ? Math.round(i) : 0;
      state.i = Math.max(0, Math.min(days.length ? days.length - 1 : 0, next));
      state.hover = -1;
      draw();
    }

    function setTimeframe(tf) {
      if (!TIMEFRAME_K[tf]) return;
      state.tf = tf;
      draw();
    }

    function setWindow(n) {
      const next = Number(n);
      if (Number.isFinite(next) && next > 0) state.n = next;
      draw();
    }

    function resize() {
      draw();
    }

    return {
      show: show,
      setDay: setDay,
      setTimeframe: setTimeframe,
      setWindow: setWindow,
      resize: resize,
      draw: draw,
    };
  }

  // Header numbers as of `iso` (point-in-time). Before the fill nothing is known; while
  // open, ret and R come from `close` (the last close <= iso); only a closed trade shows
  // its stored final ret / r. R = (price - entry) / (entry - invalidation).
  function _headerStats(trade, iso, close) {
    if (!trade || !trade.entry_date || trade.entry_date > iso) {
      return { status: 'pending', ret: null, r: null };
    }
    if (trade.exit_date && trade.exit_date <= iso) {
      return { status: 'closed', ret: trade.ret, r: trade.r };
    }
    const entry = trade.entry_price;
    if (entry == null || close == null || !(entry > 0)) {
      return { status: 'open', ret: null, r: null };
    }
    const risk = trade.invalidation == null ? null : entry - trade.invalidation;
    return {
      status: 'open',
      ret: close / entry - 1,
      r: risk != null && risk > 0 ? (close - entry) / risk : null,
    };
  }

  ZT.candles = { create: create, _view: _view, _headerStats: _headerStats };
})();