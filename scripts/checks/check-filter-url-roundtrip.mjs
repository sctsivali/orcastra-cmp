#!/usr/bin/env node
// Round-trip guard for the filter query-string codec.
//
// A filter that serialises into a shared link and parses back into something else is the worst
// failure this system can have: the URL looks right, the table is wrong, and nothing errors.
// Static analysis cannot see it, so this compiles the codec standalone and exercises it.
//
// frontend/src/lib/filter-url.ts is written alias-free with no runtime imports precisely so
// this can compile it on its own without pulling the whole app graph in.
//
// Run: node scripts/checks/check-filter-url-roundtrip.mjs [repo-root]

import { execFileSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const frontend = join(root, 'frontend');
const out = mkdtempSync(join(tmpdir(), 'filter-url-'));
// note: tsc emits ESM, and node treats a bare .js in a directory with no package.json as
// CommonJS. Without this the import fails with "Unexpected token 'export'".
writeFileSync(join(out, 'package.json'), '{"type":"module"}');

let failures = 0;
let checks = 0;

function check(name, condition, detail) {
  checks += 1;
  if (!condition) {
    failures += 1;
    console.error(`  FAIL ${name}${detail ? `: ${detail}` : ''}`);
  }
}

function eq(name, got, want) {
  check(name, JSON.stringify(got) === JSON.stringify(want), `${JSON.stringify(got)} != ${JSON.stringify(want)}`);
}

// important: `npx tsc` is not the TypeScript compiler. `tsc` is a separate, unrelated
// package on npm, and npx fetches it whenever the workspace has no local binary. That is
// exactly the Structural guards job, which is checkout plus setup-node with no install by
// design, so this step failed with "This is not the tsc command you are looking for" on
// every run from ef847d1 onward. It passed locally the whole time because
// frontend/node_modules/.bin/tsc exists there.
//
// Scope of that outage, read off the runs rather than inferred: the steps before this one
// all executed and passed every time, so no other guard was skipped while this was the last
// step. From c49df53, when a step was appended after it, exactly one check is taken down
// with the job. The cost was that THIS guard never ran in CI at all, not that it silenced
// its neighbours.
//
// The fallback is pinned to the major the app declares. An unpinned `npx -p typescript`
// fetches whatever is newest, and the first attempt at this fix failed in CI on TS6's
// error TS5112 while passing locally on the repo's 5.9. Compile the guard with the same
// major the product compiles with, or the guard is testing a different compiler.
const localTsc = resolve(frontend, 'node_modules', '.bin', 'tsc');
const [cmd, leadingArgs] = existsSync(localTsc)
  ? [localTsc, []]
  : ['npx', ['-p', 'typescript@5', 'tsc']];

// important: compile from the temp directory, not from frontend/. TS5112 rejects naming a
// file on the command line while a tsconfig.json sits in the working directory, and
// frontend/ has one. Copying the source out also makes "standalone" literal: if the codec
// ever grows an alias or a runtime import, it fails here rather than silently resolving
// through the app's config.
copyFileSync(resolve(frontend, 'src', 'lib', 'filter-url.ts'), join(out, 'filter-url.ts'));

try {
  execFileSync(
    cmd,
    [...leadingArgs, 'filter-url.ts', '--outDir', '.', '--module', 'esnext', '--target', 'es2020',
     '--moduleResolution', 'bundler', '--strict'],
    { cwd: out, stdio: 'pipe' },
  );
} catch (error) {
  console.error('filter-url codec failed to compile standalone:');
  console.error(error.stdout?.toString() ?? error.message);
  console.error('\nThe codec must stay alias-free with no runtime imports so this guard can run it.');
  rmSync(out, { recursive: true, force: true });
  process.exit(1);
}

const codec = await import(pathToFileURL(resolve(out, 'filter-url.js')).href);
const {
  serialiseRange, parseRange, serialiseDate, parseDate, serialiseText, parseText,
  serialiseFacet, parseFacet,
  matchesRange, matchesText, resolvePreset, dayStart, toDayString,
  INACTIVE_RANGE, INACTIVE_DATE, INACTIVE_TEXT, EMPTY_PARAM,
  EMPTY_FACET, EMPTY_FACET_PARAM,
} = codec;

// ---------------------------------------------------------------- number ranges
for (const empty of ['exclude', 'include', 'only']) {
  for (const [min, max] of [[2, 8], [0, 0], [-5, 5], [1, null], [null, 30], [null, null]]) {
    const state = { min, max, empty };
    const wire = serialiseRange(state);
    const back = parseRange(wire, false);
    if (wire === null) {
      eq(`range inactive round trip ${JSON.stringify(state)}`, back, INACTIVE_RANGE);
    } else if (empty === 'only') {
      eq(`range only-empty ${JSON.stringify(state)}`, back, { min: null, max: null, empty: 'only' });
    } else {
      eq(`range round trip ${JSON.stringify(state)}`, back, state);
    }
  }
}
check('inactive range writes no key', serialiseRange(INACTIVE_RANGE) === null);
eq('reversed range is swapped', parseRange('n:8..2'), { min: 2, max: 8, empty: 'exclude' });
eq('bare number is an exact match', parseRange('7'), { min: 7, max: 7, empty: 'exclude' });
eq('non-numeric degrades to inactive', parseRange('n:banana..3'), { min: null, max: 3, empty: 'exclude' });
eq('entirely non-numeric is inactive', parseRange('n:banana'), INACTIVE_RANGE);
eq('Infinity is rejected', parseRange('n:Infinity..3'), { min: null, max: 3, empty: 'exclude' });
eq('integer truncates', parseRange('n:2.7..8.9', true), { min: 2, max: 8, empty: 'exclude' });
eq('float survives when not integer', parseRange('n:2.5..8.5', false), { min: 2.5, max: 8.5, empty: 'exclude' });

check('range predicate: inside', matchesRange(5, { min: 2, max: 8, empty: 'exclude' }));
check('range predicate: below', !matchesRange(1, { min: 2, max: 8, empty: 'exclude' }));
check('range predicate: bounds inclusive', matchesRange(2, { min: 2, max: 8, empty: 'exclude' }) && matchesRange(8, { min: 2, max: 8, empty: 'exclude' }));
check('range predicate: missing excluded by default', !matchesRange(null, { min: 2, max: 8, empty: 'exclude' }));
check('range predicate: missing included on request', matchesRange(null, { min: 2, max: 8, empty: 'include' }));
check('range predicate: only-empty rejects a value', !matchesRange(5, { min: null, max: null, empty: 'only' }));
check('range predicate: only-empty accepts missing', matchesRange(undefined, { min: null, max: null, empty: 'only' }));
check('range predicate: inactive matches everything', matchesRange(null, INACTIVE_RANGE) && matchesRange(99, INACTIVE_RANGE));

// ---------------------------------------------------------------- dates
const noon = new Date(2026, 7, 20, 12, 0, 0).getTime();
for (const preset of ['today', 'yesterday', 'last7d', 'last30d', 'last90d', 'expired', 'next7d', 'next30d', 'next90d']) {
  const state = { min: null, max: null, empty: 'exclude', preset };
  eq(`date preset round trip ${preset}`, parseDate(serialiseDate(state)), state);
}
{
  const from = new Date(2026, 7, 1).getTime();
  const to = new Date(2026, 7, 14).getTime();
  const state = { min: from, max: to + 86_399_999, empty: 'exclude', preset: null };
  eq('date custom range round trip', parseDate(serialiseDate(state)), state);
  eq('date open-ended from', parseDate(serialiseDate({ ...state, max: null })), { ...state, max: null });
  eq('date open-ended to', parseDate(serialiseDate({ ...state, min: null })), { ...state, min: null });
}
eq('date only-empty', parseDate('d:~none'), { min: null, max: null, empty: 'only', preset: null });
eq('malformed date is inactive', parseDate('d:nonsense'), INACTIVE_DATE);
eq('impossible date is inactive', parseDate('d:2026-13-45'), INACTIVE_DATE);
eq('leap day parses', parseDate('d:2028-02-29..2028-02-29').min, new Date(2028, 1, 29).getTime());
check('inactive date writes no key', serialiseDate(INACTIVE_DATE) === null);
check('preset resolves against the reference day', (() => {
  const { min, max } = resolvePreset('today', noon);
  return min === dayStart(noon) && max === dayStart(noon) + 86_399_999;
})());
check('last7d spans seven calendar days', (() => {
  const { min, max } = resolvePreset('last7d', noon);
  return Math.round((max + 1 - min) / 86_400_000) === 7;
})());
eq('day string is local', toDayString(new Date(2026, 7, 20, 23, 30).getTime()), '2026-08-20');

// ---------------------------------------------------------------- text
const NASTY = [
  'lxdbr', 'a,b', 'a..b', 'n:5', 'd:today', 't:x', EMPTY_PARAM, '100%', '%zz', 'a%2Cb',
  'with space', 'üñî 名前', '/1.0/instances/x?project=y', 'a'.repeat(300), '~none,~none',
];
for (const query of NASTY) {
  for (const empty of ['exclude', 'include']) {
    const state = { query, empty };
    eq(`text round trip ${JSON.stringify(query)} ${empty}`, parseText(serialiseText(state)), state);
  }
}
eq('text only-empty', parseText('t:~none'), { query: '', empty: 'only' });
eq('empty text is inactive', parseText(serialiseText(INACTIVE_TEXT)), INACTIVE_TEXT);
check('inactive text writes no key', serialiseText(INACTIVE_TEXT) === null);
eq('bare value is accepted as contains', parseText('dc1'), { query: 'dc1', empty: 'exclude' });

check('text predicate: substring, case-insensitive', matchesText('LXDBR0', { query: 'lxdbr', empty: 'exclude' }));
check('text predicate: array any', matchesText(['a', 'lxdbr0'], { query: 'lxdbr', empty: 'exclude' }));
check('text predicate: missing excluded', !matchesText(null, { query: 'x', empty: 'exclude' }));
check('text predicate: only-empty', matchesText('', { query: '', empty: 'only' }) && !matchesText('a', { query: '', empty: 'only' }));
check('text predicate: inactive matches everything', matchesText(null, INACTIVE_TEXT));

// ---------------------------------------------------------------- enum facets
// note: these call the shipped serialiseFacet/parseFacet. An earlier version of this block
// re-implemented the encoding inline with a bare encodeURIComponent, so it agreed with itself
// while the real function was missing the tilde escape. A guard that tests its own copy of the
// code under test is not a guard.
const ENUM_VALUES = ['nuc2', 'bridge', 'Created', 'virtual-machine', 'x86_64', 'zfs', 'default',
                     'cust-suka-hati-84426622', 'SCT-OPS', 'critical',
                     // The adversarial half: separator, sigils, escapes and the sentinel itself.
                     EMPTY_FACET_PARAM, 'a,b', 'a..b', 'n:2', 'd:last7d', 't:x', '100%', '%zz',
                     'has space', 'ünïcødé', '~', '~~', '~none~', 'default~none'];

for (const value of ENUM_VALUES) {
  const wire = serialiseFacet([value]);
  const back = parseFacet(wire);
  eq(`facet round trip ${JSON.stringify(value)}`, back, [value]);
  check(`facet ${JSON.stringify(value)} carries no bare sigil`,
        !/^[ndt]:/.test(wire),
        `serialised as ${wire}`);
}

// The empty sentinel is the one value that is allowed to serialise to the reserved token, and a
// literal "~none" must NOT collide with it in either direction.
eq('empty sentinel serialises to the reserved token', serialiseFacet([EMPTY_FACET]),
   EMPTY_FACET_PARAM);
eq('empty sentinel round trips', parseFacet(serialiseFacet([EMPTY_FACET])), [EMPTY_FACET]);
check('a literal ~none does not become the sentinel',
      parseFacet(serialiseFacet([EMPTY_FACET_PARAM]))[0] !== EMPTY_FACET,
      `parsed as ${JSON.stringify(parseFacet(serialiseFacet([EMPTY_FACET_PARAM]))[0])}`);

// Multi-select, including the sentinel alongside real values, and order preservation.
const MULTI = [
  ['nuc2', 'blitz01'],
  [EMPTY_FACET, 'nuc2'],
  ['a,b', 'c'],
  [EMPTY_FACET_PARAM, EMPTY_FACET],
];
for (const values of MULTI) {
  eq(`facet multi round trip ${JSON.stringify(values)}`, parseFacet(serialiseFacet(values)), values);
}

eq('empty selection serialises to nothing readable back', parseFacet(serialiseFacet([])), []);
eq('a null query parses to no selection', parseFacet(null), []);
eq('a malformed escape degrades rather than throwing', parseFacet('%'), ['%']);
for (const wire of ['n:2..8', 'd:last7d', 't:lxdbr']) {
  check(`${wire} is not a plausible enum value`, wire.includes(':'));
}

if (failures) {
  console.error(`\nfilter url codec: ${failures} of ${checks} checks failed`);
  rmSync(out, { recursive: true, force: true });
  process.exit(1);
}

rmSync(out, { recursive: true, force: true });
console.log(`filter url codec: ${checks} round-trip and predicate checks pass`);
