#!/usr/bin/env node
// Structural guard: a poll interval's jitter is resolved once per hook instance.
//
// jitter() returns a different value on every call. Called in a render body, it hands TanStack
// Query a new refetchInterval on every render, and the observer restarts its interval timer
// whenever the interval changes, so a page that re-renders more often than the interval never
// polls. The only safe call site is a lazy useState initialiser.
//
// Run: node scripts/checks/check-poll-jitter.mjs [repo-root]

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = join(root, 'frontend', 'src');

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

function stripComments(text) {
  // Block comments keep their newlines so reported line numbers match the file.
  return text
    .replace(/\/\*[\s\S]*?\*\//g, (block) => block.replace(/[^\n]/g, ''))
    .replace(/(^|[^:])\/\/.*$/gm, '$1');
}

const DEFINITION = /export\s+function\s+jitter\s*\(/;
const SAFE_CALL = /useState\(\s*\(\)\s*=>\s*jitter\(/;

const failures = [];
let calls = 0;
for (const file of walk(srcDir)) {
  const lines = stripComments(readFileSync(file, 'utf8')).split('\n');
  lines.forEach((line, index) => {
    if (!/\bjitter\(/.test(line) || DEFINITION.test(line)) return;
    calls += 1;
    if (!SAFE_CALL.test(line)) {
      failures.push(`${relative(root, file)}:${index + 1}: ${line.trim()}`);
    }
  });
}

console.log(`poll jitter: ${calls} call sites checked`);
if (failures.length) {
  console.error('\npoll jitter: FAILED. Resolve jitter in a lazy useState initialiser:');
  for (const f of failures) console.error(`  ${f}`);
  process.exit(1);
}
console.log('poll jitter: every interval is resolved once per hook instance');
