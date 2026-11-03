#!/usr/bin/env node
// Structural guard: a component reading useSearchParams needs a Suspense boundary above it.
//
// useSearchParams opts a client component out of prerendering. Next then requires a boundary,
// and without one `next build` fails on that route. Typecheck does not see it, so the failure
// currently surfaces in CI at the Build step, minutes after a green local run.
//
// This is the same failure, caught in `make test-frontend` instead. Cheap signal, same rule.
//
// Run: node scripts/checks/check-searchparams-suspense.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

// Hooks that read the query string and therefore carry the same prerender bailout. Listed by
// name rather than by import path so a re-export cannot slip past.
const READERS = [/\buseSearchParams\s*\(/, /\buseTableFilters\s*\(/, /\buseSettingsSection\s*\(/];
// note: both spellings are legitimate and both appear in this tree. A guard that recognises
// only the one its author had in mind is the blind spot table-conventions.md warns about.
const BOUNDARY = /<(React\.)?Suspense[\s>]/;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(tsx|jsx)$/.test(entry)) yield full;
  }
}

const offenders = [];
let readers = 0;

for (const file of walk(src)) {
  const code = readFileSync(file, 'utf8');

  // The definition of a wrapper hook is not a call site. Only pages and components that
  // invoke one need the boundary.
  const isDefinition = /export\s+function\s+useTableFilters\b/.test(code);
  if (isDefinition) continue;

  const reads = READERS.some((pattern) => pattern.test(code));
  if (!reads) continue;
  readers += 1;

  if (!BOUNDARY.test(code)) {
    const line = code.split('\n').findIndex((l) => READERS.some((p) => p.test(l))) + 1;
    offenders.push(`${relative(src, file)}:${line} reads the query string with no <Suspense> in the file`);
  }
}

// A scan over nothing passes for the wrong reason.
if (readers < 5) {
  offenders.push(`only ${readers} query-string readers found, expected the scan to reach more`);
}

if (offenders.length) {
  console.error('useSearchParams without a Suspense boundary fails `next build`, not typecheck:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nSplit the page into a Content component and a default export that wraps it:');
  console.error('  export default function Page() {');
  console.error('    return <Suspense fallback={<LoadingLottiePage />}><PageContent /></Suspense>;');
  console.error('  }');
  console.error('\napp/inventory/networks/page.tsx and app/inventory/warnings/page.tsx show the shape.');
  process.exit(1);
}
console.log(`searchparams suspense: ${readers} query-string readers, every one has a boundary`);
