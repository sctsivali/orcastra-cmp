#!/usr/bin/env node
// Structural guard: every surface that offers Rebuild asks the same question, and the server asks
// it too.
//
// The console toolbar once offered Rebuild to any Tenant holding `operate` while the row menu hid
// it below Partner and the server refused it with require_partner(): a Tenant filled in the whole
// dialog and got a 403. Rule 1: a file that opens RebuildDialog decides whether to offer it through
// lib/rebuild-access.ts (rebuildOffered or useRebuildOffered), never through its own role or
// permission check. Rule 2: the row menu builds the Rebuild item only inside `if (rebuildOffered)`.
// Rule 3: the server route checks verify_rebuild_access and no longer requires a Partner. Rule 4:
// `rebuild` is a tenant grant on both sides, and the client rule asks for the same three grants
// the server checks.
//
// Run: node scripts/checks/check-rebuild-gate-parity.mjs [repo-root]

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');
const offenders = [];
const fail = (msg) => offenders.push(msg);
const read = (path) => readFileSync(join(root, path), 'utf8');

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.tsx?$/.test(name)) out.push(full);
  }
  return out;
}

// Rule 1
const DIALOG_HOME = 'frontend/src/components/rebuild-dialog.tsx';
let surfaces = 0;
for (const file of walk(src)) {
  const rel = relative(root, file);
  if (rel === DIALOG_HOME) continue;
  const text = readFileSync(file, 'utf8');
  if (!/<RebuildDialog\b/.test(text)) continue;
  surfaces += 1;
  if (!/\b(rebuildOffered|useRebuildOffered)\b/.test(text)) {
    fail(`${rel}: opens RebuildDialog without asking lib/rebuild-access.ts whether to offer it`);
  }
}
if (surfaces < 2) fail(`only ${surfaces} file(s) open RebuildDialog; expected the Instances page and the console toolbar`);

const toolbar = read('frontend/src/components/console/console-toolbar.tsx');
if (!/\{rebuildIsOffered && powerButton\(\s*"rebuild"/.test(toolbar)) {
  fail('frontend/src/components/console/console-toolbar.tsx: the Rebuild button must render only when useRebuildOffered says so');
}

// Rule 2
const rows = read('frontend/src/app/inventory/instances/components/instance-row-actions.ts');
const rebuildBlock = /if \(rebuildOffered\) \{\s*actions\.push\(\{\s*id: "rebuild"/.test(rows);
const rebuildItems = (rows.match(/id: "rebuild"/g) ?? []).length;
if (!rebuildBlock || rebuildItems !== 1) {
  fail('instance-row-actions.ts: the one Rebuild item must be built inside `if (rebuildOffered)`');
}

// Rule 3
const route = read('backend/app/routers/instance_rebuild.py');
const post = /@router\.post\("\/\{instance_name\}\/rebuild"\)([\s\S]*?)\n@router\./.exec(route)?.[1] ?? '';
if (!post) fail('backend/app/routers/instance_rebuild.py: the rebuild POST route was not found');
if (/require_partner/.test(post)) fail('instance_rebuild.py: the rebuild POST still requires a Partner; Tenants with the grant are refused');
if (!/verify_rebuild_access\(/.test(post)) fail('instance_rebuild.py: the rebuild POST must check verify_rebuild_access');

// Rule 4
const catalogue = read('backend/app/core/tenant_permissions.py');
if (!/"rebuild"/.test(/TENANT_RULE_PERMISSIONS = frozenset\(\{([^}]*)\}\)/.exec(catalogue)?.[1] ?? '')) {
  fail('backend/app/core/tenant_permissions.py: `rebuild` is missing from TENANT_RULE_PERMISSIONS');
}
const types = read('frontend/src/types/api.ts');
if (!/export type TenantPermission = [^;]*"rebuild"/.test(types)) fail('frontend/src/types/api.ts: TenantPermission lacks "rebuild"');
const access = read('backend/app/core/instance_access.py');
const serverGrants = [...(/def verify_rebuild_access[\s\S]*?return scoped_project/.exec(access)?.[0] ?? '')
  .matchAll(/verify_tenant_permission\([^)]*"(\w+)"\)/g)].map((m) => m[1]).sort();
const clientRule = read('frontend/src/lib/rebuild-access.ts');
const clientGrants = [...clientRule.matchAll(/hasPermission\("(\w+)"\)/g)].map((m) => m[1]).sort();
if (serverGrants.join() !== 'operate,rebuild,view' || clientGrants.join() !== serverGrants.join()) {
  fail(`the client asks for [${clientGrants}] and the server checks [${serverGrants}]; both must be operate, rebuild and view`);
}

if (offenders.length) {
  console.error('rebuild gate parity: FAILED\n');
  for (const o of offenders) console.error(`  - ${o}`);
  console.error('');
  process.exit(1);
}
console.log(`rebuild gate parity: ${surfaces} surfaces, the row menu and the server ask the same three grants`);
