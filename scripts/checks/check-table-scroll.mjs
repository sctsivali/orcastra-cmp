#!/usr/bin/env node
/**
 * A table gets one scroll container, and `components/ui/table.tsx` already owns it.
 *
 * Wrapping `<Table>` in a second SCROLLING element splits the two axes across two boxes, and
 * the horizontal scrollbar ends up somewhere the operator cannot see. The outer box is the one
 * a call site bounds the height of, so it takes vertical scroll. The inner box is `w-full` and
 * unbounded, so it is as tall as the whole table, and a scrollbar is painted at the bottom edge
 * of the box that owns it. That edge sits below the last row. The picker in
 * `components/instance-create-form.tsx` shipped that way: the horizontal scrollbar appeared only
 * once the list was scrolled all the way down, and at every other position the columns to the
 * right were unreachable without knowing to shift-wheel.
 *
 * The fix is a prop, not a wrapper. Height, border and radius go on `containerClassName`, which
 * lands on the element that is already scrolling.
 *
 * `overflow-hidden` is NOT banned here, and the distinction is the whole point of the rule. A
 * clipping parent is not a scroll container, and it cannot bury a scrollbar: the inner container
 * is `w-full`, so it never exceeds its parent and its scrollbar is never clipped. Several places
 * legitimately clip a bordered box's corners around a table plus its pagination. Banning those
 * too would have been a rule about a class name rather than about the defect.
 *
 * A hard ban rather than a ratchet. It starts at zero, the fix is mechanical, and a call site
 * that reintroduces it is reintroducing a shipped bug rather than inheriting one.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// note: no exempt list, deliberately. A dead file's wrappers carrying `overflow-hidden` are
// allowed here anyway, so an exemption would be an entry that never fires while implying the
// file would otherwise fail. Verified by running this check with the list emptied; it still
// reports zero.

// Only the ones that actually establish a scroll container. `overflow-hidden`, `overflow-clip`
// and `overflow-visible` do not scroll and are allowed.
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
