// Test helper: load the page's classic scripts into a fresh VM context.
//
// Usage:
//   import { loadScripts } from './helpers.mjs';
//   const ctx = loadScripts(['js/data.js', 'data/sample.js']);
//   ctx.ZT.data.fmtPct(null);
//
// Paths are relative to docs/research/viz/.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

export const vizRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export function loadScripts(relPaths) {
  const ctx = vm.createContext({ console });
  // Classic scripts expect `window`; the context's global object *is* window.
  ctx.window = ctx;
  for (const rel of relPaths) {
    const file = path.resolve(vizRoot, rel);
    const code = fs.readFileSync(file, 'utf8');
    vm.runInContext(code, ctx, { filename: file });
  }
  return ctx;
}