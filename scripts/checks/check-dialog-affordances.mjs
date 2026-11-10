#!/usr/bin/env node
/**
 * Two rules about how a dialog is opened and how it scrolls. Both are ratchets: each count may
 * fall and never rise.
 *
 * Rule 1: a dialog must not be opened from a `<Badge onClick>`. `Badge` renders a `div`
 * (components/ui/badge.tsx), so it takes no focus, gets no focus ring, and Enter does nothing.
 * `check-table-typography.mjs` bans the same defect for `<TableCell onClick>` and
 * `<TableRow onClick>`. Use `TableLink`, which renders a real button or anchor.
 *
 * Rule 2: a `DialogContent` that bounds its height must scroll in `DialogBody`, with
 * `max-h-... flex flex-col overflow-hidden` on the content. With `overflow-y-auto` on the content
 * the header scrolls away with the body and a long record loses its title. The remaining
 * offenders are large dialogs that each want their own pass.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// The one remaining Badge onClick is in tag-manager-legacy.tsx, which nothing imports.
// Lower this when you fix one.
const BADGE_ONCLICK_BASELINE = 1;

// Dialogs that still bound their own height and scroll their own content, so their titles
// scroll away. Each is a large file that wants its own pass.
// Lower this when you convert one to DialogBody.
const DIALOG_SCROLL_BASELINE = 5;

// A dead file that nothing imports goes here rather than into a baseline bump.
const EXEMPT_FILES = new Set([]);

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
