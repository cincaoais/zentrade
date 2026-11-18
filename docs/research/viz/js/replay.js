// ZT.replay — day-by-day replay controls and point-in-time side panel.
// Classic script: works from file:// with no build step.
(function () {
  'use strict';

  const ZT = (window.ZT = window.ZT || {});

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

  function closeAt(bars, iso) {
    if (!bars || !Array.isArray(bars.d) || !Array.isArray(bars.c)) return null;
    const j = indexAtOrBefore(bars.d, iso);
    if (j < 0) return null;
    const c = bars.c[j];
    return Number.isFinite(c) ? c : null;
  }

  // Pure point-in-time panel for day i. Never exposes meta.stats: the footer
  // only shows the full-period result once the replay actually reaches the end.
  function _panel(data, i) {
    const D = ZT.data;
    const days = data.days || [];
    const idx = Math.max(0, Math.min(days.length ? days.length - 1 : 0, Math.round(i) || 0));
    const date = days.length ? days[idx] : null;

    const equitySeries = data.equity || [];
    const spySeries = data.spy || [];
    const equity = equitySeries[idx] == null ? null : equitySeries[idx];
    const spy = spySeries[idx] == null ? null : spySeries[idx];
    const ddSeries = D.drawdown(equitySeries);
    const dd = ddSeries[idx] == null ? null : ddSeries[idx];
    const used = data.slots_used && data.slots_used[idx] != null ? data.slots_used[idx] : 0;

    const items = date == null ? [] : D.signalsOn(data, idx) || [];
    const signals = items
      .slice()
      .sort((a, b) => (Number(b.score) || 0) - (Number(a.score) || 0))
      .slice(0, 15)
      .map((s) => ({
        symbol: s.symbol,
        score: s.score,
        taken: !!s.taken,
      }));

    const open = [];
    if (date != null) {
      const trades = D.tradesAsOf(data, idx);
      for (const t of trades) {
        if (!t.open) continue;
        const bars = data.bars ? data.bars[t.symbol] : null;
        const close = closeAt(bars, date);
        const ret =
          close != null && Number.isFinite(t.entry_price) && t.entry_price !== 0
            ? close / t.entry_price - 1
            : null;
        open.push({ symbol: t.symbol, ret: ret });
      }
    }

    return {
      date: date,
      equity: equity,
      spy: spy,
      dd: dd,
      used: used,
      signals: signals,
      open: open,
    };
  }

  function statValue(key, value) {
    if (value == null || !Number.isFinite(value)) return '—';
    if (key === 'cagr' || key === 'max_drawdown' || key === 'win_rate' || key === 'spy_cagr') {
      return ZT.data.fmtPct(value);
    }
    if (key === 'trades') return String(Math.round(value));
    if (key === 'avg_r') return value.toFixed(2) + 'R';
    return Number(value).toFixed(2);
  }

  function create(root, data) {
    if (!root) throw new Error('ZT.replay.create: root element is required');
    if (!data) throw new Error('ZT.replay.create: data is required');
    const doc = root.ownerDocument || (typeof document !== 'undefined' ? document : null);
    if (!doc) throw new Error('ZT.replay.create: a DOM is required');

    const days = data.days || [];
    const slots = (data.meta && data.meta.slots) || 0;
    const state = { i: 0, speed: 1, playing: false, tf: 'D', zoom: 63 };
    const dayCbs = [];
    const timeframeCbs = [];
    const zoomCbs = [];

    function el(tag, cls, text) {
      const node = doc.createElement(tag);
      if (cls) node.className = cls;
      if (text != null) node.textContent = text;
      return node;
    }

    function button(label, cls) {
      const b = el('button', cls || 'replay-btn', label);
      b.type = 'button';
      return b;
    }

    function buttonGroup(labels, cls, onPick) {
      const group = el('div', cls);
      const buttons = labels.map((entry) => {
        const b = button(entry.label, 'replay-btn');
        b.addEventListener('click', () => onPick(entry.value, b));
        group.appendChild(b);
        return { value: entry.value, node: b };
      });
      return { group: group, buttons: buttons };
    }

    function markActive(buttons, value) {
      for (const entry of buttons) {
        const active = entry.value === value;
        entry.node.classList.toggle('is-active', active);
        entry.node.setAttribute('aria-pressed', active ? 'true' : 'false');
      }
    }

    // --- controls -----------------------------------------------------------
    const wrap = el('div', 'replay');
    root.appendChild(wrap);

    const controls = el('div', 'replay-controls');
    wrap.appendChild(controls);

    const playBtn = button('▶ Play', 'replay-play');
    controls.appendChild(playBtn);

    const range = el('input', 'replay-range');
    range.type = 'range';
    range.min = '0';
    range.max = String(Math.max(0, days.length - 1));
    range.step = '1';
    range.value = '0';
    range.setAttribute('aria-label', 'Replay day');
    controls.appendChild(range);

    const speeds = buttonGroup(
      [
        { label: '1x', value: 1 },
        { label: '5x', value: 5 },
        { label: '20x', value: 20 },
      ],
      'replay-speeds',
      (value) => setSpeed(value),
    );
    controls.appendChild(speeds.group);

    const timeframes = buttonGroup(
      [
        { label: 'D', value: 'D' },
        { label: 'W', value: 'W' },
        { label: 'M', value: 'M' },
      ],
      'replay-timeframes',
      (value) => {
        state.tf = value;
        markActive(timeframes.buttons, value);
        for (const cb of timeframeCbs) cb(value);
      },
    );
    controls.appendChild(timeframes.group);

    const zooms = buttonGroup(
      [
        { label: '1M', value: 21 },
        { label: '3M', value: 63 },
        { label: '6M', value: 126 },
        { label: '1Y', value: 252 },
      ],
      'replay-zooms',
      (value) => {
        state.zoom = value;
        markActive(zooms.buttons, value);
        for (const cb of zoomCbs) cb(value);
      },
    );
    controls.appendChild(zooms.group);

    // --- panels -------------------------------------------------------------
    const body = el('div', 'replay-body');
    wrap.appendChild(body);

    const summary = el('div', 'replay-summary');
    body.appendChild(summary);

    const signalsPanel = el('section', 'replay-panel replay-signals');
    signalsPanel.appendChild(el('h3', 'replay-title', 'Signals'));
    const signalsList = el('div', 'replay-list');
    signalsPanel.appendChild(signalsList);
    body.appendChild(signalsPanel);

    const openPanel = el('section', 'replay-panel replay-open');
    openPanel.appendChild(el('h3', 'replay-title', 'Open positions'));
    const openList = el('div', 'replay-list');
    openPanel.appendChild(openList);
    body.appendChild(openPanel);

    const footer = el('div', 'replay-footer');
    wrap.appendChild(footer);

    function statRow(label, value, cls) {
      const row = el('div', cls || 'replay-stat');
      row.appendChild(el('span', 'replay-stat-label', label));
      row.appendChild(el('span', 'replay-stat-value', value));
      return row;
    }

    function render() {
      const p = _panel(data, state.i);

      summary.textContent = '';
      summary.appendChild(statRow('Date', p.date == null ? '—' : p.date, 'replay-stat replay-date'));
      summary.appendChild(
        statRow(
          'Equity',
          p.equity == null
            ? '—'
            : ZT.data.fmtPct(p.equity - 1) + ' (' + p.equity.toFixed(3) + ')',
        ),
      );
      summary.appendChild(
        statRow(
          'SPY',
          p.spy == null ? '—' : ZT.data.fmtPct(p.spy - 1) + ' (' + p.spy.toFixed(3) + ')',
        ),
      );
      summary.appendChild(statRow('Drawdown', ZT.data.fmtPct(p.dd)));
      summary.appendChild(statRow('Slots', p.used + '/' + slots));

      signalsList.textContent = '';
      if (!p.signals.length) {
        signalsList.appendChild(el('div', 'replay-empty', 'No signals'));
      } else {
        for (const s of p.signals) {
          const row = el('div', 'replay-signal' + (s.taken ? ' is-taken' : ' is-skipped'));
          const score = Number.isFinite(s.score) ? Number(s.score).toFixed(2) : '—';
          row.appendChild(el('span', 'replay-symbol', s.symbol));
          row.appendChild(el('span', 'replay-score', score));
          row.appendChild(
            el(
              'span',
              'replay-status',
              s.taken ? '✓ taken' : '— not taken',
            ),
          );
          signalsList.appendChild(row);
        }
      }

      openList.textContent = '';
      if (!p.open.length) {
        openList.appendChild(el('div', 'replay-empty', 'No open positions'));
      } else {
        for (const o of p.open) {
          const row = el('div', 'replay-position');
          row.appendChild(el('span', 'replay-symbol', o.symbol));
          row.appendChild(el('span', 'replay-ret', ZT.data.fmtPct(o.ret)));
          openList.appendChild(row);
        }
      }

      // Full-period stats only once the replay has reached the last day.
      footer.textContent = '';
      if (state.i === days.length - 1 && data.meta && data.meta.stats) {
        const stats = data.meta.stats;
        footer.appendChild(el('span', 'replay-footer-label', 'full-period result'));
        for (const key of Object.keys(stats)) {
          footer.appendChild(el('span', 'replay-footer-stat', key + ': ' + statValue(key, stats[key])));
        }
      }
    }

    function setDay(i) {
      if (!days.length) return;
      const next = Math.max(0, Math.min(days.length - 1, Math.round(Number(i) || 0)));
      state.i = next;
      if (range.value !== String(next)) range.value = String(next);
      render();
      for (const cb of dayCbs) cb(next);
    }

    // --- playback -----------------------------------------------------------
    const win = doc.defaultView || (typeof window !== 'undefined' ? window : null);
    const raf = win && win.requestAnimationFrame ? win.requestAnimationFrame.bind(win) : null;
    const caf = win && win.cancelAnimationFrame ? win.cancelAnimationFrame.bind(win) : null;
    let rafId = null;
    let lastTs = null;
    let acc = 0;

    function now() {
      return win && win.performance && win.performance.now
        ? win.performance.now()
        : Date.now();
    }

    function frame(ts) {
      if (!state.playing) return;
      if (lastTs == null) lastTs = ts;
      const dt = Math.max(0, (ts - lastTs) / 1000);
      lastTs = ts;
      acc += dt * state.speed;
      if (acc >= 1) {
        const steps = Math.floor(acc);
        acc -= steps;
        setDay(state.i + steps);
      }
      if (state.i >= days.length - 1) {
        pause();
        return;
      }
      rafId = raf ? raf(frame) : setTimeout(() => frame(now()), 16);
    }

    function play() {
      if (!days.length || state.playing) return;
      if (state.i >= days.length - 1) return;
      state.playing = true;
      playBtn.textContent = '⏸ Pause';
      lastTs = null;
      acc = 0;
      rafId = raf ? raf(frame) : setTimeout(() => frame(now()), 16);
    }

    function pause() {
      state.playing = false;
      if (rafId != null) {
        if (caf) caf(rafId);
        else clearTimeout(rafId);
        rafId = null;
      }
      playBtn.textContent = '▶ Play';
    }

    function toggle() {
      if (state.playing) pause();
      else play();
    }

    function setSpeed(x) {
      const value = Number(x);
      state.speed = Number.isFinite(value) && value > 0 ? value : 1;
      markActive(speeds.buttons, state.speed);
    }

    function onDay(cb) {
      if (typeof cb === 'function') dayCbs.push(cb);
    }

    function onTimeframe(cb) {
      if (typeof cb === 'function') timeframeCbs.push(cb);
    }

    function onZoom(cb) {
      if (typeof cb === 'function') zoomCbs.push(cb);
    }

    // --- wiring -------------------------------------------------------------
    playBtn.addEventListener('click', toggle);
    range.addEventListener('input', () => setDay(Number(range.value)));

    function onKeyDown(e) {
      if (e.code === 'Space' || e.key === ' ') {
        e.preventDefault();
        toggle();
        return;
      }
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      const target = e.target;
      const tag = target && target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      e.preventDefault();
      pause();
      setDay(state.i + (e.key === 'ArrowRight' ? 1 : -1));
    }

    if (win && win.addEventListener) win.addEventListener('keydown', onKeyDown);
    else if (doc.addEventListener) doc.addEventListener('keydown', onKeyDown);

    setSpeed(state.speed);
    markActive(timeframes.buttons, state.tf);
    markActive(zooms.buttons, state.zoom);
    render();

    return {
      setDay: setDay,
      play: play,
      pause: pause,
      setSpeed: setSpeed,
      onDay: onDay,
      onTimeframe: onTimeframe,
      onZoom: onZoom,
    };
  }

  ZT.replay = { create: create, _panel: _panel };
})();