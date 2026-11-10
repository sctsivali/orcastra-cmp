#!/usr/bin/env node
// Structural guard: the unreachable-nodes banner names a host only to a role that may see hosts.
//
// Cluster names in UnreachableNodesBanner are a diagnosis for an operator and a disclosure for a
// tenant, who holds no cluster:read and sees node names nowhere else; /technologies also passes
// per-node `reasons` (raw connection errors with hosts, ports and TLS detail).
// check-unreachable-banner.mjs only checks the tag is present and check-role-gates.mjs never opens
// components/ui. Four rules, no baseline: (1) one role gate that paints nothing before the session
// resolves; (2) the ungated renderer takes exactly { noun, className }; (3) the gate agrees with
// ROLE_PERMISSIONS (Partner holds cluster:read, Tenant does not, no role between them); (4) no
// call site invents a second channel for a hostname.
//
// Run: node scripts/checks/check-banner-role-gate.mjs [repo-root]

import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { closing, openingTags } from './source-scan.mjs';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');
const BANNER = join(src, 'components', 'ui', 'unreachable-nodes-banner.tsx');
const RBAC = join(src, 'types', 'rbac.ts');

for (const path of [BANNER, RBAC]) {
  if (!existsSync(path)) {
    console.error(`banner role gate: ${relative(root, path)} does not exist, so this asserts nothing`);
    process.exit(1);
  }
}

const code = readFileSync(BANNER, 'utf8');
const rbac = readFileSync(RBAC, 'utf8');
const offenders = [];

// The file explains its own gate in prose, so counting raw occurrences counts the explanation.
// Strip comments first: what is asserted is what executes.
const executable = code
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '');

// --- 1. the gate -----------------------------------------------------------------------------

if (!/^"use client";$/m.test(code.split('\n').slice(0, 2).join('\n'))) {
  offenders.push('the banner calls a session hook but does not open with "use client"');
}

// important: match the destructuring of usePermissions() itself. A comment or an import that
// mentions "usePermissions()" would pass a substring check while `hasRole` comes from somewhere
// else. The two names the rules below depend on must come out of the call.
const bind = executable.match(/const\s*\{([^}]*)\}\s*=\s*usePermissions\(\)\s*;/);
if (!bind) {
  offenders.push(
    'the banner does not destructure usePermissions(), so whatever the gate reads, it is not ' +
      'the session role',
  );
} else {
  const bound = bind[1].split(',').map((n) => n.trim()).filter(Boolean);
  for (const name of ['isLoading', 'hasRole']) {
    if (!bound.includes(name)) {
      offenders.push(`${name} is not bound from usePermissions(), so the rule below asserts nothing`);
    }
  }
}

const gates = executable.match(/hasRole\(Role\.PARTNER\)/g) ?? [];
if (gates.length !== 1) {
  offenders.push(
    `expected exactly one hasRole(Role.PARTNER) gate, found ${gates.length}. One gate, in the ` +
      'exported component, is the whole design.',
  );
}

// The gate's direction, and what each side renders. Counting the gate passes with its branches
// swapped, which hands every tenant the named list.
const ungated = /if \(!hasRole\(Role\.PARTNER\)\) \{\s*return <UnreachableNodesNotice noun=\{noun\} className=\{className\} \/>;\s*\}/
  .exec(executable);
if (!ungated) {
  offenders.push(
    'the banner no longer reads `if (!hasRole(Role.PARTNER)) return <UnreachableNodesNotice ' +
      'noun={noun} className={className} />`. The role below Partner gets that line and only that.',
  );
}
for (const name of ['UnreachableNodesNotice', 'UnreachableNodesDetail']) {
  const renders = executable.match(new RegExp(`<${name}\\b`, 'g')) ?? [];
  if (renders.length !== 1) {
    offenders.push(`${name} renders ${renders.length} times. One place on each side of the gate is the design.`);
  }
}
if (ungated && executable.indexOf('<UnreachableNodesDetail') < ungated.index) {
  offenders.push('UnreachableNodesDetail renders above the role gate');
}

// With no session resolveRoles([]) answers [Role.TENANT], so painting during the first read would
// show the tenant line to a partner for a frame. The banner renders nothing while loading.
if (!/if\s*\(isLoading\)\s*return null;/.test(executable)) {
  offenders.push(
    'the banner does not `if (isLoading) return null;`. usePermissions resolves roles to ' +
      '[Role.TENANT] when there is no session, so anything else flashes the wrong copy.',
  );
}

// --- 2. the ungated renderer cannot be handed a name -----------------------------------------
//
// Pinned by signature: a new prop such as `nodes` needs an edit to that line and to this guard.
const notice = executable.match(/function UnreachableNodesNotice\(\s*\{[^}]*\}\s*:\s*\{([^}]*)\}\s*\)/);
if (!notice) {
  offenders.push(
    'UnreachableNodesNotice is not declared with a literal inline prop type, so its surface ' +
      'cannot be read from here. Keep the signature literal.',
  );
} else {
  const props = notice[1]
    .split(';')
    .map((p) => p.trim().split(':')[0].replace('?', '').trim())
    .filter(Boolean)
    .sort();
  const allowed = ['className', 'noun'];
  if (props.join(',') !== allowed.join(',')) {
    offenders.push(
      `UnreachableNodesNotice takes {${props.join(', ')}} and may take only {${allowed.join(', ')}}. ` +
        'Any other prop is a channel a host name can travel down.',
    );
  }
}

// The signature keeps a name out of the props, and this keeps one out of the body.
const noticeBody = executable.slice(
  executable.indexOf('function UnreachableNodesNotice'),
  executable.indexOf('function UnreachableNodesDetail'),
);
const noticeHoles = [...noticeBody.matchAll(/\$\{([^}]*)\}/g)].map((m) => m[1].trim());
// The banner takes several name lists (unreachable, partially read and late clusters), and each
// is a channel.
if (noticeHoles.join(',') !== 'noun' || /\b(?:nodes|partialNodes|lateNodes|partial|late|unreachable|reasons)\b/.test(noticeBody)) {
  offenders.push('UnreachableNodesNotice prints something other than its noun');
}

// --- 3. the gate and the permission matrix agree ---------------------------------------------

/** The Set literal for one role inside ROLE_PERMISSIONS, paren-balanced from its key. */
function permissionsOf(role) {
  // Anchor inside ROLE_PERMISSIONS. `[Role.PARTNER]:` appears first in
  // ROLE_HIERARCHY, where it maps to a number, and reading that one finds no permissions at all
  // and reports the matrix as broken.
  const matrix = rbac.indexOf('ROLE_PERMISSIONS');
  if (matrix === -1) return null;
  const at = rbac.indexOf(`[Role.${role}]:`, matrix);
  if (at === -1) return null;
  const setAt = rbac.indexOf('new Set', at);
  if (setAt === -1) return null;
  const open = rbac.indexOf('(', setAt);
  let depth = 0;
  let end = -1;
  for (let i = open; i < rbac.length; i += 1) {
    if (rbac[i] === '(') depth += 1;
    else if (rbac[i] === ')') {
      depth -= 1;
      if (depth === 0) { end = i; break; }
    }
  }
  if (end === -1) return null;
  return new Set([...rbac.slice(open, end).matchAll(/Permission\.([A-Z_]+)/g)].map((m) => m[1]));
}

// Admin is ALL_PERMISSIONS rather than a literal set, so it is asserted by that shape instead.
if (!/\[Role\.ADMIN\]:\s*ALL_PERMISSIONS/.test(rbac)) {
  offenders.push('Role.ADMIN no longer maps to ALL_PERMISSIONS, so this cannot assume it holds cluster:read');
}

const partner = permissionsOf('PARTNER');
const tenant = permissionsOf('TENANT');
if (!partner || !tenant) {
  offenders.push('could not read ROLE_PERMISSIONS for PARTNER and TENANT out of types/rbac.ts');
} else {
  if (!partner.has('CLUSTER_READ')) {
    offenders.push('Partner no longer holds CLUSTER_READ, so hasRole(Role.PARTNER) is the wrong gate for a host name');
  }
  if (tenant.has('CLUSTER_READ')) {
    offenders.push('Tenant now holds CLUSTER_READ. Either the banner gate or the matrix is wrong; decide which.');
  }
}

// A new role between Partner and Tenant would pass everything above without anyone re-deriving
// the gate, so the Role enum is pinned.
const roles = [...rbac.matchAll(/^\s{2}([A-Z]+)\s*=\s*"/gm)].map((m) => m[1]);
if (roles.join(',') !== 'ADMIN,PARTNER,TENANT') {
  offenders.push(`the Role enum is now [${roles.join(', ')}]. Re-derive the banner gate before widening it.`);
}

// --- 4. no call site invents a second channel -------------------------------------------------

// lateNodes are node names on the same footing as nodes: the banner shows them to Partner and
// above only, and the tenant notice below never receives them.
const ALLOWED_PROPS = new Set(['nodes', 'partialNodes', 'lateNodes', 'noun', 'partialNoun', 'reasons', 'className', 'key']);
// The one noun that is not a literal: a specification category's title, from a fixed list.
const NOUN_EXPRESSIONS = new Set(['`${category?.title ?? "instance"} specifications`']);

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (full.endsWith('.tsx')) yield full;
  }
}

const lineOf = (source, index) => source.slice(0, index).split('\n').length;

let sites = 0;
for (const file of walk(src)) {
  const rel = relative(src, file).split(sep).join('/');
  if (rel === 'components/ui/unreachable-nodes-banner.tsx') continue;
  const body = readFileSync(file, 'utf8');
  if (!body.includes('<UnreachableNodesBanner')) continue;
  for (const tag of openingTags(body, 'UnreachableNodesBanner')) {
    sites += 1;
    // `noun` is printed to every role, so a name passed through it would reach a tenant.
    const noun = tag.text.match(/\bnoun=(?:"[^"]*"|\{)/);
    const expression = noun?.[0].endsWith('{')
      ? tag.text.slice(noun.index + noun[0].length, closing(tag.text, noun.index + noun[0].length - 1))
      : null;
    if (!noun || (expression !== null && !NOUN_EXPRESSIONS.has(expression))) {
      offenders.push(
        `${rel}:${lineOf(body, tag.index)} gives the banner a noun that is not a fixed word. It is ` +
          'printed to every role, so it cannot come from the payload.',
      );
    }
    for (const attr of tag.text.matchAll(/(?:^|\s)([a-zA-Z][a-zA-Z0-9]*)=/g)) {
      if (!ALLOWED_PROPS.has(attr[1])) {
        offenders.push(
          `${rel}:${lineOf(body, tag.index)} passes "${attr[1]}" to the banner. Declare it in ` +
            'ALLOWED_PROPS here first, having decided which roles may see it.',
        );
      }
    }
  }
}

// A scan over nothing passes for the wrong reason. Thirteen pages plus the software inventory.
if (sites < 14) {
  offenders.push(`only ${sites} banner call sites found, expected at least 14`);
}

if (offenders.length) {
  console.error('The unreachable-nodes banner can name a host to a role that may not see one:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nOne gate, in the exported component, and a renderer for the ungated side that');
  console.error('cannot be handed a name. See components/ui/unreachable-nodes-banner.tsx.');
  process.exit(1);
}

console.log(
  `banner role gate: 1 gate, ${sites} call sites, Partner holds cluster:read and Tenant does not`,
);
