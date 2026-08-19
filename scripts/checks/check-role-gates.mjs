#!/usr/bin/env node
// Structural guard: a route the sidebar hides by role must also refuse to render it.
//
// The nav config is the single place that says which destinations belong to which role, and
// filterGroups drops the ones the operator cannot have. Hiding a link is not a gate: the route
// is still reachable by typing the URL, by a bookmark, or by browser history. Five of the seven
// partner destinations called useRequireRole and two did not, so a tenant deep-linking to
// /inventory/identities got the page shell and a wall of 403s instead of a redirect. Nothing
// caught it, because each page reads correctly on its own and the discrepancy only exists
// between them.
//
// The backend is the real enforcement (require_partner on the routers, and the authorized()
// callback). This is the UI half: the two must not disagree about who may see a page.
//
// Run: node scripts/checks/check-role-gates.mjs [repo-root]

import { readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
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

// Routes whose page is deliberately ungated, with the reason. Anything not listed must gate.
const UNGATED_BY_DESIGN = new Map();

const source = readFileSync(navConfig, 'utf8');

// A group literal opens at two-space indent and its `role:` covers every item inside it. An
// item may also carry its own. Reset at each group, or a gated group leaks onto the ungated one
// that follows it, which reads as a much stricter product than the one that ships.
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

if (offenders.length) {
  console.error('The nav hides these routes by role but the routes do not refuse them:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nGate the page as inventory/warnings/page.tsx does: call useRequireRole at the');
  console.error('top level, render LoadingLottiePage until isResolved, then null when not allowed.');
  process.exit(1);
}
console.log(`role gates: ${required.size} role-restricted destinations, every page refuses its own route`);
