#!/usr/bin/env node
// A page header stacks on a phone.
//
// A title and actions held on one row at every width push the primary action past the right edge
// of a 390px phone, where the layout clips it and no scroll reaches it. The document itself does
// not overflow, so a page-width check cannot see it.
//
// Run: node scripts/checks/check-page-header-stacks.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return walk(path);
    return path.endsWith('.tsx') ? [path] : [];
  });
}

const ROW_AT_EVERY_WIDTH = /className="[^"]*\bflex\b[^"]*\bjustify-between\b[^"]*"/;
const STACKS = /\b(flex-col|flex-wrap)\b/;
const failures = [];
let headers = 0;

for (const file of walk(src)) {
  const lines = readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, index) => {
    if (!line.includes('<h1')) return;
    headers += 1;
    for (let at = index - 1; at >= Math.max(0, index - 4); at -= 1) {
      if (ROW_AT_EVERY_WIDTH.test(lines[at]) && !STACKS.test(lines[at])) {
        failures.push(`${relative(root, file)}:${at + 1}: this header holds its actions on one row at every width`);
        return;
      }
    }
  });
}

if (headers === 0) {
  console.error('page header: found no <h1>, so this guard is reading the wrong tree');
  process.exit(1);
}
if (failures.length > 0) {
  console.error(`page header: ${failures.length} header(s) cannot stack on a phone.`);
  for (const failure of failures) console.error(`  ${failure}`);
  console.error('Use flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between.');
  process.exit(1);
}
console.log(`page header: ${headers} headers checked, each stacks on a phone`);
