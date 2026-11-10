#!/usr/bin/env node
/**
 * A table gets one scroll container, and `components/ui/table.tsx` already owns it.
 *
 * Wrapping `<Table>` in a second scrolling element splits the axes: the outer box (height
 * bounded by the call site) scrolls vertically, while the inner `w-full` box is as tall as the
 * table, so its horizontal scrollbar sits below the last row. Put height, border and radius on
 * `containerClassName` instead. `overflow-hidden` is allowed: a clipping parent is not a
 * scroll container, and the `w-full` inner container never exceeds it.
 *
 * A hard ban rather than a ratchet: it starts at zero and the fix is mechanical.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// No exempt list: `overflow-hidden` wrappers already pass, so an entry would never fire.

// Only classes that establish a scroll container. `overflow-hidden`, `overflow-clip` and
// `overflow-visible` do not scroll and are allowed.
const SCROLLS = /\boverflow(-[xy])?-(auto|scroll)\b/;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (full.endsWith('.tsx')) yield full;
  }
}

const lineOf = (code, index) => code.slice(0, index).split('\n').length;

// The element immediately enclosing a `<Table`, found by walking back over whitespace to the
// previous opening tag. A wrapper that scrolls is only a problem when the table is its child,
// which in every shape this codebase writes means the tag directly above it.
const WRAPPER = /<(\w+)\b([^>]*)>\s*$/;

const nested = [];
const selfScrolling = [];

for (const file of walk(src)) {
  const rel = relative(src, file).split('\\').join('/');
  const code = readFileSync(file, 'utf8');

  const open = /<Table\b/g;
  let m;
  while ((m = open.exec(code)) !== null) {
    const before = code.slice(0, m.index);
    const w = before.match(WRAPPER);
    if (w && SCROLLS.test(w[2])) {
      nested.push(`${rel}:${lineOf(code, m.index)} inside <${w[1]}> that scrolls`);
    }
  }

  // The same defect written the other way round: a call site putting an overflow class on the
  // table itself, which does nothing, or on containerClassName, which fights the primitive.
  const container = /containerClassName="([^"]*)"/g;
  while ((m = container.exec(code)) !== null) {
    if (SCROLLS.test(m[1])) {
      selfScrolling.push(`${rel}:${lineOf(code, m.index)} containerClassName sets its own overflow`);
    }
  }
}

const problems = [...nested, ...selfScrolling];

console.log(
  `table scroll: ${nested.length} nested scroll container   ` +
    `${selfScrolling.length} redundant overflow on containerClassName`,
);

if (problems.length > 0) {
  console.error('\ntable scroll: a table gets exactly one scroll container.\n');
  for (const p of problems) console.error(`  ${p}`);
  console.error(
    '\nMove height, border and radius to <Table containerClassName="..."> and delete the wrapper.\n',
  );
  process.exit(1);
}

console.log('table scroll: one container per table');
