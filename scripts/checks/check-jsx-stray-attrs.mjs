#!/usr/bin/env node
/**
 * A JSX attribute below the `>` of its own opening tag is a text child, rendered verbatim:
 *
 *   <Button variant="ghost" size="icon" className="h-7 w-7">
 *   aria-label={`Project actions for ${project.name}`}
 *
 * The button draws the literal text and stays unnamed. tsc and eslint accept it, and
 * check-icon-button-names.mjs misses it: isIconOnly() needs exactly one self-closing child.
 *
 * Line-based, without a parser: it flags a line of nothing but attributes in child position,
 * but not a leak on the tag's own line (`<Button className="x"> aria-label={foo}`).
 * Zero findings allowed; there is no baseline.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// Optional root, so the guard can be pointed at a fixture tree to prove it still fails on
// the broken shape. CI passes `.` from the repo root, which resolves the same as the default.
const root = process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// A JSX attribute name: camelCase, or the aria-/data- hyphenated families. Two characters
// minimum on the camelCase branch, so prose like `x= {value}` cannot trip the check.
const NAME = '(?:[a-z][A-Za-z0-9]+|(?:aria|data)-[a-z][a-z0-9-]*)';

// A run of attributes, optionally ending in a bare `name=` whose {value} became a sibling
// expression. Anchored at both ends: the whole line has to be attributes and nothing else, so
// `title={x} and some prose` and `count={n} items` are not reported.
const LEAK = new RegExp(`^(?:${NAME}=(?:"[^"]*"|\\{.*?\\})\\s+)*${NAME}=(?:"[^"]*"|\\{.*\\})?\\s*$`);

// The previous non-blank line closed an opening tag, so what follows it is child position.
const CLOSES_OPENING_TAG = />$/;
const SELF_CLOSING = /\/>$/;
const ARROW = /=>$/;
// `<OverflowCell<string>` ends in `>` but that `>` closes a type argument, not the tag.
const GENERIC_TYPE_ARG = /<[A-Z][A-Za-z0-9]*<[^<>]*>$/;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (full.endsWith('.tsx') || full.endsWith('.jsx')) yield full;
  }
}

const offenders = [];
let scanned = 0;

for (const file of walk(src)) {
  scanned += 1;
  const lines = readFileSync(file, 'utf8').split('\n');
  const rel = relative(src, file).split(sep).join('/');

  for (let i = 1; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (!line || !LEAK.test(line)) continue;

    let j = i - 1;
    while (j >= 0 && lines[j].trim() === '') j -= 1;
    if (j < 0) continue;
    const tag = lines[j].trim();

    if (!CLOSES_OPENING_TAG.test(tag)) continue;
    if (SELF_CLOSING.test(tag) || ARROW.test(tag) || GENERIC_TYPE_ARG.test(tag)) continue;

    offenders.push({ where: `${rel}:${i + 1}`, tag, line });
  }
}

if (offenders.length > 0) {
  console.error(
    `jsx stray attributes: ${offenders.length} attribute(s) sit below their own opening tag and render as text.`
  );
  console.error('Move the attribute inside the tag, before the closing `>`.');
  for (const o of offenders) {
    console.error(`  ${o.where}`);
    console.error(`    tag   ${o.tag}`);
    console.error(`    leak  ${o.line}`);
  }
  process.exit(1);
}

console.log(`jsx stray attributes: ${scanned} files scanned, none leaked into child position`);
