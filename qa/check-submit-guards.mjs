#!/usr/bin/env node
// Structural guard: every form that fires a mutation must claim a synchronous in-flight flag.
//
// The disabled prop on a submit button is driven by React state (isPending, isSubmitting,
// submitting). State only reaches the DOM on the next render, so two clicks inside one tick both
// pass the check and both fire. react-hook-form does not close this: handleSubmit has no
// re-entry guard and awaits its resolver before calling the handler, which widens the window.
// TanStack Query's mutate has no guard either.
//
// This was not theoretical. instance-create-form provisioned two instances from one double
// click, and network-create-form was caught by a Playwright assertion doing the same. Six forms
// now carry the guard; this stops the seventh shipping without one.
//
// Run: node qa/check-submit-guards.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

// Defaults to the repo this file lives in, so CI needs no argument and a local run needs no
// absolute path.
const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..');
const srcRoot = join(root, 'frontend', 'src');

// Forms whose submit path is guarded somewhere other than the component, with the reason.
const EXEMPT = new Map([
  [
    'components/break-glass/break-glass-modal.tsx',
    'the submit lives in lib/break-glass-store.ts, which guards synchronously with get().submitting',
  ],
]);

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (entry.endsWith('.tsx')) out.push(full);
  }
  return out;
}

const MUTATION = /\.(mutateAsync|mutate)\s*\(/;
const HANDLER = /const\s+(submit|onSubmit|handleSubmit|handleCreate|handleSave)\s*=\s*async\s*\(/;
const GUARD = /(submitInFlightRef\.current\)\s*return|InFlightRef\.current\s*=\s*true)/;

const offenders = [];
for (const file of walk(srcRoot)) {
  const src = readFileSync(file, 'utf8');
  if (!HANDLER.test(src) || !MUTATION.test(src)) continue;
  const rel = relative(join(root, 'frontend', 'src'), file).split('\\').join('/');
  if (EXEMPT.has(rel)) continue;
  if (!GUARD.test(src)) offenders.push(rel);

  // A claim with no release locks the form after one submit, which is worse than the bug.
  const claims = (src.match(/InFlightRef\.current = true/g) ?? []).length;
  const releases = (src.match(/InFlightRef\.current = false/g) ?? []).length;
  if (claims > 0 && releases === 0) offenders.push(`${rel} (claims the guard but never releases it)`);
}

if (offenders.length) {
  console.error('Forms fire a mutation without a synchronous double-submit guard:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nCopy the pattern in components/network-create-form.tsx: a useRef claimed as the');
  console.error('first statement of the handler, before any await, and released in a finally.');
  process.exit(1);
}
console.log('submit guards: all mutation forms are guarded');
