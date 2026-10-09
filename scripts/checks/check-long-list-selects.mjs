#!/usr/bin/env node
// Structural guard: a list that comes from an API with no upper bound is searchable.
//
// An enum the code declares (units, tri-states, a disk kind) is fine as a plain Radix Select.
// A list of projects, pools, volumes, networks or organisations is not: on one fleet the
// project picker on the create-instance form held more rows than the popover could show, and
// the operator scrolled for the one they wanted. SearchableSelect (components/ui) renders a
// search row once a list passes its threshold and stays as compact as a Select under it, so
// there is no reason to hand a data-driven list to <Select>.
//
// The shape test below flags a <SelectContent> whose body maps a data-looking identifier into
// <SelectItem>. Every conversion is registered by the trigger id it carries so a spec can find
// it; a file that still needs the plain shape is registered with the reason, and a registry
// entry naming a file that no longer exists fails, so coverage cannot be claimed for nothing.
//
// Run: node scripts/checks/check-long-list-selects.mjs [repo-root]

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = join(root, 'frontend', 'src');

// Files whose data-driven lists are SearchableSelect, by the trigger ids they must carry.
const CONVERTED = new Map([
  ['components/instance-create-form.tsx', ['create-node', 'create-project', 'create-pool']],
  ['components/instance-create/disk-row.tsx', ['create-disk-pool-${disk.id}', 'create-disk-volume-${disk.id}']],
  ['components/instance-create/nic-row.tsx', ['nic-network-${nic.id}']],
  ['components/network-acls/acl-form-dialog.tsx', ['acl-project']],
  ['components/profiles/profile-form-dialog.tsx', ['profile-project']],
  ['components/networks/network-create-dialog.tsx', ['net-project']],
  ['app/my-keys/page.tsx', ['project']],
  ['components/iso-upload-dialog.tsx', ['iso-pool']],
  ['components/integrations/transfer-owner-key-dialog.tsx', ['transfer-owner-key']],
]);

// Files that still map a data-driven list into <SelectItem>, each with the reason it waits.
const DEFERRED = new Map([
  [
    'components/policy-management-section.tsx',
    'rule rows are compact (h-7 text-xs) and SearchableSelect has no size variant yet; the ' +
      'organisation list is bounded and carries a __none__ sentinel row',
  ],
  [
    'components/tenant-access-dialog.tsx',
    'same compact rule rows and organisation list as policy-management-section.tsx',
  ],
  ['components/org-personalization-settings.tsx', 'bounded organisation list with a "via app" sublabel'],
  ['components/user-groups-settings.tsx', 'bounded organisation list with a __none__ sentinel row'],
  ['components/vm-create-form.tsx', 'exported, mounted by no route'],
]);

// An identifier that names rows an API returned, as opposed to a declared enum.
const DATA_LIST =
  /\b([\w$]*?(?:project|pool|volume|network|org|organization|organisation|uplink|profile|image|instance|cluster|node)s?(?:Options|List|Rows|Data)?)\s*\??\.map\s*\(/i;

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (entry.endsWith('.tsx')) out.push(full);
  }
  return out;
}

/** The body of each <SelectContent>...</SelectContent>. */
function selectContents(code) {
  const out = [];
  const open = /<SelectContent\b/g;
  let m;
  while ((m = open.exec(code)) !== null) {
    const end = code.indexOf('</SelectContent>', m.index);
    if (end === -1) break;
    out.push(code.slice(m.index, end));
  }
  return out;
}

const failures = [];
const seen = new Set();
let flagged = 0;
const files = walk(srcDir);

for (const file of files) {
  const rel = relative(srcDir, file);
  const body = readFileSync(file, 'utf8');

  const ids = CONVERTED.get(rel);
  if (ids) {
    seen.add(rel);
    for (const id of ids) {
      const literal = id.includes('${') ? `id={\`${id}\`}` : `id="${id}"`;
      const tag = body.indexOf(literal);
      const inSearchable = tag !== -1 && body.lastIndexOf('<SearchableSelect', tag) > body.lastIndexOf('<Select', tag);
      if (!inSearchable) {
        failures.push(`${rel}: registered as converted but no <SearchableSelect ${literal}> was found`);
      }
    }
  }
  if (DEFERRED.has(rel)) seen.add(rel);

  for (const content of selectContents(body)) {
    const hit = content.match(DATA_LIST);
    if (!hit || !/<SelectItem\b/.test(content)) continue;
    if (DEFERRED.has(rel)) {
      flagged += 1;
      continue;
    }
    failures.push(
      `${rel}: <SelectContent> maps ${hit[1]} into <SelectItem>. A data-driven list is a ` +
        `SearchableSelect; register the file in scripts/checks/check-long-list-selects.mjs ` +
        `with a reason if it must stay a plain Select.`,
    );
  }
}

for (const rel of [...CONVERTED.keys(), ...DEFERRED.keys()]) {
  if (!seen.has(rel)) failures.push(`${rel}: registered here but not found under frontend/src. Stale entry.`);
}

console.log(
  `long-list selects: ${files.length} files scanned, ${CONVERTED.size} files searchable, ` +
    `${DEFERRED.size} deferred (${flagged} plain lists carried with a reason)`,
);
for (const [rel, why] of DEFERRED) console.log(`  deferred  ${rel}: ${why}`);

if (failures.length) {
  console.error('\nlong-list selects: FAILED');
  for (const f of failures) console.error(`  ${f}`);
  process.exit(1);
}
console.log('long-list selects: every data-driven list is searchable');
