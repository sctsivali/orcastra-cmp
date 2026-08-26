#!/usr/bin/env node
/**
 * One product, two casing conventions, and no way to tell which slot you were in.
 *
 * /technologies rendered `Fleet totals` as an <h2> directly above a card whose title was
 * `CPU Resources` and whose rows were labelled `Total Cores` and `Memory per Node`, then
 * `Hardware by node` directly above rows labelled `CPU topology`, `Base clock` and
 * `Physical cores`. `Logical CPUs` sat one line above `Physical cores` in the same grid.
 * The owner saw the seam before anyone else did.
 *
 * The rule the repo already followed at the top, and now follows all the way down:
 *
 *   Title Case   anything that NAMES a thing: h1-h4, CardTitle, DialogTitle,
 *                AlertDialogTitle, SheetTitle, TabsTrigger, TableHead, DetailSection
 *                title, DetailField label, StatCard title, and a column `label:`.
 *   sentence     anything that SAYS something. Empty and error states are sentences even
 *                when they occupy one of the slots above, so they are checked the other
 *                way round: `No Clusters Registered` is as wrong as `Hardware by node`.
 *
 * Both directions matter. The previous audit
 * (documentation/ui/heading-and-copy-conventions.md) found the empty states split 19 to 2
 * with three files carrying both spellings, and one of them, clustering/members/page.tsx,
 * shipped `No Members Found` and `No members found` six lines apart.
 *
 * Nothing in the repo could see any of it. tsc and eslint do not read English, and none of
 * the twelve structural guards looks at string content.
 *
 * Deliberately parser-free: the CI `guards` job runs with no `npm ci`, so importing
 * typescript from frontend/node_modules would fail there. Regex only.
 *
 * What it does not catch, stated rather than papered over:
 *
 *   interpolated titles   `Delete Instance &quot;{name}&quot;?` and
 *                         `Cards on This Node ({n})` hold a JSX expression, so the child
 *                         regex skips them. That half is covered at the DOM instead, by
 *                         /root/qa-orcastra/tests/heading-case.spec.ts, which reads what the
 *                         browser paints and so sees the interpolated result.
 *   indirected titles     hardware-aggregate.tsx builds its cards as object literals and
 *                         renders `{category.title}`. The literal is checked by the
 *                         `label:` rule; a `title:` key is not, because ~200 of them are
 *                         toast titles, which are sentences.
 *   `label:` beginning    a digit is skipped, because select option values share the exact
 *                         shape of a column definition (`{ value, label }`) and
 *                         "15 minutes" is data, not a heading.
 *
 * Zero, not a ratchet, in both directions. The tree is clean and neither shape is ever
 * intentional.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// Optional root, so the guard can be pointed at a fixture tree to prove it still fails on
// the broken shape. CI passes `.` from the repo root, which resolves the same as the default.
const root = process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// Lower case in the middle of a title is correct for these. Matches the owner's own
// example, "Hardware by Node".
const SMALL = new Set([
  'a', 'an', 'the',
  'and', 'but', 'or', 'nor', 'for', 'yet', 'so',
  'at', 'by', 'in', 'of', 'on', 'to', 'up', 'as', 'per', 'via',
  'from', 'with', 'into', 'over', 'off', 'out',
]);

// An empty or error state, whichever element renders it. These are sentences: they explain
// a situation rather than name a section. "Something" earns its place because
// app/error.tsx is the root error boundary and "Something went wrong" is the whole message.
const EMPTY_LEAD = /^(No|Not|Nothing|None|Loading|Could|Cannot|Failed|Unable|Nobody|Something)\b/;

// Rendered by nothing. Both are already carved out of check-icon-button-names.mjs and
// check-dialog-affordances.mjs for the same reason: counting dead code hides real
// regressions behind it. vm-create-form.tsx even renders <h1>Create New Project</h1> inside
// what is meant to be an instance form.
const EXEMPT_FILES = new Map([
  ['components/vm-table.tsx', 'rendered by nothing, reachable only through the components barrel'],
  ['components/vm-create-form.tsx', 'rendered by nothing, and its h1 names the wrong entity'],
]);

// Product terms whose spelling is deliberate, with the reason. A hyphenated token is
// otherwise treated as one word, so Auto-update and Real-time are left alone.
const SPELLINGS = new Map([
  ['Break-Glass', 'matches the shipped backend OpenAPI tag, tags=["Break-Glass"]'],
  ['Cloud-Init', "Title Case of cloud-init, the upstream project's own hyphenated name"],
]);

const ELEMENTS = ['h1', 'h2', 'h3', 'h4', 'CardTitle', 'DialogTitle', 'AlertDialogTitle', 'SheetTitle', 'TabsTrigger', 'TableHead'];
// Tolerates a leading icon sibling, which is how three of these titles are written.
const elementRe = (name) =>
  new RegExp(`<${name}\\b[^>]*>((?:\\s*<[^>]*/>\\s*)*)\\s*([A-Za-z][^<>{}\\n]{1,80}?)\\s*</${name}>`, 'g');

const PROPS = [['DetailSection', 'title'], ['DetailField', 'label'], ['StatCard', 'title']];
// `[^<]` rather than `[^>]`: an attribute value may itself hold a `>`, and network-acls/page.tsx
// writes `colSpan={nodes.length > 0 ? 7 : 6}` above its title. A `>`-bounded body stopped there
// and the empty state went unchecked. `<` still bounds it, so the match cannot cross into a
// child tag and credit this component with some other element's title.
const propRe = (comp, prop) => new RegExp(`<${comp}\\b[^<]{0,600}?\\b${prop}="([^"]{2,90})"`, 'g');

const EMPTY_PROPS = [['EmptyState', 'title'], ['TableEmptyRow', 'title']];

const LABEL = /\blabel:\s*"([A-Za-z][^"]{1,60})"/g;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (full.endsWith('.tsx') || full.endsWith('.ts')) yield full;
  }
}

// Word 2..n must not start lower case unless it is a small word. A hyphenated token counts
// as one word, so only its first letter is judged.
function badTitleWord(s) {
  const words = s.trim().split(/\s+/).filter(Boolean);
  if (words.length < 2) return null;
  for (let i = 1; i < words.length; i += 1) {
    const w = words[i].replace(/[^A-Za-z0-9-]/g, '');
    if (!w || !/^[a-z]/.test(w)) continue;
    if (SMALL.has(w.toLowerCase())) continue;
    return w;
  }
  return null;
}

// An initialism, including a pluralised one. "ACLs", "ISOs" and "CPUs" are single upper case
// tokens carrying a lower case plural, not Title Case words.
function isAcronym(w) {
  if (w === w.toUpperCase()) return true;
  return w.length > 2 && w.endsWith('s') && w.slice(0, -1) === w.slice(0, -1).toUpperCase();
}

// Word 2..n must not start upper case unless it is an acronym, an initialism, or a proper
// noun the app already spells that way.
function badSentenceWord(s) {
  const words = s.trim().split(/\s+/).filter(Boolean);
  for (let i = 1; i < words.length; i += 1) {
    const w = words[i].replace(/[^A-Za-z0-9-]/g, '');
    if (!w || !/^[A-Z]/.test(w)) continue;
    if (isAcronym(w)) continue;
    if (SPELLINGS.has(w)) continue;
    return w;
  }
  return null;
}

const offenders = [];
let scanned = 0;
let titles = 0;

for (const file of walk(src)) {
  const code = readFileSync(file, 'utf8');
  const rel = relative(src, file).split(sep).join('/');
  if (EXEMPT_FILES.has(rel)) continue;
  scanned += 1;

  const lineOf = (i) => code.slice(0, i).split('\n').length;
  const found = [];

  for (const name of ELEMENTS) {
    const re = elementRe(name);
    let m;
    while ((m = re.exec(code))) found.push({ kind: `<${name}>`, s: m[2], i: m.index, titleSlot: true, emptySlot: true });
  }
  for (const [comp, prop] of PROPS) {
    const re = propRe(comp, prop);
    let m;
    while ((m = re.exec(code))) found.push({ kind: `${comp} ${prop}=`, s: m[1], i: m.index, titleSlot: true, emptySlot: true });
  }
  for (const [comp, prop] of EMPTY_PROPS) {
    const re = propRe(comp, prop);
    let m;
    while ((m = re.exec(code))) found.push({ kind: `${comp} ${prop}=`, s: m[1], i: m.index, titleSlot: false, emptySlot: true });
  }
  {
    LABEL.lastIndex = 0;
    let m;
    // emptySlot is false here on purpose. A bare `label:` is not necessarily a heading, and
    // hooks/use-loading-progress.ts names its steps "Initializing", "Fetching VMs",
    // "Loading Nodes": a coherent Title Case set of step names, not an empty state.
    while ((m = LABEL.exec(code))) found.push({ kind: 'label:', s: m[1], i: m.index, titleSlot: true, emptySlot: false });
  }

  for (const f of found) {
    titles += 1;
    const where = `${rel}:${lineOf(f.i)}`;
    if (f.emptySlot && EMPTY_LEAD.test(f.s)) {
      const bad = badSentenceWord(f.s);
      if (bad) offenders.push(`${where} ${f.kind} "${f.s}" is an empty or error state, so "${bad}" should be lower case`);
      continue;
    }
    if (!f.titleSlot) continue;
    const bad = badTitleWord(f.s);
    if (bad) offenders.push(`${where} ${f.kind} "${f.s}" is a title, so "${bad}" should be capitalised`);
  }
}

// A scan over nothing passes for the wrong reason.
if (scanned < 200) offenders.push(`only ${scanned} files scanned, expected the walk to reach more`);
if (titles < 400) offenders.push(`only ${titles} titles matched, expected the patterns to reach more`);

if (offenders.length > 0) {
  console.error(`heading case: ${offenders.length} string(s) break the casing contract.`);
  console.error('Titles are Title Case. Empty and error states are sentences, in any element.');
  console.error('Rules and carve-outs: documentation/ui/heading-and-copy-conventions.md');
  for (const o of offenders) console.error(`  ${o}`);
  process.exit(1);
}

console.log(`heading case: ${titles} titles across ${scanned} files, all correctly cased`);
