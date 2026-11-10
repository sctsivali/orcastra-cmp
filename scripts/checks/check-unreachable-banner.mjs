#!/usr/bin/env node
// Structural guard: a backend that names its dead clusters is useless if the page drops the label.
//
// Routers report unreachable_nodes so an outage cannot read as an empty result. The field and the
// banner live in different files, so either half can drift unseen: a router that reports the field
// without seeding it, or a page that never renders the banner and shows a shorter list with
// nothing explaining why.
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
    // The JSX tag, not the bare name: an import alone renders nothing.
    const source = readFileSync(path, 'utf8');
    if (!source.includes('<UnreachableNodesBanner')) {
      offenders.push(`${page} drops the unreachable_nodes that ${router} reports`);
    }
    // A cluster that answered and lost rows is a shorter list with no outage to explain it, so the
    // banner has to be handed those clusters too, in the same card as the unreachable ones.
    const losesRows = readFileSync(join(routersDir, router), 'utf8').includes('partial_nodes');
    if (losesRows && !openingTags(source, 'UnreachableNodesBanner').some((t) => /\spartialNodes=/.test(t.text))) {
      offenders.push(`${page} drops the partial_nodes that ${router} reports`);
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

// Comments go, offsets kept, so a line number still points at the source and a comment that
// names the tag is not counted as a render.
const stripComments = (source) =>
  source
    .replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:"'`])\/\/.*$/gm, (comment, lead) => lead + ' '.repeat(comment.length - lead.length));

// Where each top-level declaration starts. A component in these files is one, so the stretch
// from one start to the next is one component's body.
const DECLARATION = /^(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\b|const\s+[A-Za-z_$][\w$]*\s*=)/gm;

// Show Details is the whole diagnosis: a banner with no reasons lists bare names under it.
//
// One banner per list: unreachable and partly read clusters go to the same banner (partialNodes),
// so the operator reads one total. A second tag in the same component splits that count.
let banners = 0;
for (const file of walk(join(root, 'frontend', 'src'))) {
  if (file.endsWith('unreachable-nodes-banner.tsx')) continue;
  const source = stripComments(readFileSync(file, 'utf8'));
  const tags = openingTags(source, 'UnreachableNodesBanner');
  const lineAt = (index) => source.slice(0, index).split('\n').length;
  for (const tag of tags) {
    banners += 1;
    if (!/\breasons=/.test(tag.text)) {
      offenders.push(`${relative(root, file)}:${lineAt(tag.index)} renders the banner without reasons`);
    }
  }
  if (tags.length < 2) continue;
  const starts = [...source.matchAll(DECLARATION)].map((m) => m.index);
  const owner = (index) => starts.filter((start) => start <= index).pop() ?? 0;
  const perComponent = new Map();
  for (const tag of tags) {
    const key = owner(tag.index);
    perComponent.set(key, [...(perComponent.get(key) ?? []), tag]);
  }
  for (const group of perComponent.values()) {
    if (group.length > 1) {
      offenders.push(
        `${relative(root, file)}:${group.map((tag) => lineAt(tag.index)).join(',')} renders ` +
          `${group.length} banners in one component. Hand the partly read clusters to the one ` +
          'banner as partialNodes.',
      );
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
// One per list: twelve inventory pages, the Overview, and the single-banner surfaces.
if (banners < 20) {
  offenders.push(`only ${banners} banners found; expected at least 20`);
}

if (offenders.length) {
  console.error('Unreachable-cluster reporting is wired on one side only:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nA page whose list can shrink from an outage renders UnreachableNodesBanner');
  console.error('directly under its header. Copy inventory/networks/page.tsx.');
  process.exit(1);
}
console.log(`unreachable banner: ${reporting.length} reporting routers, every mapped page renders it`);
