#!/usr/bin/env node
// Structural security guard: tenant-visible surfaces must not mount the privileged node
// inventory query. The backend remains authoritative, but a UI that knowingly calls a 403
// endpoint leaks intent, produces broken dialogs, and can regress as soon as a role gate moves.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path) => readFileSync(join(root, path), "utf8");

const home = read("frontend/src/app/page.tsx");
const dashboard = read("frontend/src/components/dashboard.tsx");
const tenantCards = read(
  "frontend/src/components/node-status/tenant-cluster-status-cards.tsx",
);
const nav = read("frontend/src/components/sidebar/nav-config.tsx");
const sidebar = read("frontend/src/components/sidebar/sidebar.tsx");
const hooks = read("frontend/src/hooks/use-lxd.ts");

const failures = [];

if (!home.includes("useNodeAvailability()")) {
  failures.push("Home must count clusters through useNodeAvailability().");
}
if (/useNodesStatus|api\.nodes\.status/.test(home)) {
  failures.push("Home still references the privileged /nodes inventory query.");
}
if (!dashboard.includes("isPartner ? (") || !dashboard.includes("<TenantClusterStatusCards")) {
  failures.push("Overview must split operator node inventory from tenant availability by role.");
}
if (!tenantCards.includes("useNodeAvailability()")) {
  failures.push("Tenant cluster cards must use the availability-only hook.");
}
if (/useNodesStatus|NodeDetailModal|api\.nodes\.status|\.resources\(/.test(tenantCards)) {
  failures.push("Tenant cluster cards reference privileged node inventory or hardware detail.");
}
if (
  !nav.includes("if (canViewNodeInventory)") ||
  !nav.includes("queryKeys.nodesFor(actorScope)") ||
  !nav.includes("api.nodes.status()") ||
  !nav.includes("queryKeys.nodeAvailabilityFor(actorScope)") ||
  !nav.includes("api.nodes.availability()")
) {
  failures.push("Home prefetch must split scoped operator inventory from tenant availability.");
}
if (!sidebar.includes("buildPrefetchMap(queryClient, scopeKey, isPartner)")) {
  failures.push("Sidebar node prefetch must carry the actor scope and role boundary.");
}
for (const scopedKey of [
  "queryKeys.nodesFor(scopeKey)",
  "queryKeys.nodeNamesFor(scopeKey)",
  "queryKeys.nodeAvailabilityFor(scopeKey)",
  "queryKeys.nodeResources(scopeKey, nodeName)",
  "queryKeys.nodeGpus(scopeKey, nodeName)",
]) {
  if (!hooks.includes(scopedKey)) {
    failures.push(`Node inventory cache is missing actor scope: ${scopedKey}.`);
  }
}

if (failures.length) {
  console.error("Tenant node inventory boundary: FAILED");
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}

console.log("tenant node inventory: tenant surfaces use availability only");
