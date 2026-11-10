#!/usr/bin/env node
/**
 * Ratchet: an icon-only button must carry its own accessible name.
 *
 * A `<Button>` whose children are all icons renders as a bare `button` in the
 * accessibility tree unless the element itself names it. A Radix `Tooltip` contributes
 * `aria-describedby` when it opens, which describes the button without naming it, so a
 * screen reader announces "button" and nothing else.
 *
 * `title` counts as a name because the accname algorithm falls back to it, but it is the
 * weakest option: a permission-dependent title such as "No operate permission" never says
 * which action the button is. Prefer `aria-label`.
 *
 * The count may fall and never rise.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// Lower this when you fix one. A toggle whose name should change with state wants a pass with
// the keyboard rather than a bulk attribute edit.
const BASELINE = 19;

// A dead file that nothing imports goes here rather than into a baseline bump.
const EXEMPT_FILES = new Set([]);

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (full.endsWith('.tsx')) yield full;
  }
}

/** Find each `<Button ...>` and return [openingTag, innerBody]. Handles nesting. */
function buttons(code) {
  const found = [];
  let i = 0;
  while ((i = code.indexOf('<Button', i)) !== -1) {
    // Skip `<Buttons`, `<ButtonGroup` and similar.
    const after = code[i + 7];
    if (after && /[A-Za-z0-9_]/.test(after)) {
      i += 7;
      continue;
    }
    // Walk to the end of the opening tag, respecting braces so a `>` inside an arrow
    // function in a prop does not end it early.
    let depth = 0;
    let j = i;
    for (; j < code.length; j += 1) {
      const c = code[j];
      if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    const open = code.slice(i, j + 1);
    if (open.endsWith('/>')) {
      found.push([open, '']);
      i = j + 1;
      continue;
    }
    // Body runs to the matching close, counting nested Buttons.
    let k = j + 1;
    let nest = 1;
    while (k < code.length && nest > 0) {
      const nextOpen = code.indexOf('<Button', k);
      const nextClose = code.indexOf('</Button>', k);
      if (nextClose === -1) break;
      if (nextOpen !== -1 && nextOpen < nextClose) {
        nest += 1;
        k = nextOpen + 7;
      } else {
        nest -= 1;
        k = nextClose + 9;
      }
    }
    found.push([open, code.slice(j + 1, Math.max(j + 1, k - 9))]);
    i = j + 1;
  }
  return found;
}

/**
 * Is the body exactly one self-closing element and nothing else?
 *
 * Kept narrow. Broader shapes, such as dropping `{...}` expressions, report the house
 * pending-button idiom `{isPending ? <Loader2 /> : "Refresh"}` and bare JSX text as nameless,
 * and detecting rendered text from source needs a parser. One self-closing element is
 * unambiguously an icon. A button whose icon sits inside a ternary is not caught; that hole
 * is accepted to keep the check free of false positives.
 */
function isIconOnly(body) {
  return /^<[A-Z][A-Za-z0-9]*(\s[^>]*?)?\/>$/.test(body.trim());
}

const NAMING_ATTRS = ['aria-label', 'aria-labelledby', 'title'];

const offenders = [];
let scanned = 0;

for (const file of walk(src)) {
  const rel = relative(src, file).split(sep).join('/');
  if (EXEMPT_FILES.has(rel)) continue;
  const code = readFileSync(file, 'utf8');
  if (!code.includes('<Button')) continue;

  for (const [open, body] of buttons(code)) {
    scanned += 1;
    if (!isIconOnly(body)) continue;
    if (NAMING_ATTRS.some((a) => open.includes(`${a}=`))) continue;
    // An icon is the only child. Name it.
    const line = code.slice(0, code.indexOf(open)).split('\n').length;
    const icon = body.match(/<([A-Z][A-Za-z0-9]*)\s/)?.[1] || 'icon';
    offenders.push(`${rel}:${line} <Button> renders only <${icon} /> and has no accessible name`);
  }
}

const count = offenders.length;

if (count > BASELINE) {
  console.error(
    `icon button names: ${count} icon-only buttons have no accessible name (baseline ${BASELINE}, may fall and never rise).`
  );
  console.error(
    'A Radix Tooltip does not name a button, it describes one. Add aria-label to the Button itself.'
  );
  for (const o of offenders) console.error(`  ${o}`);
  process.exit(1);
}

console.log(
  `icon button names: ${scanned} buttons inspected, ${count} unnamed icon-only (baseline ${BASELINE})`
);
