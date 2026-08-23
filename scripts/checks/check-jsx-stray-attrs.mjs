#!/usr/bin/env node
/**
 * A JSX attribute that ends up below the `>` of its own opening tag is not an attribute.
 * It is a text child, and JSX renders it verbatim.
 *
 *   <Button variant="ghost" size="icon" className="h-7 w-7">
 *   aria-label={`Project actions for ${project.name}`}
 *     <MoreVertical className="h-4 w-4" />
 *   </Button>
 *
 * That shipped. Every project card on /inventory drew the literal string
 * "aria-label=Project actions for <name>" across its title and pushed the host badge out
 * of the card, and the kebab trigger it was meant to name announced as a bare "button"
 * for three days. It arrived in c49df53, the commit whose entire purpose was to give that
 * button an accessible name.
 *
 * Nothing already in the repo could see it:
 *
 *   tsc          a text child is valid JSX, so there is no type error
 *   eslint       likewise, no rule fires
 *   check-icon-button-names.mjs
 *                skips it. isIconOnly() requires the body to be exactly one
 *                self-closing element, and the stray text makes the body look like an
 *                ordinary labelled button, so the button is never even counted
 *
 * Hence this file. Deliberately parser-free: the CI `guards` job runs with no `npm ci`,
 * so importing typescript from frontend/node_modules would fail there.
 *
 * What it catches: a line that is nothing but attributes, sitting where a child belongs.
 * That is the copy-paste shape the defect actually took.
 *
 * What it does not catch: a leak written inline on the same line as the tag,
 * `<Button className="x"> aria-label={foo}`. Detecting that needs a real parser. The hole
 * is stated rather than papered over, in the register the other guards here use.
 *
 * Cross-checked once against a full TypeScript AST walk of the same tree, looking for
 * JsxText nodes shaped like an attribute. Both found dashboard.tsx:719 and nothing else.
 *
 * Zero, not a ratchet. The tree is clean and this shape is never intentional.
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
