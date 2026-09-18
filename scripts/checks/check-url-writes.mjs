#!/usr/bin/env node
// Structural guard: a same-path URL write from an interaction is a history write, not a
// navigation.
//
// Filters, tabs and pickers keep their state in the query string. Written through
// `router.replace(url, { scroll: false })` that is a navigation, and on Next 15.5 a same-path,
// params-only navigation can take the aliased-prefetch path (a param-less prefetch entry for the
// page alive, a loading.tsx on the segment), which never sets shouldScroll and lands the
// refetched segment with the document scrolled to the top under the open popover. It also costs
// an RSC round trip per keystroke. lib/url-state.ts wraps window.history.replaceState, which Next
// patches to update usePathname and useSearchParams with no fetch and no scroll of its own.
//
// The line this holds:
// - the filter engine writes only through lib/url-state;
// - window.history.replaceState and pushState are called only inside lib/url-state;
// - a same-path router.replace elsewhere is either a mount-time writer (the history patch is
//   installed after hydration, so an effect in the hydration commit must stay on the router) named
//   below with its reason, or a defect.
//
// Run: node scripts/checks/check-url-writes.mjs [repo-root]

import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

const MODULE = join('lib', 'url-state.ts');
const ENGINE = join('hooks', 'use-table-filters.ts');
const IMPORT = /from\s+["']@\/lib\/url-state["']/;

// Effect-time writers that must stay a navigation, with the number of writes each may hold.
// A page effect in the hydration commit runs before Next's history patch exists, so a
// replaceState there would leave the router's own URL stale.
const ALLOWED_ROUTER_WRITES = new Map([
  [join('app', 'inventory', 'instances', 'page.tsx'), { max: 1, why: 'legacy ?instance= translated once on mount' }],
  [join('app', 'inventory', 'warnings', 'page.tsx'), { max: 1, why: 'legacy ?severity= and ?node= translated once on mount' }],
  [join('app', 'inventory', 'clustering', 'members', 'page.tsx'), { max: 1, why: 'the default cluster picked from data on load' }],
]);

// What makes a router.replace a same-path write: a query-only target, the current pathname
// re-used, or the scroll:false a non-navigating write asks for.
const SAME_PATH = /(["'`]\?|\$\{pathname\}\?|\?\$\{|scroll:\s*false)/;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(tsx?|jsx?)$/.test(entry)) yield full;
  }
}

// Comments describe the writes they replaced, so they are not calls.
function stripComments(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .map((line) => line.replace(/(^|\s)\/\/.*$/, '$1'))
    .join('\n');
}

// The call text from `router.replace(` to its balanced close, so a target split over lines is
// still judged whole.
function callText(code, start) {
  let depth = 0;
  for (let i = start; i < code.length; i += 1) {
    if (code[i] === '(') depth += 1;
    else if (code[i] === ')') {
      depth -= 1;
      if (depth === 0) return code.slice(start, i + 1);
    }
  }
  return code.slice(start, start + 400);
}

const offenders = [];
let scanned = 0;
let importers = 0;
let routerFiles = 0;
const seenAllowed = new Map();

if (!existsSync(join(src, MODULE))) {
  offenders.push(`${MODULE} is missing`);
} else {
  const mod = readFileSync(join(src, MODULE), 'utf8');
  for (const name of ['replaceUrl', 'replaceQuery', 'currentQuery']) {
    if (!new RegExp(`export function ${name}\\(`).test(mod)) offenders.push(`${MODULE} no longer exports ${name}`);
  }
  if (!/window\.history\.replaceState\(/.test(mod)) offenders.push(`${MODULE} no longer writes through window.history.replaceState`);
}

for (const file of walk(src)) {
  scanned += 1;
  const rel = relative(src, file);
  const code = stripComments(readFileSync(file, 'utf8'));
  if (IMPORT.test(code)) importers += 1;
  if (/\buseRouter\(/.test(code)) routerFiles += 1;

  if (rel !== MODULE && /\bhistory\s*\.\s*(replaceState|pushState)\s*\(/.test(code)) {
    offenders.push(`${rel} calls history.replaceState/pushState directly; use lib/url-state`);
  }

  if (rel === ENGINE) {
    if (!IMPORT.test(code)) offenders.push(`${rel} does not import @/lib/url-state`);
    if (!/\breplaceQuery\(/.test(code)) offenders.push(`${rel} does not write through replaceQuery`);
    if (/\buseRouter\(|router\.replace\(|router\.push\(/.test(code)) offenders.push(`${rel} navigates on a filter write`);
    continue;
  }

  const re = /router\s*\.\s*replace\s*\(/g;
  let m;
  while ((m = re.exec(code)) !== null) {
    const text = callText(code, m.index + m[0].length - 1);
    if (!SAME_PATH.test(text)) continue;
    const line = code.slice(0, m.index).split('\n').length;
    const allowed = ALLOWED_ROUTER_WRITES.get(rel);
    if (!allowed) {
      offenders.push(`${rel}:${line} same-path router.replace; write through lib/url-state or name it here with its reason`);
      continue;
    }
    const n = (seenAllowed.get(rel) ?? 0) + 1;
    seenAllowed.set(rel, n);
    if (n > allowed.max) offenders.push(`${rel}:${line} holds more same-path router writes than allowed (${allowed.max}: ${allowed.why})`);
  }
}

for (const [rel, { why }] of ALLOWED_ROUTER_WRITES) {
  if (!seenAllowed.has(rel)) offenders.push(`${rel} is allowed a router write (${why}) but has none; drop it from the list`);
}

// A scan over nothing passes for the wrong reason.
if (scanned < 100) offenders.push(`only ${scanned} files scanned under frontend/src`);
if (importers < 5) offenders.push(`only ${importers} files import @/lib/url-state; the engine and the four moved sites make five`);
if (routerFiles < 6) offenders.push(`only ${routerFiles} files call useRouter(); the scan is not seeing the tree`);

if (offenders.length) {
  console.error('A same-path URL write must not navigate:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nlib/url-state.ts says why, and which writers must stay on the router.');
  process.exit(1);
}
console.log(`url writes: ${scanned} files, ${importers} import lib/url-state, ${seenAllowed.size} effect-time router writes allowed by name`);
