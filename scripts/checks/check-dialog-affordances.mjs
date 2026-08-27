#!/usr/bin/env node
/**
 * Two rules about how a dialog is opened and how it scrolls.
 *
 * RULE 1, hard ban: a dialog must not be opened from a `<Badge onClick>`.
 * `Badge` renders a `div` (components/ui/badge.tsx), so it takes no focus, gets no focus
 * ring, and Enter does nothing. It looks interactive to a mouse and is invisible to a
 * keyboard, which is the same defect `check-table-typography.mjs` already bans for
 * `<TableCell onClick>` and `<TableRow onClick>`, one element further out. Use `TableLink`,
 * which renders a real button or anchor and carries its affordance at rest.
 *
 * RULE 2, ratchet: a `DialogContent` that bounds its height must scroll in `DialogBody`.
 * The recipe is `max-h-... flex flex-col overflow-hidden` on the content plus a `DialogBody`
 * inside it. Put `overflow-y-auto` on the content instead and the header scrolls away with
 * the body, so on a long record the operator loses the title telling them what they are
 * looking at. `components/ui/clickable-badge.tsx` had the correct shape and the dialog it
 * sat next to did not, in the same feature, which is how easy this is to get wrong.
 *
 * A ratchet rather than a ban for rule 2, because the remaining offenders are large dialogs
 * that each want their own pass. It may fall and never rise.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// What is left after this pass. Five of the six are the tag manager, whose badges are filter
// toggles rather than dialog openers: they want a segmented-control pass with the keyboard,
// not a bulk attribute edit, because their name should change with state. The sixth is the
// dashboard project card, where the badge sits beside a real chevron button that already does
// the same thing, so that one is a duplicate affordance to remove rather than convert.
// Lower it when you fix one.
const BADGE_ONCLICK_BASELINE = 6;

// Dialogs still bounding their own height and scrolling their own content, so their titles
// scroll away. Each is a large file that wants its own pass. It was 8, then 7 when
// instance-monitoring-modal was replaced, then 6 when the warnings page's inline dialog
// moved into a shared component, then 5 when the two groups modals merged into one. That is
// what a falling ratchet is supposed to look like.
// node-detail-modal was converted with the live-stats work: it now uses DialogBody so the
// cluster name and its data-age line stay pinned while a long storage list scrolls.
// Lower it when you convert one to DialogBody.
const DIALOG_SCROLL_BASELINE = 5;

// Deprecated and rendered by nothing, so nothing in it can reach a user. Counting it would
// hide real regressions behind dead code. Same reasoning as check-icon-button-names.mjs.
const EXEMPT_FILES = new Set(['components/vm-table.tsx']);

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (full.endsWith('.tsx')) yield full;
  }
}

/** Opening tags for `name`, with their body, brace-aware so nested JSX does not end them early. */
function tags(code, name) {
  const found = [];
  const open = new RegExp(`<${name}\\b`, 'g');
  let m;
  while ((m = open.exec(code)) !== null) {
    let i = m.index + m[0].length;
    let depth = 0;
    let quote = null;
    for (; i < code.length; i += 1) {
      const c = code[i];
      if (quote) {
        if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'" || c === '`') quote = c;
      else if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      else if (c === '>' && depth === 0) break;
    }
    found.push({ tag: code.slice(m.index, i + 1), index: m.index });
  }
  return found;
}

const lineOf = (code, index) => code.slice(0, index).split('\n').length;

const badgeClicks = [];
const scrollingDialogs = [];

for (const file of walk(src)) {
  const rel = relative(src, file).split('\\').join('/');
  if (EXEMPT_FILES.has(rel)) continue;
  const code = readFileSync(file, 'utf8');

  for (const { tag, index } of tags(code, 'Badge')) {
    if (/\bonClick\s*=/.test(tag)) badgeClicks.push(`${rel}:${lineOf(code, index)}`);
  }

  for (const { tag, index } of tags(code, 'DialogContent')) {
    if (!/max-h-/.test(tag)) continue;
    // The correct shape: the content is a flex column that clips, and DialogBody scrolls.
    const correct = /overflow-hidden/.test(tag) && /flex flex-col/.test(tag);
    if (!correct) scrollingDialogs.push(`${rel}:${lineOf(code, index)}`);
  }
}

console.log(
  `dialog affordances: ${badgeClicks.length} Badge onClick (baseline ${BADGE_ONCLICK_BASELINE})   ` +
    `${scrollingDialogs.length} self-scrolling DialogContent (baseline ${DIALOG_SCROLL_BASELINE})`
);

let failed = false;

if (badgeClicks.length > BADGE_ONCLICK_BASELINE) {
  failed = true;
  console.log(
    `\nBadge onClick rose from ${BADGE_ONCLICK_BASELINE} to ${badgeClicks.length}. A Badge is a ` +
      `div: it cannot be focused and Enter does nothing, so the control is mouse-only. ` +
      `Use TableLink (components/ui/table-link.tsx):`
  );
  for (const hit of badgeClicks) console.log(`  ${hit}`);
}

if (scrollingDialogs.length > DIALOG_SCROLL_BASELINE) {
  failed = true;
  console.log(
    `\nSelf-scrolling dialogs rose from ${DIALOG_SCROLL_BASELINE} to ${scrollingDialogs.length}. ` +
      `A DialogContent that bounds its height must clip and let DialogBody scroll, or the ` +
      `title scrolls out of view. Use:\n` +
      `  <DialogContent className="max-w-Nxl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">\n` +
      `    <DialogHeader className="pr-8">...</DialogHeader>\n` +
      `    <DialogBody>...</DialogBody>`
  );
  for (const hit of scrollingDialogs) console.log(`  ${hit}`);
}

if (failed) process.exit(1);
console.log('dialog affordances: within baseline');
