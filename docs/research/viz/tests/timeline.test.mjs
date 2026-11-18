import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

// Load the classic script into a throw-away browser-ish context.
function loadTimeline() {
  const ctx = vm.createContext({ console });
  ctx.window = ctx;
  const code = fs.readFileSync(path.join(here, "..", "js", "timeline.js"), "utf8");
  vm.runInContext(code, ctx, { filename: "timeline.js" });
  return ctx.window.ZT.timeline;
}

const DAYS = [
  "2024-01-02", // 0
  "2024-01-03", // 1
  "2024-01-04", // 2
  "2024-01-05", // 3
  "2024-01-08", // 4
  "2024-01-09", // 5
];

function makeData() {
  return {
    meta: { variant: "H1-2B~retr", slots: 2 },
    days: DAYS.slice(),
    equity: [1, 1.01, 1.02, 1.01, 1.03, 1.04],
    spy: [1, 1.002, 1.003, 1.001, 1.004, 1.005],
    slots_used: [1, 1, 1, 1, 1, 2],
    trades: [
      // open at day 2, exits later; exit_price is deliberately far away.
      { id: 0, symbol: "AAA", slot: 0, entry_date: "2024-01-02", exit_date: "2024-01-09",
        entry_price: 10, exit_price: 20, ret: 1 },
      // closed exactly on day 2.
      { id: 1, symbol: "BBB", slot: 1, entry_date: "2024-01-03", exit_date: "2024-01-04",
        entry_price: 10, exit_price: 9, ret: -0.1 },
      // enters later, must not show up at day 2.
      { id: 2, symbol: "CCC", slot: 0, entry_date: "2024-01-08", exit_date: null,
        entry_price: 10, exit_price: null, ret: null },
    ],
    signals: [],
    bars: {
      AAA: { d: DAYS.slice(), o: [], h: [], l: [], c: [10, 11, 12, 12.5, 12.8, 13], v: [] },
      BBB: { d: DAYS.slice(), o: [], h: [], l: [], c: [10, 10, 9, 9.1, 9.2, 9.3], v: [] },
      CCC: { d: DAYS.slice(), o: [], h: [], l: [], c: [10, 10, 10, 10, 13, 14], v: [] },
    },
  };
}

test("_boxes at a mid-period i: points in time, closed/open heights, colors, slots", () => {
  const tl = loadTimeline();
  const data = makeData();
  const i = 2; // 2024-01-04

  const boxes = tl._boxes(data, i);
  assert.equal(boxes.length, 2, "only trades entered on or before day 2: " + JSON.stringify(boxes));

  const open = boxes.find((b) => b.id === 0);
  const closed = boxes.find((b) => b.id === 1);
  assert.ok(open && closed);

  // Entry index and slot lane.
  assert.equal(open.x0, 0);
  assert.equal(open.z, 0);
  assert.equal(closed.x0, 1);
  assert.equal(closed.z, 1);

  // The trade that exits later is still open and clipped at i.
  assert.equal(open.open, true);
  assert.equal(open.x1, i);

  // The trade that exited today is closed at its exit index.
  assert.equal(closed.open, false);
  assert.equal(closed.x1, 2);

  // Closed height comes from exit_price vs entry_price: 0.2 + 4 * |ret|.
  assert.ok(Math.abs(closed.h - (0.2 + 4 * Math.abs(9 / 10 - 1))) < 1e-9);
  assert.equal(closed.color, "#ef4444");

  // Open height comes from the close at days[i] vs entry_price: 12 / 10 - 1.
  assert.ok(Math.abs(open.h - (0.2 + 4 * Math.abs(12 / 10 - 1))) < 1e-9);
  assert.equal(open.color, "#22c55e");
});

test("_boxes: a trade exiting after i is open and never leaks its exit price", () => {
  const tl = loadTimeline();
  const data = makeData();
  const i = 2;

  const box = tl._boxes(data, i).find((b) => b.id === 0);
  assert.equal(box.open, true);

  const pointInTime = 0.2 + 4 * Math.abs(12 / 10 - 1); // close on 2024-01-04
  const leaked = 0.2 + 4 * Math.abs(20 / 10 - 1); // exit_price 20, in the future
  assert.ok(Math.abs(box.h - pointInTime) < 1e-9, "h should use days[i] close, got " + box.h);
  assert.ok(Math.abs(box.h - leaked) > 1e-6, "h must not use exit_price");
});

test("_boxes returns [] before the period and before the first entry", () => {
  const tl = loadTimeline();
  const data = makeData();

  // Arrays come from another vm realm, so check contents instead of deep-equal.
  const before = tl._boxes(data, -1);
  assert.ok(Array.isArray(before));
  assert.equal(before.length, 0);

  // No trade has entered yet at day 0 of this reduced data set.
  const later = { ...data, trades: [data.trades[2]] };
  const none = tl._boxes(later, 0);
  assert.ok(Array.isArray(none));
  assert.equal(none.length, 0);
});