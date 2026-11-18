// ZT.main — build the page and wire the timeline, candles and replay modules.
// Classic script; called by the inline module in index.html once ZT_VIZ is set.
(function () {
  'use strict';

  const START_WINDOW = 63; // must match replay.js state.zoom (3M)

  // Most recent trade with entry_date <= days[i] (point-in-time), or null.
  function latestEntry(data, i) {
    const day = data.days && data.days[i];
    let best = null;
    for (const t of data.trades || []) {
      if (day && t.entry_date && t.entry_date <= day) {
        if (!best || t.entry_date >= best.entry_date) best = t;
      }
    }
    return best;
  }

  const ZT = (window.ZT = window.ZT || {});

  function showError(message) {
    if (window.console && window.console.error) window.console.error(message);
    const box = document.getElementById('error');
    if (!box) return;
    box.hidden = false;
    box.textContent = box.textContent ? box.textContent + '\n' + message : message;
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function setBadge(meta) {
    const badge = document.getElementById('badge');
    if (!badge) return;
    if (meta.holdout) {
      badge.hidden = false;
      badge.className = 'badge holdout';
      badge.textContent = 'HOLDOUT';
    } else if (meta.variant === 'SAMPLE' || meta.sample) {
      badge.hidden = false;
      badge.className = 'badge sample';
      badge.textContent = 'SAMPLE DATA';
    } else {
      badge.hidden = true;
      badge.textContent = '';
    }
  }

  // Create one module, showing an error box instead of throwing when it is
  // absent (another file may not be written yet) or fails to initialise.
  function mount(name, root, data) {
    const mod = ZT[name];
    if (!mod || typeof mod.create !== 'function') {
      showError('js/' + name + '.js is missing or invalid (ZT.' + name + '.create).');
      return null;
    }
    if (name === 'timeline' && !window.THREE) {
      showError('three.js is not loaded — timeline disabled.');
      return null;
    }
    try {
      return mod.create(root, data);
    } catch (err) {
      showError('ZT.' + name + '.create() failed: ' + (err && err.stack ? err.stack : err));
      return null;
    }
  }

  function call(mod, method) {
    if (mod && typeof mod[method] === 'function') {
      mod[method].apply(mod, Array.prototype.slice.call(arguments, 2));
    }
  }

  ZT.main = function main() {
    const data = window.ZT_VIZ;
    if (!data) {
      showError('ZT.main: window.ZT_VIZ is not set.');
      return null;
    }
    const meta = data.meta || {};

    // ---------------------------------------------------------- top bar
    setText('variant', meta.variant || 'unknown variant');
    setText('period', meta.period || '');
    const start = meta.start || (data.days && data.days[0]) || '?';
    const end = meta.end || (data.days && data.days[data.days.length - 1]) || '?';
    setText('range', start + ' → ' + end);
    setBadge(meta);

    // ---------------------------------------------------------- modules
    const timelineRoot = document.getElementById('timeline');
    const canvas = document.getElementById('candles');
    const replayRoot = document.getElementById('replay');

    const timeline = mount('timeline', timelineRoot, data);
    const candles = mount('candles', canvas, data);
    const replay = mount('replay', replayRoot, data);

    // ---------------------------------------------------------- wiring
    let manualPick = false;
    let shownId = null;
    if (replay && typeof replay.onDay === 'function') {
      replay.onDay(function (i) {
        call(timeline, 'setDay', i);
        // Until the user clicks a trade, follow the latest entry as of day i.
        if (!manualPick) {
          const latest = latestEntry(data, i);
          if (latest && latest.id !== shownId) {
            shownId = latest.id;
            call(candles, 'show', latest.symbol, latest.id);
          }
        }
        call(candles, 'setDay', i);
      });
    }
    if (timeline && typeof timeline.onSelect === 'function') {
      timeline.onSelect(function (trade) {
        if (trade) {
          manualPick = true;
          shownId = trade.id;
          call(candles, 'show', trade.symbol, trade.id);
        }
      });
    }
    if (replay && typeof replay.onTimeframe === 'function') {
      replay.onTimeframe(function (tf) {
        call(candles, 'setTimeframe', tf);
      });
    }
    if (replay && typeof replay.onZoom === 'function') {
      replay.onZoom(function (n) {
        call(timeline, 'setWindow', n);
        call(candles, 'setWindow', n);
      });
    }

    // ---------------------------------------------------------- initial state
    // Day 0, paused; show the first trade's symbol (or SPY when there are none).
    const firstTrade = data.trades && data.trades[0];
    const symbol = firstTrade && firstTrade.symbol ? firstTrade.symbol : 'SPY';

    call(timeline, 'setWindow', START_WINDOW);
    call(candles, 'setWindow', START_WINDOW);
    call(replay, 'setDay', 0);
    call(replay, 'pause');
    call(timeline, 'setDay', 0);
    call(candles, 'setDay', 0);
    call(candles, 'show', symbol, null);

    // ---------------------------------------------------------- resize
    window.addEventListener('resize', function () {
      call(timeline, 'resize');
      call(candles, 'resize');
      call(replay, 'resize');
    });

    return { timeline: timeline, candles: candles, replay: replay };
  };
})();