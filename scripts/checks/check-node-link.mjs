#!/usr/bin/env node
// Structural guard: every Node or Cluster cell renders components/ui/node-link.tsx, which owns
// the role-gated link into Clusters, and that component is the only cell-level caller of
// inventoryLinks.clusters.
//
// Per feature directory (a trailing /components folded into its route), the declared Node and
// Cluster columns minus those marked `node-link: exempt <reason>` must not exceed the `<NodeLink`
// count. `inventoryLinks.clusters(` appears only in the component and the allow-list below, and
// the component must read both the role and the registry. Fewer than MIN_DECLARED columns, or no
// files walked, fails, so a reformatted tree cannot pass unseen. Regex over comment-stripped
// source: a NodeLink in the wrong column of the right directory still passes the count;
// frontend/e2e/node-link-cells.spec.ts covers that.
//
// Run: node scripts/checks/check-node-link.mjs [repo-root]

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');
const COMPONENT = 'components/ui/node-link.tsx';
const MIN_DECLARED = 26;

// Page-level Clusters links that are not table cells, each with a reason.
const ALLOWED_CLUSTERS_CALLERS = new Map([
  [
    'app/inventory/warnings/components/warning-detail-dialog.tsx',
    'footer button of a Partner-gated dialog, a Button rather than a cell',
  ],
]);

const HEADER_LABEL = '(?:Node|Cluster|Clusters)';
const ROLE_NODE_RE = /facetSpec:\s*\{[^}]*\brole:\s*"node"/g;
const SORTABLE_HEAD_RE = new RegExp(`<SortableTableHead\\b[^>]*\\blabel="${HEADER_LABEL}"`, 'g');
const TABLE_HEAD_RE = new RegExp(`<TableHead\\b[^>]*>\\s*${HEADER_LABEL}\\s*</TableHead>`, 'g');
const NODE_LINK_RE = /<NodeLink\b/g;
const CLUSTERS_CALL_RE = /inventoryLinks\.clusters\(/g;
// The reason must not run into the comment terminator: `exempt */} <TableHead ...` is not one.
const EXEMPT_RE = /node-link:\s*exempt\s+((?:(?!\*\/)[^\n]){15,})/g;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(tsx|jsx)$/.test(entry)) yield full;
  }
}

/** Source with comments removed, so text inside a comment cannot trip the guard. */
function stripComments(code) {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

/** Feature directory of a file: its route, with components/ folded in. */
function groupOf(rel) {
  let dir = dirname(rel);
  if (dir.endsWith('/components')) dir = dirname(dir);
  return dir;
}

const failures = [];
const groups = new Map();
let walked = 0;
let declaredTotal = 0;
let usedTotal = 0;
let exemptTotal = 0;

for (const file of walk(src)) {
  const rel = relative(src, file);
  walked += 1;
  const raw = readFileSync(file, 'utf8');
  const code = stripComments(raw);

  if (rel !== COMPONENT) {
    const calls = code.match(CLUSTERS_CALL_RE)?.length ?? 0;
    if (calls > 0 && !ALLOWED_CLUSTERS_CALLERS.has(rel)) {
      failures.push(`${rel}: calls inventoryLinks.clusters ${calls}x outside NodeLink, which bypasses the role gate`);
    }
  }

  const roleColumns = code.match(ROLE_NODE_RE)?.length ?? 0;
  const headers = (code.match(SORTABLE_HEAD_RE)?.length ?? 0) + (code.match(TABLE_HEAD_RE)?.length ?? 0);
  const exempt = [...raw.matchAll(EXEMPT_RE)].length;
  const used = code.match(NODE_LINK_RE)?.length ?? 0;

  if (roleColumns === 0 && headers === 0 && used === 0 && exempt === 0) continue;
  const key = groupOf(rel);
  const group = groups.get(key) ?? { roles: 0, headers: 0, used: 0, exempt: 0, files: [] };
  group.roles += roleColumns;
  group.headers += headers;
  group.used += used;
  group.exempt += exempt;
  group.files.push(`${rel} (${roleColumns} role, ${headers} header, ${used} NodeLink, ${exempt} exempt)`);
  groups.set(key, group);
}

for (const [key, group] of groups) {
  // A column is declared in defineTable and again as a header, possibly in different files of the
  // same directory. Taking the larger sum counts a split table once and misses neither kind.
  group.declared = Math.max(0, Math.max(group.roles, group.headers) - group.exempt);
  declaredTotal += group.declared;
  usedTotal += group.used;
  exemptTotal += group.exempt;
  if (group.declared > group.used) {
    failures.push(
      `${key}: ${group.declared} Node/Cluster column(s) but ${group.used} NodeLink cell(s)\n` +
        group.files.map((f) => `      ${f}`).join('\n'),
    );
  }
}

const componentPath = join(src, COMPONENT);
if (!existsSync(componentPath)) {
  failures.push(`${COMPONENT} is missing`);
} else {
  const component = readFileSync(componentPath, 'utf8');
  if (!component.includes('usePermissions(')) failures.push(`${COMPONENT} no longer reads the role`);
  if (!component.includes('inventoryLinks.clusters(')) failures.push(`${COMPONENT} no longer builds through lib/inventory-links.ts`);
}

if (walked === 0) failures.push('no frontend sources walked');
if (declaredTotal + exemptTotal < MIN_DECLARED) {
  failures.push(`only ${declaredTotal + exemptTotal} Node/Cluster columns found; expected at least ${MIN_DECLARED}`);
}

if (failures.length) {
  console.error('check-node-link: FAIL');
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(
  `node links: ${groups.size} feature directories, ${declaredTotal} Node/Cluster columns, ${usedTotal} NodeLink cells, ${exemptTotal} exempt over ${walked} files`,
);
