#!/usr/bin/env node
/**
 * Ratchet: an icon-only button must carry its own accessible name.
 *
 * A `<Button>` whose children are all icons renders as a bare `button` in the
 * accessibility tree unless the element itself names it. Wrapping it in a Radix
 * `Tooltip` does not fix this: the tooltip contributes `aria-describedby` when it opens,
 * which is a description, not a name. A screen reader announces "button" and nothing else.
 *
 * Measured in the live app before this file existed, as admin:
 *
 *   /inventory              56 buttons, 22 with no accessible name, all `MoreVertical`
 *   /inventory/instances   139 buttons,  4 with none, the pagination chevrons
 *
 * `title` counts as a name here because the accname algorithm falls back to it, but it is
 * the weakest option and it is what caused the instances-row defect: that title was
 * permission-dependent, so five buttons announced "No operate permission" and never said
 * which action they were. Prefer `aria-label`. The check accepts `title` so it stays a
 * naming rule rather than an attribute-style rule.
 *
 * Baseline rather than zero, because the count is what it is today and a ratchet that
 * cannot pass is a ratchet somebody deletes. It may fall and never rise.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// What is left after this pass. The surfaces already cleared are the ones an operator
// meets first: the dashboard project menus, the shared table pagination, the instance row
// actions on both the dashboard and the instances page, and every kebab menu in the app.
//
// The remainder is concentrated in the console and terminal surface, where a toolbar is
// almost entirely icons, plus the monitoring chart controls. Those want their own pass
// with the keyboard, not a bulk attribute edit, because several are toggles whose name
// should change with state rather than sit still.
//
// Lower it when you fix one. It may fall and never rise.
//
// 55 -> 54: the back button on the Create Project form. The dashboard project kebab, which
// the comment above already counted as cleared, was in fact invisible to this check the
// whole time: its aria-label had landed below the opening tag as a text child, so the body
// was not one self-closing element and isIconOnly() skipped it. Fixing that leak did not
// move the number, it only made the button countable. See check-jsx-stray-attrs.mjs.
const BASELINE = 54;

// `vm-table.tsx` is deprecated and rendered by nothing, so its buttons cannot be announced
// to anybody. Counting them would mean either a permanently failing check or a baseline
// that hides real regressions behind dead code.
const EXEMPT_FILES = new Set(['components/vm-table.tsx']);

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
 * Deliberately narrow. Two broader attempts were tried and both were wrong in the
 * direction that makes a ratchet worthless:
 *
 *   dropping whole `{...}` expressions      127 hits, 101 of them false, because
 *                                           `{isPending ? <Loader2 /> : "Refresh"}` is
 *                                           the house pending-button idiom
 *   then also accepting quoted literals      84 hits, still false on bare JSX text, so
 *                                           the sign-in button reading "Signing in..."
 *                                           inside a ternary was reported as nameless
 *
 * Detecting rendered text from source is a parser's job, not a regex's. This shape needs
 * no text detection at all: one self-closing element is unambiguously an icon and nothing
 * else, which is exactly the defect measured in the browser (kebab menus and pagination
 * chevrons). A button whose icon sits inside a ternary is not caught here. That is a known
 * hole, accepted on purpose, because a check nobody trusts gets deleted and this one can
 * be trusted absolutely.
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
