#!/usr/bin/env node
// Structural guard: a control that asks an operator to choose a cluster must say whether that
// cluster is answering.
//
// The vocabulary, the dot and the consequence line all already existed in lib/cluster-status.ts,
// components/ui/searchable-select.tsx and components/ui/node-status-notice.tsx. What kept going
// wrong was coverage: six create forms were wired to them while the monitoring filter, the key
// generator, the group creator and the cluster-members picker each kept their own plain Select
// over bare names. Two of those went further and silently dropped the clusters that were not
// answering, so an operator whose node had gone down saw no row and no reason.
//
// None of that is visible from inside one file, which is why it is checked here rather than
// reviewed. Every picker is registered below with how it resolves status, and anything that
// looks like a new one has to be classified rather than shipped unnoticed.
//
// Run: node scripts/checks/check-node-picker-status.mjs [repo-root]

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const srcDir = join(root, 'frontend', 'src');

// How each known picker resolves status.
//   'hook' - useNodePickerOptions, the one place options are assembled
//   'meta' - builds options inline but resolves through clusterStatusMeta
//   string - a deferred gap, carrying the reason it is still open
const PICKERS = new Map([
  ['components/instance-create-form.tsx', 'hook'],
  ['components/vm-create-form.tsx', 'hook'],
  ['components/project-create-form.tsx', 'hook'],
  ['components/network-create-form.tsx', 'hook'],
  ['components/network-acls/acl-form-dialog.tsx', 'hook'],
  ['components/image-upload-dialog.tsx', 'hook'],
  ['components/iso-upload-dialog.tsx', 'hook'],
  ['components/groups/create-group-dialog.tsx', 'hook'],
  ['app/my-keys/page.tsx', 'hook'],
  ['components/tenant-access-dialog.tsx', 'meta'],
  ['components/policy-management-section.tsx', 'meta'],
  ['app/inventory/clustering/members/page.tsx', 'meta'],
  ['components/monitoring/node-filter-select.tsx', 'meta'],
  // Closed. /api/v1/integrations/available-clusters now sends status, reason and checked_at, so
  // the component resolves it like every other picker instead of offering bare names.
  ['components/cluster-access-selector.tsx', 'meta'],
  // note: this one slipped the check for its whole life by being split across two files. The page
  // called useNodePickerOptions but rendered no Select, and the picker rendered a Select whose
  // options arrived as a prop named `nodeOptions`, so neither half matched both halves of the
  // shape test. Splitting a picker across a component boundary is a way past this guard, which
  // is worth knowing about the guard.
  ['app/inventory/snapshots/components/snapshot-scope-picker.tsx', 'hook'],
]);

// Not pickers, though they trip the shape test below.
const NOT_A_PICKER = new Map([
  ['components/dashboard.tsx', 'its Selects are MiB/GiB units; useClusters feeds a table'],
  ['components/groups/edit-group-dialog.tsx', 'node is a disabled Input, not a choice'],
  ['app/inventory/monitoring/monitoring-content.tsx', 'delegates to NodeFilterSelect; its own Select is Top-N'],
  ['components/integrations/create-api-key-dialog.tsx', 'mounts ClusterAccessSelector, registered above'],
  ['components/integrations/edit-api-key-permissions-dialog.tsx', 'mounts ClusterAccessSelector, registered above'],
]);

// A file worth classifying: it renders a picker control AND names a cluster/node option source.
const RENDERS_PICKER = /<SearchableSelect|<Select[\s>]/;
const CLUSTER_SOURCE =
  /useNodeNames|useNodesStatus|useNodePickerOptions|useClusters|nodeNames|availableClusters|AvailableCluster/;

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (entry.endsWith('.tsx')) out.push(full);
  }
  return out;
}

const failures = [];
const files = walk(srcDir);
const seen = new Set();

for (const full of files) {
  const rel = relative(srcDir, full).split('\\').join('/');
  const body = readFileSync(full, 'utf8');

  const registered = PICKERS.get(rel);
  if (registered === 'hook' || registered === 'meta') {
    seen.add(rel);
    const symbol = registered === 'hook' ? 'useNodePickerOptions' : 'clusterStatusMeta';
    // note: word-boundary, not includes(). A first cut used includes() and a probe that renamed
    // the call to clusterStatusMetaX still passed, because the old name is a prefix of the new.
    if (!new RegExp(`\\b${symbol}\\b`).test(body)) {
      failures.push(
        `${rel}: registered as '${registered}' but does not use ${symbol}. ` +
          `A cluster picker must resolve status through lib/cluster-status.ts.`,
      );
    }
    continue;
  }
  if (registered !== undefined) {
    seen.add(rel);
    continue; // a deferred gap, carrying its reason
  }
  if (NOT_A_PICKER.has(rel)) {
    seen.add(rel);
    continue;
  }

  if (RENDERS_PICKER.test(body) && CLUSTER_SOURCE.test(body)) {
    failures.push(
      `${rel}: looks like a cluster or node picker but is not registered in ` +
        `scripts/checks/check-node-picker-status.mjs. Wire it to useNodePickerOptions, or ` +
        `record why it is not a picker.`,
    );
  }
}

// A registry entry naming a file that no longer exists is a stale claim of coverage.
for (const rel of [...PICKERS.keys(), ...NOT_A_PICKER.keys()]) {
  if (!seen.has(rel)) {
    failures.push(`${rel}: registered here but not found under frontend/src. Stale entry.`);
  }
}

const wired = [...PICKERS.values()].filter((v) => v === 'hook' || v === 'meta').length;
const deferred = [...PICKERS.values()].filter((v) => v !== 'hook' && v !== 'meta');

console.log(
  `node picker status: ${files.length} files scanned, ${wired} pickers carrying status, ` +
    `${deferred.length} deferred`,
);
for (const [rel, reason] of PICKERS) {
  if (reason !== 'hook' && reason !== 'meta') console.log(`  deferred  ${rel}: ${reason}`);
}

if (failures.length) {
  console.error('\nnode picker status: FAILED');
  for (const f of failures) console.error(`  ${f}`);
  process.exit(1);
}
console.log('node picker status: every cluster picker names the state of what it offers');
