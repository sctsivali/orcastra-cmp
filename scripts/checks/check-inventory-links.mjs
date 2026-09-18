#!/usr/bin/env node
// Structural guard: every link into a filtered list is built by lib/inventory-links.ts, and
// every key that registry writes is one the target page reads.
//
// The defect behind it: a filter lives in the URL as `<table id>.<field>=<facet value>`, and a
// link hand-written at the source page spelled the key for the wrong table, carried the display
// name where the accessor compares the id, or encoded the value differently from the filter
// engine. Each shipped at least once, and each read as "No X matches the current filters" on a
// list that plainly had the rows once the chip was removed.
//
// Four rules:
//   A. no `/inventory/<x>?...` or `/inventory/<x>/${...}` string literal outside the registry
//      (a `/create` prefill is a form seed, not a list filter, and is exempt);
//   B. no `?q=` / `&q=` literal outside the registry, `q` being the search key;
//   C. each manifest key resolves on the target route's sources: a namespaced facet needs a
//      defineTable with that `id` carrying that `field`, a bare facet needs a table without an
//      id carrying it, a reserved key needs `reservedParams` or a `searchParams.get`;
//   D. a scan that parsed fewer than ten targets or walked no files fails, so a reformatted
//      manifest cannot pass by being invisible.
//
// Run: node scripts/checks/check-inventory-links.mjs [repo-root]

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');
const REGISTRY = 'lib/inventory-links.ts';
const MIN_TARGETS = 10;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(tsx?|jsx?)$/.test(entry)) yield full;
  }
}

/** Source with comments removed, so a rule in a comment cannot trip the guard. */
function stripComments(code) {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, '')
    // A `//` that follows a colon is a URL inside a string, not a comment.
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

const failures = [];
let walked = 0;

// ── A and B: hand-built hrefs ───────────────────────────────────────────────────────────────
const HREF_RE = /["'`]\/inventory\/[a-z-]+(?:\/[^"'`\s?]*)?(?:\?|\/\$\{)/g;
const SEARCH_RE = /["'`][^"'`\n]*[?&]q=/g;

for (const file of walk(src)) {
  const rel = relative(src, file);
  if (rel === REGISTRY) continue;
  walked += 1;
  const code = stripComments(readFileSync(file, 'utf8'));
  for (const match of code.matchAll(HREF_RE)) {
    if (/\/create(\?|\/\$\{)$/.test(match[0])) continue;
    failures.push(`${rel}: hand-built inventory href ${match[0]} (use lib/inventory-links.ts)`);
  }
  for (const match of code.matchAll(SEARCH_RE)) {
    failures.push(`${rel}: writes the search key by hand ${match[0].trim()} (use lib/inventory-links.ts)`);
  }
}

// ── C: the manifest resolves on its targets ─────────────────────────────────────────────────
const registrySource = readFileSync(join(src, REGISTRY), 'utf8');
const manifestStart = registrySource.indexOf('export const LINK_TARGETS = {');
const manifestEnd = registrySource.indexOf('} as const;', manifestStart);
if (manifestStart === -1 || manifestEnd === -1) {
  failures.push(`${REGISTRY}: LINK_TARGETS block not found`);
}
const manifest = registrySource.slice(manifestStart, manifestEnd);
const ENTRY_RE =
  /^\s*(\w+):\s*\{\s*path:\s*"(\/inventory\/[^"]+)",\s*search:\s*(true|false),\s*facets:\s*\[([^\]]*)\](?:,\s*reserved:\s*\[([^\]]*)\])?\s*\},?$/gm;
const declared = (manifest.match(/path:/g) ?? []).length;
const targets = [];
for (const match of manifest.matchAll(ENTRY_RE)) {
  const list = (raw) => raw.split(',').map((s) => s.trim().replace(/^"|"$/g, '')).filter(Boolean);
  targets.push({
    name: match[1],
    path: match[2],
    search: match[3] === 'true',
    facets: list(match[4]),
    reserved: list(match[5] ?? ''),
  });
}
if (targets.length !== declared) {
  failures.push(
    `${REGISTRY}: ${declared} targets declared but ${targets.length} parsed; keep one entry per line`,
  );
}

/** The route directory's own sources plus its components/, not a nested route's. */
function routeSources(routeDir) {
  const files = [];
  const take = (dir) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) {
        if (entry === 'components') take(full);
        continue;
      }
      if (/\.(tsx?)$/.test(entry)) files.push(full);
    }
  };
  take(routeDir);
  return files;
}

function readTables(code) {
  const tables = [];
  let from = 0;
  for (;;) {
    const start = code.indexOf('defineTable<', from);
    if (start === -1) break;
    const columnsAt = code.indexOf('columns: [', start);
    if (columnsAt === -1) break;
    let depth = 0;
    let end = -1;
    for (let i = code.indexOf('[', columnsAt); i < code.length; i++) {
      if (code[i] === '[') depth++;
      else if (code[i] === ']') {
        depth--;
        if (depth === 0) { end = i; break; }
      }
    }
    if (end === -1) break;
    from = end;
    const head = code.slice(start, columnsAt);
    const block = code.slice(columnsAt, end);
    tables.push({
      id: (head.match(/\bid:\s*"([^"]+)"/) ?? [])[1] ?? null,
      fields: new Set([...block.matchAll(/field:\s*"([^"]+)"/g)].map((m) => m[1])),
      reserved: new Set(
        ((head.match(/reservedParams:\s*\[([^\]]*)\]/) ?? [])[1] ?? '')
          .split(',').map((s) => s.trim().replace(/^"|"$/g, '')).filter(Boolean),
      ),
    });
  }
  return tables;
}

let keysVerified = 0;
for (const target of targets) {
  const routeDir = join(src, 'app', target.path);
  if (!existsSync(join(routeDir, 'page.tsx'))) {
    failures.push(`${REGISTRY}: ${target.name} points at ${target.path} but app${target.path}/page.tsx does not exist`);
    continue;
  }
  const code = routeSources(routeDir).map((f) => stripComments(readFileSync(f, 'utf8'))).join('\n');
  const tables = readTables(code);
  const reads = new Set([...code.matchAll(/searchParams\.get\("([^"]+)"\)/g)].map((m) => m[1]));
  const bareTables = tables.filter((t) => t.id === null);

  if (target.search && bareTables.length === 0) {
    failures.push(`${REGISTRY}: ${target.name} says search:true but ${target.path} has no un-namespaced table for a bare ?q=`);
  }
  for (const key of target.facets) {
    const dot = key.indexOf('.');
    const ns = dot === -1 ? null : key.slice(0, dot);
    const field = dot === -1 ? key : key.slice(dot + 1);
    const ok = tables.some((t) => t.id === ns && t.fields.has(field));
    if (ok) keysVerified += 1;
    else failures.push(`${REGISTRY}: ${target.name} writes ${key} but no table on ${target.path} ${ns ? `with id "${ns}" ` : 'without an id '}declares field "${field}"`);
  }
  for (const key of target.reserved) {
    const ok = key === 'tab' || reads.has(key) || tables.some((t) => t.reserved.has(key));
    if (ok) keysVerified += 1;
    else failures.push(`${REGISTRY}: ${target.name} writes reserved ${key} but ${target.path} neither reserves nor reads it`);
  }
}

// ── D: the scan saw something ───────────────────────────────────────────────────────────────
if (walked === 0) failures.push('no frontend sources walked');
if (targets.length < MIN_TARGETS) failures.push(`only ${targets.length} link targets parsed; expected at least ${MIN_TARGETS}`);

if (failures.length) {
  console.error('check-inventory-links: FAIL');
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`inventory links: ${targets.length} targets, ${keysVerified} keys verified, 0 hand-built hrefs over ${walked} files`);
