#!/usr/bin/env node
// Structural guard: a backend that names its dead clusters is useless if the page drops the label.
//
// Eleven routers report unreachable_nodes so an outage cannot read as an empty result. Twice now
// the two halves drifted apart. networks.py shipped the field without seeding it, and lxd_server.py
// and lxd_warnings.py seeded it while their pages never rendered the banner, so the operator saw a
// shorter list and nothing explaining why. Neither gap was visible from inside one file.
//
// The backend half is ratcheted in backend/tests/test_fresh_bypass_is_wired.py. This is the front
// half: every router that reports the field must map to a page, and every mapped page must render
// UnreachableNodesBanner. A new router with no entry here fails rather than passing silently.
//
// Run: node scripts/checks/check-unreachable-banner.mjs [repo-root]

import { readdirSync, readFileSync, existsSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { openingTags } from './source-scan.mjs';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const routersDir = join(root, 'backend', 'app', 'routers');
const appDir = join(root, 'frontend', 'src', 'app');

// Which page renders each router's list. A router serving no page of its own maps to [].
const ROUTER_TO_PAGES = new Map([
  ['instances.py', ['inventory/instances/page.tsx']],
  ['ipam.py', ['inventory/ipam/page.tsx']],
  ['lxd_operations.py', ['inventory/operations/page.tsx']],
  ['lxd_server.py', ['inventory/server/page.tsx']],
  ['lxd_warnings.py', ['inventory/warnings/page.tsx']],
  ['network_acls.py', ['inventory/network-acls/page.tsx']],
  ['networks.py', ['inventory/networks/page.tsx']],
  ['permissions.py', ['inventory/identities/page.tsx', 'inventory/groups/page.tsx']],
  ['profiles.py', ['inventory/profiles/page.tsx']],
  ['storage_pools.py', ['inventory/storage-pools/page.tsx']],
  ['storage_volumes.py', ['inventory/storage-volumes/page.tsx']],
  ['snapshots_fleet.py', ['inventory/snapshots/page.tsx']],
  // /vms backs the same Instances page as instances.py, which is already covered above.
  ['vms.py', []],
]);

const reporting = readdirSync(routersDir)
  .filter((f) => f.endsWith('.py'))
  .filter((f) => readFileSync(join(routersDir, f), 'utf8').includes('unreachable_nodes'))
  .sort();

const offenders = [];

for (const router of reporting) {
  if (!ROUTER_TO_PAGES.has(router)) {
    offenders.push(`${router} reports unreachable_nodes but has no entry in ROUTER_TO_PAGES`);
    continue;
  }
  for (const page of ROUTER_TO_PAGES.get(router)) {
    const path = join(appDir, page);
    if (!existsSync(path)) {
      offenders.push(`${page} is mapped from ${router} but does not exist`);
      continue;
    }
    // the JSX tag, not the bare name: an import alone renders nothing
    const source = readFileSync(path, 'utf8');
    if (!source.includes('<UnreachableNodesBanner')) {
      offenders.push(`${page} drops the unreachable_nodes that ${router} reports`);
    }
  }
}

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (full.endsWith('.tsx')) yield full;
  }
}

// Show Details is the whole diagnosis: a banner with no reasons lists bare names under it. Each
// banner on its own, because most pages hold two, and one that kept `reasons` hid the other.
let banners = 0;
for (const file of walk(join(root, 'frontend', 'src'))) {
  if (file.endsWith('unreachable-nodes-banner.tsx')) continue;
  const source = readFileSync(file, 'utf8');
  for (const tag of openingTags(source, 'UnreachableNodesBanner')) {
    banners += 1;
    if (!/\breasons=/.test(tag.text)) {
      const line = source.slice(0, tag.index).split('\n').length;
      offenders.push(`${relative(root, file)}:${line} renders the banner without reasons`);
    }
  }
}

// A mapping that has gone stale in the other direction is also a failure: it would let a router
// silently stop reporting while this file still claims it does.
for (const router of ROUTER_TO_PAGES.keys()) {
  if (!reporting.includes(router)) {
    offenders.push(`${router} is mapped here but no longer reports unreachable_nodes`);
  }
}

// A scan over nothing passes for the wrong reason.
if (reporting.length < 11) {
  offenders.push(`only ${reporting.length} routers report unreachable_nodes; expected at least 11`);
}
if (banners < 30) {
  offenders.push(`only ${banners} banners found; expected at least 30`);
}

if (offenders.length) {
  console.error('Unreachable-cluster reporting is wired on one side only:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nA page whose list can shrink from an outage renders UnreachableNodesBanner');
  console.error('directly under its header. Copy inventory/networks/page.tsx.');
  process.exit(1);
}
console.log(`unreachable banner: ${reporting.length} reporting routers, every mapped page renders it`);
