#!/usr/bin/env node
// One date format, and every call site on it.
//
// Five formats grew here: 30/10/2026, 22:55:00 beside Oct 30, 2026, 10:55 PM beside 30 Oct 2026 and
// the browser's own locale, sometimes two of them in one row. The zone label makes one mistake
// worse than it was: an API timestamp with no offset is UTC, and read as local it would now print a
// wrong time with a confident label on it.
//
// This runs the real helpers under each zone an operator sits in, then holds the tree to them.
//
// Run: node scripts/checks/check-date-format.mjs [repo-root]

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

const EXPECTED = {
  'Asia/Jakarta': { full: 'Oct 30, 2026, 10:55 PM WIB', seconds: 'Oct 30, 2026, 10:55:04 PM WIB' },
  'Asia/Pontianak': { full: 'Oct 30, 2026, 10:55 PM WIB', seconds: 'Oct 30, 2026, 10:55:04 PM WIB' },
  'Asia/Makassar': { full: 'Oct 30, 2026, 11:55 PM WITA', seconds: 'Oct 30, 2026, 11:55:04 PM WITA' },
  'Asia/Jayapura': { full: 'Oct 31, 2026, 12:55 AM WIT', seconds: 'Oct 31, 2026, 12:55:04 AM WIT' },
  UTC: { full: 'Oct 30, 2026, 3:55 PM UTC', seconds: 'Oct 30, 2026, 3:55:04 PM UTC' },
  'America/New_York': { full: 'Oct 30, 2026, 11:55 AM EDT', seconds: 'Oct 30, 2026, 11:55:04 AM EDT' },
};

if (process.env.DATE_CHECK_ZONE) {
  // One zone per process: Node reads TZ once.
  const requireFromFrontend = createRequire(resolve(root, 'frontend/package.json'));
  const ts = requireFromFrontend('typescript');
  const path = join(src, 'lib', 'utils.ts');
  const output = ts.transpileModule(readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: path,
  }).outputText;
  const loaded = { exports: {} };
  new Function('require', 'module', 'exports', output)(requireFromFrontend, loaded, loaded.exports);
  const { formatDate, formatDateTime, formatDateTimeSeconds, formatClockTime } = loaded.exports;

  const zone = process.env.DATE_CHECK_ZONE;
  const want = EXPECTED[zone];
  const instant = '2026-10-30T15:55:04Z';
  assert.equal(formatDateTime(instant), want.full, zone);
  assert.equal(formatDateTimeSeconds(instant), want.seconds, zone);
  assert.equal(formatDateTime('2026-10-30T15:55:04+00:00'), want.full, `${zone}: an offset is honoured`);
  assert.equal(formatDateTime('2026-10-30T15:55:04'), want.full, `${zone}: no offset means UTC`);
  assert.equal(formatDateTime('2026-10-30T15:55:04.123456'), want.full, `${zone}: microseconds, no offset`);
  assert.equal(formatDate('2026-10-30'), 'Oct 30, 2026', `${zone}: a date alone does not cross midnight`);
  assert.equal(formatDate('2026-01-01'), 'Jan 1, 2026', `${zone}: a date alone does not cross midnight`);
  for (const value of [null, undefined, '', 'not a date']) {
    assert.equal(formatDate(value), '—');
    assert.equal(formatDateTime(value), '—');
    assert.equal(formatDateTimeSeconds(value), '—');
  }
  assert.match(formatClockTime(Date.parse(instant)), /^\d{1,2}:55:04 [AP]M$/);
  assert.equal(formatClockTime(null), '—');
  process.exit(0);
}

for (const zone of Object.keys(EXPECTED)) {
  execFileSync(process.execPath, [fileURLToPath(import.meta.url), root], {
    env: { ...process.env, TZ: zone, DATE_CHECK_ZONE: zone },
    stdio: 'inherit',
  });
}

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.tsx?$/.test(full)) yield full;
  }
}

const offenders = [];
const LOCAL_DATE = /\.toLocale(?:Date|Time)String\(|\bnew Date\([^)]*\)\.toLocaleString\(|Intl\.DateTimeFormat/;
for (const file of walk(src)) {
  const rel = relative(src, file).split(sep).join('/');
  if (rel === 'lib/utils.ts') continue;
  readFileSync(file, 'utf8').split('\n').forEach((text, index) => {
    if (/^\s*(\/\/|\*)/.test(text)) return;
    if (LOCAL_DATE.test(text)) offenders.push(`frontend/src/${rel}:${index + 1} formats a date by hand`);
  });
}

if (offenders.length) {
  console.error('A date is formatted outside lib/utils.ts:');
  for (const offender of offenders) console.error(`  ${offender}`);
  process.exit(1);
}
console.log(`date format: one format in ${Object.keys(EXPECTED).length} zones, every call site on it`);
