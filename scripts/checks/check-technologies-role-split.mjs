#!/usr/bin/env node
// Structural guard for the Technologies role split. Runtime authorization remains a backend
// responsibility; this catches the UI regressions that would remount privileged node inventory
// for a Tenant or turn the category openers back into non-keyboard card click handlers.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path) => readFileSync(join(root, path), "utf8");

const page = read("frontend/src/app/technologies/page.tsx");
const card = read(
  "frontend/src/app/technologies/components/technology-summary-card.tsx",
);
const api = read("frontend/src/lib/api.ts");
const hardware = read(
  "frontend/src/app/technologies/components/hardware-aggregate.tsx",
);
const tenant = read(
  "frontend/src/app/technologies/components/tenant-instance-technologies.tsx",
);
const hardwareDialog = read(
  "frontend/src/app/technologies/components/hardware-detail-dialog.tsx",
);

const tenantStart = page.indexOf("function TenantTechnologies()");
const tenantEnd = page.indexOf("export default function TechnologiesPage()");
const tenantTree =
  tenantStart >= 0 && tenantEnd > tenantStart ? page.slice(tenantStart, tenantEnd) : "";

const failures = [];

if (!page.includes("return isPartner ? <PlatformTechnologies /> : <TenantTechnologies />")) {
  failures.push("Technologies must mount separate operator and Tenant trees by role.");
}
if (!tenantTree.includes("useInstanceSpecifications()")) {
  failures.push("Tenant Technologies must read the safe instance specifications projection.");
}
if (/useNodesStatus|nodeAPI|api\.nodes/.test(tenantTree)) {
  failures.push("Tenant Technologies references privileged node inventory.");
}
if (!api.includes('fetchRead<InstanceSpecificationsResponse>("/instances/specifications"')) {
  failures.push("The frontend API client is missing GET /instances/specifications.");
}
if (!card.includes('<button') || !card.includes('aria-haspopup="dialog"')) {
  failures.push("Technology category cards must be real dialog buttons.");
}
if (page.includes("Hardware by Node") || page.includes("NodeHardwareList")) {
  failures.push("The removed Hardware by Node disclosure returned to Technologies.");
}
if (!hardware.includes("reportingNodeCount > 0 ? totalThreads.toString() : EMPTY_VALUE")) {
  failures.push("Hardware totals must not present missing node readings as zero capacity.");
}
if (!hardware.includes('label: "Hardware Threads"')) {
  failures.push("Platform CPU capacity must be labelled as hardware threads, not cores.");
}
if (!page.includes("nodes.length > 0 && hardwareInfo ?")) {
  failures.push("Platform Technologies must show its empty state when no nodes exist.");
}
if (
  /resources\.cpu_(?:total|used|available|sockets) \|\| undefined/.test(hardwareDialog) ||
  /hardware\?\.cpu\.(?:cores|threads|numa_nodes) \|\| undefined/.test(hardwareDialog) ||
  /topology\?\.(?:hugepages_total|used|free) \?/.test(hardwareDialog)
) {
  failures.push("Hardware detail must preserve valid zero CPU and topology readings.");
}
if (
  !tenant.includes('countResourceSource(instances, field, "default")') ||
  !tenant.includes('countResourceSource(instances, field, "unknown")') ||
  !tenant.includes('instance.root_disk.source === "unknown"')
) {
  failures.push("Tenant summaries must account for LXD defaults and unknown limits.");
}
if (
  !tenant.includes('return "Explicit value not reported"') ||
  !tenant.includes('return "Inherited value not reported"') ||
  !tenant.includes('return "Pool default"') ||
  !tenant.includes('return "Unknown"')
) {
  failures.push("Tenant details must explain missing limits without presenting them as zero.");
}
if (
  !tenant.includes("unreachableNodes={response.unreachable_nodes}") ||
  !tenant.includes("nodes={unreachableNodes}")
) {
  failures.push("Every Tenant category dialog must retain the partial-cluster notice.");
}
if (
  !hardwareDialog.includes("DataTablePagination") ||
  !hardwareDialog.includes("selectedNode") ||
  hardwareDialog.includes("nodes.map((node)")
) {
  failures.push("Hardware dialogs must render node detail on demand instead of mounting every node.");
}
if (
  !hardwareDialog.includes("Refreshing hardware readings") ||
  !hardwareDialog.includes("The latest refresh failed") ||
  !tenant.includes("Refreshing instance specifications")
) {
  failures.push("Technology detail dialogs must expose loading and refresh-error states.");
}
if (
  !hardwareDialog.includes("animate-spin motion-reduce:animate-none") ||
  !tenant.includes("animate-spin motion-reduce:animate-none")
) {
  failures.push("Technology refresh spinners must respect reduced-motion preferences.");
}

if (failures.length) {
  console.error("Technologies role split: FAILED");
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}

console.log("technologies role split: scoped data and accessible category dialogs");
