#!/usr/bin/env node
// Comments explain the code; change history belongs in commits. Fails on a comment that credits a
// decision, cites a review, carries a calendar date or a release-round label, or opens with
// `note:`. backend/tests/test_comments_carry_no_change_history.py applies the same rules to Python.
//
// Comments are read from the TypeScript scanner's comment ranges, so strings and regex literals
// are never mistaken for comments.
//
// Run: node scripts/checks/check-comment-hygiene.mjs [repo-root]

import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..'));
const ts = createRequire(join(root, 'frontend', 'package.json'))('typescript');

const DIRS = ['frontend/src', 'frontend/e2e', 'scripts/checks'];
const SOURCE = /\.(ts|tsx|js|jsx|mjs|cjs)$/;
const SKIP_DIRS = new Set(['node_modules', '.next']);

const RULES = [
  ['decision credit', /\bowner (decision|decided|asked|ruling)\b|\(owner,? 20\d\d|\bdecision D\d+\b/i],
  ['review reference', /\b(security|code|design) review\b[^\n]{0,40}(\b20\d\d\b|\bfinding \d|\bv\d)/i],
  ['release round', /\bRC\d+[ -](acceptance|follow-?ups?|hotfix|outage)\b|\bFU-\d+\b|\bround \d+\b/i],
  ['note: prefix', /^note:/i],
];
// A date is history unless it sits inside a quoted or backticked example.
const DATE = /\b20\d\d-[01]\d-[0-3]\d\b(?!T\d)/g;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (SOURCE.test(entry)) yield full;
  }
}

function kindOf(name) {
  if (name.endsWith('.tsx')) return ts.ScriptKind.TSX;
  if (name.endsWith('.ts')) return ts.ScriptKind.TS;
  if (name.endsWith('.jsx')) return ts.ScriptKind.JSX;
  return ts.ScriptKind.JS;
}

/** Every comment in a file, as {line, text}, from the compiler's own comment ranges. */
function comments(name, text) {
  const sf = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, kindOf(name));
  const seen = new Map();
  const add = (ranges) => {
    for (const r of ranges ?? []) {
      if (!seen.has(r.pos)) {
        seen.set(r.pos, { line: sf.getLineAndCharacterOfPosition(r.pos).line + 1, text: text.slice(r.pos, r.end) });
      }
    }
  };
  const visit = (node) => {
    if (node.kind === ts.SyntaxKind.JsxText) return;
    const kids = node.getChildren(sf);
    if (kids.length === 0) {
      add(ts.getLeadingCommentRanges(text, node.pos));
      add(ts.getTrailingCommentRanges(text, node.end));
      return;
    }
    kids.forEach(visit);
  };
  visit(sf);
  return [...seen.values()];
}

/** Rule names a comment breaks; each comment line is checked without its comment marker. */
export function violations(comment) {
  const found = new Set();
  for (const raw of comment.split('\n')) {
    const line = raw.replace(/^\s*(\/\/+|\/\*+|\*+\/?|\{\s*\/\*+)\s?/, '').replace(/\*\/\s*\}?\s*$/, '').trim();
    for (const [name, rule] of RULES) if (rule.test(line)) found.add(name);
    for (const m of line.matchAll(DATE)) {
      const before = line.slice(0, m.index);
      const quoted = (before.match(/[`'"]/g) ?? []).length % 2 === 1;
      if (!quoted) found.add('date');
    }
  }
  return [...found];
}

// Self-test, so a rule that stops matching fails loudly instead of passing everything.
assert.deepEqual(violations('// Read-only past the term (owner decision 2026-10-27).').sort(), ['date', 'decision credit']);
assert.deepEqual(violations('/* Security review of v1.0.0-RC4, finding 1. */'), ['review reference']);
assert.deepEqual(violations('// RC5 acceptance FU-3 moved this.'), ['release round']);
assert.deepEqual(violations('// note: the cache is per worker.'), ['note: prefix']);
assert.deepEqual(violations(' * note: rows arrive unsorted.'), ['note: prefix']);
assert.deepEqual(violations('// important: the owner key decides who may rotate.'), []);
assert.deepEqual(violations('// Parses `2026-01-31` and "2026-02-01" style dates.'), []);
assert.deepEqual(violations('// Timestamps look like 2026-01-31T10:00:00Z.'), []);
assert.deepEqual(violations('// Rounds to two decimals, ex; 1.234 -> 1.23.'), []);

const failures = [];
let files = 0;
let scanned = 0;
for (const dir of DIRS) {
  for (const file of walk(join(root, dir))) {
    files += 1;
    const rel = relative(root, file);
    for (const c of comments(rel, readFileSync(file, 'utf8'))) {
      scanned += 1;
      const broken = violations(c.text);
      if (broken.length) failures.push(`${rel}:${c.line} ${broken.join(', ')}: ${c.text.split('\n')[0].trim().slice(0, 100)}`);
    }
  }
}

if (files === 0) failures.push('no source files walked');
if (failures.length) {
  console.error('check-comment-hygiene: FAIL (history belongs in commits, not comments)');
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`comment hygiene: ${scanned} comments in ${files} files carry no change history`);
