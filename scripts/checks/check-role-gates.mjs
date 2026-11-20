#!/usr/bin/env node
// Structural guard: a route the sidebar hides by role must also refuse to render it.
//
// The nav config is the single place that says which destinations belong to which role, and
// filterGroups drops the ones the operator cannot have. Hiding a link is not a gate: the route
// is still reachable by URL, bookmark or history, and a page without useRequireRole renders its
// shell and a wall of 403s instead of redirecting. Each page reads correctly on its own, so the
// discrepancy only shows across files.
//
// The backend is the real enforcement (require_partner on the routers, and the authorized()
// callback). This is the UI half: the two must not disagree about who may see a page.
//
// Run: node scripts/checks/check-role-gates.mjs [repo-root]

import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');
const navConfig = join(src, 'components', 'sidebar', 'nav-config.tsx');
const appDir = join(src, 'app');

// Roles that are a genuine restriction. "tenant" is the floor: hasMinimumRole(TENANT) is true
// for every authenticated operator, so a tenant-tagged item gates nothing and needs no hook.
const GATED = new Map([
  ['partner', 'Role.PARTNER'],
  ['admin', 'Role.ADMIN'],
]);

// Routes whose page is ungated by design, with the reason. Anything not listed must gate.
const UNGATED_BY_DESIGN = new Map();

// The edition's own groups use the same literal shape in edition-nav.ts.
const editionNav = join(src, 'components', 'sidebar', 'edition-nav.ts');
const source = [navConfig, editionNav].filter(existsSync).map((file) => readFileSync(file, 'utf8')).join('\n');

// A group literal opens at two-space indent and its `role:` covers every item inside it. An
// item may also carry its own. Reset at each group, or a gated group's role leaks onto the
// ungated group that follows it.
const required = new Map();
let groupRole = null;
for (const line of source.split('\n')) {
  if (/^ {2}\{\s*$/.test(line)) groupRole = null;
  const groupLevel = line.match(/^ {4}role:\s*"(\w+)"/);
  if (groupLevel) groupRole = groupLevel[1];

  const href = line.match(/href:\s*"([^"]+)"/);
  if (!href) continue;
  const itemLevel = line.match(/role:\s*"(\w+)"/);
  const role = itemLevel ? itemLevel[1] : groupRole;
  if (role && GATED.has(role)) required.set(href[1], role);
}

const offenders = [];

for (const [href, role] of required) {
  if (UNGATED_BY_DESIGN.has(href)) continue;
  const page = join(appDir, href.replace(/^\//, ''), 'page.tsx');
  if (!existsSync(page)) {
    offenders.push(`${href} is gated to ${role} in the nav but has no page.tsx`);
    continue;
  }
  const body = readFileSync(page, 'utf8');
  const expected = `useRequireRole(${GATED.get(role)})`;
  if (!body.includes(expected)) {
    offenders.push(`${href} is ${role}-only in the nav but its page never calls ${expected}`);
  }
}

// A scan over nothing passes for the wrong reason.
if (required.size < 7) {
  offenders.push(`only ${required.size} gated destinations found in nav-config.tsx; expected at least 7`);
}

// The server half: every page that gates itself in the browser is also refused by the
// authorized() callback in auth.ts, so a tenant deep link never mounts the page shell.
const authSource = readFileSync(join(src, 'auth.ts'), 'utf8');
// An edition's own pages are refused by the same callback through the lists in lib/edition.ts.
const editionSource = readFileSync(join(src, 'lib', 'edition.ts'), 'utf8');
const quotedEntries = (source, pattern) => {
  const block = source.match(pattern);
  // Comments go first: a quoted path inside a `//` note is not an entry.
  return block ? [...block[1].replace(/\/\/.*$/gm, '').matchAll(/"([^"]+)"/g)].map((m) => m[1]) : null;
};
const EDITION_LISTS = new Map([
  ['adminOrPartnerPaths', 'EDITION_ADMIN_OR_PARTNER_PATHS'],
  ['adminOnlyPaths', 'EDITION_ADMIN_ONLY_PATHS'],
]);
const serverPaths = (name) => {
  const own = quotedEntries(authSource, new RegExp(`const ${name} = \\[([\\s\\S]*?)\\];`));
  if (!own) return null;
  const edition = EDITION_LISTS.get(name);
  const extra = edition ? quotedEntries(editionSource, new RegExp(`const ${edition}: readonly string\\[\\] = \\[([\\s\\S]*?)\\];`)) : [];
  return [...own, ...(extra ?? [])];
};
const SERVER_LISTS = new Map([
  ['PARTNER', 'adminOrPartnerPaths'],
  ['ADMIN', 'adminOnlyPaths'],
]);
const covered = (route, paths) => paths.some((p) => route === p || route.startsWith(`${p}/`));

const pages = [];
const walk = (dir) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walk(full);
    else if (/^page\.(tsx|ts|jsx|js)$/.test(entry.name)) pages.push(full);
  }
};
walk(appDir);

let serverChecked = 0;
for (const page of pages) {
  const body = readFileSync(page, 'utf8');
  // Route groups such as (marketing) do not appear in the URL.
  const route = `/${relative(appDir, dirname(page)).split(sep).filter((s) => !/^\(.*\)$/.test(s)).join('/')}`;
  for (const [role, list] of SERVER_LISTS) {
    // Any argument list: useRequireRole(Role.PARTNER, "/") gates the page just the same. The gate
    // lives in the page file itself, as in inventory/identities/page.tsx.
    if (!new RegExp(`useRequireRole\\(\\s*Role\\.${role}\\b`).test(body)) continue;
    const paths = serverPaths(list);
    if (!paths) {
      offenders.push(`auth.ts has no ${list} list to refuse ${route} on the server`);
      continue;
    }
    serverChecked += 1;
    if (!covered(route, paths)) {
      offenders.push(`${route} calls useRequireRole(Role.${role}) but auth.ts ${list} does not refuse it on the server`);
    }
  }
}
// Fewer than this means pages fell out of the scan.
if (serverChecked < 15) {
  offenders.push(`only ${serverChecked} role-gated pages found under app/; expected at least 15`);
}

if (offenders.length) {
  console.error('The nav hides these routes by role but the routes do not refuse them:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nGate the page as inventory/warnings/page.tsx does: call useRequireRole at the');
  console.error('top level, render LoadingLottiePage until isResolved, then null when not allowed.');
  console.error('Refuse it on the server too: add its route to the matching list in frontend/src/auth.ts.');
  process.exit(1);
}
console.log(
  `role gates: ${required.size} role-restricted destinations, every page refuses its own route; ` +
    `${serverChecked} gated pages also refused on the server`,
);
