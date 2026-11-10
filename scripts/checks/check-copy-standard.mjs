#!/usr/bin/env node
// One spelling per idea in user-visible copy: the ellipsis, a missing value, plurals, toast titles,
// grouped numbers that must not follow the browser locale, and time units. Each rule holds at zero.
//
// Comments are stripped first, so only executable code is asserted. Parser-free and dependency-free,
// for the reason check-jsx-stray-attrs.mjs gives.
//
// Run: node scripts/checks/check-copy-standard.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

const RULES = [
  [/[A-Za-z0-9)}]\.\.\.(?=["'`<\s]|$)/, 'use the ellipsis character, not three dots'],
  [/["'`]N\/A["'`]|["'`]n\/a["'`]/, 'use EMPTY_VALUE for a missing value'],
  [/(?:\|\||\?\?|:)\s*"-"/, 'use EMPTY_VALUE for a missing value'],
  [/(?:\}|\d) [a-z]+\(s\)/, 'use plural() instead of a (s) suffix'],
  [/\bcapability\(s\)/, 'use plural() instead of a (s) suffix'],
  [/\btitle:\s*["'`](?:Error|Success|Validation Error|Configuration Error|Copied!)["'`]/, 'a toast title names its object: Could not delete cluster, Cluster deleted'],
  [/\.toLocaleString\(/, 'use formatCount, which does not change with the browser locale'],
  [/(?:\b\d+(?:\.\d+)?|\}) (?:ms|s|min|sec|secs)\b(?=[\s.,;:)"'`<]|$)/, 'a single time is glued: 30s, 120ms'],
  [/ • /, 'the separator is a middle dot'],
  [/\be\.g\.[, ]/, 'write "for example" in product copy'],
  // Operators open the graphical console; SPICE is only the wire protocol under it. Identifiers
  // such as SpiceSession and the lowercase package and stored values (spice-vdagent,
  // spice-graphics) do not match; lib/spice/ is the protocol client itself.
  [/\b(?:SPICE|Spice)\b(?!-)/, 'say graphical console: SPICE is the wire protocol, not what the operator opens'],
  [/\bbg-(?:warning|destructive)\/\d+\b[^"]*\bborder\b|\bborder-(?:warning|destructive)\/\d+\b[^"]*\bbg-(?:warning|destructive)\/\d+\b/, 'use Alert, DialogSubmitError or PartialReadNotice instead of a hand-rolled box'],
];

// Page-wide strips are chrome rather than boxes, and they carry no message shape Alert could hold.
const BOX_EXEMPT = new Set([
  'components/tenant-term-notice.tsx',
  'components/vault-notice.tsx',
  'components/release/release-notice.tsx',
  'components/org-personalization-settings.tsx',
]);

const stripComments = (source) =>
  source
    .replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, ' '))
    .replace(/\{\s*\/\*[\s\S]*?\*\/\s*\}/g, (comment) => comment.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:"'`])\/\/.*$/gm, (comment, lead) => lead + ' '.repeat(comment.length - lead.length));

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.tsx?$/.test(full)) yield full;
  }
}

const offenders = [];
let scanned = 0;
for (const file of walk(src)) {
  scanned += 1;
  const rel = relative(src, file).split(sep).join('/');
  stripComments(readFileSync(file, 'utf8')).split('\n').forEach((text, index) => {
    for (const [pattern, why] of RULES) {
      if (!pattern.test(text)) continue;
      if (why.startsWith('use Alert') && (BOX_EXEMPT.has(rel) || rel.startsWith('components/ui/'))) continue;
      if (why.startsWith('use formatCount') && rel === 'lib/utils.ts') continue;
      if (why.startsWith('say graphical console') && rel.startsWith('lib/spice/')) continue;
      offenders.push(`frontend/src/${rel}:${index + 1} ${why}`);
    }
  });
}

if (scanned < 200) offenders.push(`only scanned ${scanned} files, so this asserts nothing`);
if (offenders.length) {
  console.error('Copy that is not on the one spelling:');
  for (const offender of offenders) console.error(`  ${offender}`);
  process.exit(1);
}
console.log(`copy standard: ${scanned} files, ${RULES.length} rules, none broken`);
