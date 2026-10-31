#!/usr/bin/env node
/**
 * Operator-facing vocabulary contract for the console and its API descriptions.
 *
 * Wire names such as node, host, and location remain untouched. This guard targets phrases
 * that can be painted as copy, while allowing hardware terms such as NUMA Node and true host
 * surfaces. It is intentionally parser-free so the CI guards job needs no frontend install.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = join(root, "frontend", "src");

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* walk(path);
    else if (/\.tsx?$/.test(name)) yield path;
  }
}

function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "))
    .replace(/(^|[^:])\/\/.*$/gm, (comment) => comment.replace(/[^\n]/g, " "));
}

function lineOf(source, index) {
  return source.slice(0, index).split("\n").length;
}

const forbidden = [
  [/\bRAM\b/g, 'use "Memory", not "RAM"'],
  [/\b(?:VCPU|VCPUs|Vcpu|Vcpus)\b/g, 'spell virtual allocation as "vCPU" or "vCPUs"'],
  [/\b(?:Kib|Mib|Gib|Tib|Pib)\b/g, "use canonical IEC capitalization"],
  [/\b(?:CPU [Cc]ores?|Max CPU|Logical CPUs?|Total Threads|Unallocated Threads)\b/g, "distinguish vCPU allocation from hardware threads or physical cores"],
  [/\b(?:CPU count|CPU Quota|Unallocated CPU|CPU over-commit)\b/g, "use vCPU for virtual allocation"],
  [/\b(?:Register|Select|Search|Loading|Offline|Inactive|Total|Target|Reporting) Nodes?\b/gi, 'call a registered endpoint a "Cluster"'],
  [/\b(?:No|All) nodes?\b/gi, 'call registered endpoints "clusters"'],
  [/\bNode (?:Status|Type|Information|Registered)\b/g, 'call a registered endpoint a "Cluster"'],
  [/\b(?:Software by|Memory per) Node\b/g, 'call a registered endpoint a "Cluster"'],
  [/\b(?:across (?:all )?|connected |hypervisor |different )nodes?\b/gi, 'call registered endpoints "clusters"'],
  [/\bpick a different node\b/gi, 'call a registered endpoint a "cluster"'],
  [/\bfrom node\b/gi, 'call a registered endpoint a "cluster"'],
  [/\b(?:label|title|placeholder|searchPlaceholder|emptyText)\s*[:=]\s*["'`]Nodes?["'`]/g, 'use "Cluster" in operator labels'],
  [/>\s*Nodes?\s*</g, 'use "Cluster" in operator text'],
  [/\bOrg:/g, 'spell the authorization concept as "Organization"'],
  [/\bNode ID:/g, 'call a registered endpoint identifier "Cluster ID"'],
  [/\b(?:[Tt]his|[Yy]our) node\b/g, 'call a registered endpoint a "cluster", or a machine in one a "member"'],
  [/\bregistering the node\b/gi, 'call a registered endpoint a "cluster"'],
];

const requiredContracts = [
  ["components/project-create-form.tsx", "Max vCPUs", "project allocation uses vCPU"],
  ["components/project-create-form.tsx", "Max Memory", "project allocation uses Memory"],
  ["components/project-create-form.tsx", "hardware threads", "host capacity uses hardware threads"],
  ["app/inventory/monitoring/monitoring-content.tsx", 'formatBytes(data.memoryUsedBytes)', "monitoring memory uses the one size formatter"],
  ["app/inventory/monitoring/monitoring-content.tsx", 'formatNetworkRate(', "monitoring rate uses the network formatter"],
  ["app/inventory/monitoring/monitoring-content.tsx", "CPU Usage", "utilization is CPU usage"],
  // The utilization gauges moved out of monitoring-content.tsx; the contract moved with them.
  ["app/inventory/monitoring/components/resource-utilization-card.tsx", 'label="CPU Usage"', "the utilization gauge is CPU usage"],
  ["app/inventory/monitoring/components/resource-utilization-card.tsx", 'formatBytes(stats.used_memory_bytes)', "the memory gauge uses the one size formatter"],
  ["components/regions/regions-page.tsx", "Hardware Threads", "region capacity uses hardware threads"],
  ["app/technologies/components/hardware-aggregate.tsx", 'label: "Reporting Clusters"', "technology coverage uses clusters"],
  ["app/technologies/components/hardware-aggregate.tsx", 'label: "Memory per Cluster"', "technology averages use clusters"],
  ["app/inventory/warnings/components/warning-detail-dialog.tsx", 'label="Cluster Member"', "LXD machine location uses Cluster Member"],
  ["components/node-connection-summary.tsx", "Cluster ID:", "registered endpoint metadata uses Cluster ID"],
  ["components/user-groups-settings.tsx", 'label: "Organizations"', "Organization remains a distinct authorization concept"],
  ["components/user-groups-settings.tsx", '<SelectItem value="tenant">Tenant</SelectItem>', "Tenant remains a distinct authorization role"],
];

const backendRequiredContracts = [
  [
    "backend/app/models/schemas.py",
    'description="Target Cluster"',
    "project OpenAPI target uses Cluster",
  ],
  [
    "backend/app/models/schemas.py",
    'description="Maximum vCPU allocation"',
    "project OpenAPI allocation uses vCPU",
  ],
  [
    "backend/app/models/schemas.py",
    'description="Maximum Memory (for example, \'2GiB\' or \'512MiB\')"',
    "project OpenAPI memory uses Memory and IEC units",
  ],
  [
    "backend/app/models/schemas.py",
    'description="Maximum Storage (for example, \'10GB\' or \'500MB\')"',
    "project OpenAPI storage uses decimal units",
  ],
  [
    "backend/app/models/schemas.py",
    "Hardware thread count; the legacy wire field name is retained for compatibility.",
    "legacy cpu_cores documents its hardware-thread semantics",
  ],
  [
    "backend/app/models/schemas.py",
    "Memory in GiB; the legacy ``ram_total_gb`` wire name is retained for compatibility.",
    "legacy ram_total_gb documents its binary-memory semantics",
  ],
  [
    "backend/app/services/lxd/resources.py",
    'f"CPU capacity={aggregated_resources.cpu_total} hardware threads, "',
    "Cluster resource logs distinguish hardware threads",
  ],
  [
    "backend/app/services/lxd/node_resources.py",
    'f"({total_cpu_used} vCPUs allocated), "',
    "member aggregation logs distinguish vCPU allocation",
  ],
  [
    "backend/app/routers/cluster_members.py",
    'f"Memory={memory_total / 1024 / 1024 / 1024:.1f} GiB "',
    "Cluster member logs label binary memory as GiB",
  ],
  [
    "backend/app/routers/nodes.py",
    'description="Get CPU, Memory, and Storage capacity for a specific Cluster."',
    "Cluster resource OpenAPI copy uses canonical terms",
  ],
  [
    "backend/app/routers/projects.py",
    'f"{resources.get(\'cpu_total\', 0)} hardware threads"',
    "project capacity refusal distinguishes host hardware threads",
  ],
  [
    "backend/app/routers/projects.py",
    'f"Storage limit ({format_size(storage_bytes)})',
    "project storage refusal uses the one size formatter",
  ],
];

const backendGlossaryFiles = [
  "backend/app/models/schemas.py",
  "backend/app/models/instance_metrics.py",
  "backend/app/services/lxd/instance_metrics.py",
  "backend/app/services/lxd/instance_metrics_sections.py",
  "backend/app/routers/cluster_members.py",
  "backend/app/routers/monitoring.py",
  "backend/app/services/monitoring_history.py",
  "backend/app/services/monitoring_history_store.py",
  "backend/app/services/monitoring_usage.py",
  "backend/app/services/monitoring_sweep.py",
  "backend/app/services/monitoring_memory.py",
  "backend/app/core/host_detail.py",
  "backend/app/routers/nodes.py",
  "backend/app/services/lxd/node_resources.py",
  "backend/app/services/lxd/resources.py",
  // Their detail and warning messages reach the console verbatim through lib/api.ts.
  "backend/app/core/lxd_errors.py",
  "backend/app/routers/lxd_warnings.py",
  "backend/app/routers/cert_manager.py",
  "backend/app/routers/my_keys.py",
  "backend/app/routers/security_advisories.py",
];

const backendForbidden = [
  [/\bRAM\b/g, 'use "Memory", not "RAM"'],
  [/\bCPU cores?\b/g, 'use "hardware threads", verified "physical cores", or "vCPUs"'],
  [/\bex;,/g, 'write "for example" in operator-facing API descriptions'],
  [/\.1f\}\s+GB\b/g, "a 1024-derived Memory display must use GiB"],
  [/\bdef\s+format_bytes\s*\(/g, "do not restore a private ambiguous byte formatter"],
  [/\bNode is not reachable\b/g, 'a registered endpoint is a "Cluster" in operator messages'],
  [/\bFailed to (?:get node resources|process node)\b/g, 'a registered endpoint is a "cluster" in operator messages'],
  [/\bCould not reach the LXD host\b/g, 'say "cluster": the endpoint may be Incus, and it may be several machines'],
  [/\bCould not read host kernels\b/g, 'the kernels belong to cluster members'],
];

const failures = [];
let scanned = 0;

for (const file of walk(src)) {
  scanned += 1;
  const raw = readFileSync(file, "utf8");
  const source = stripComments(raw);
  const rel = relative(src, file).split(sep).join("/");

  for (const [pattern, guidance] of forbidden) {
    pattern.lastIndex = 0;
    let match;
    while ((match = pattern.exec(source)) !== null) {
      failures.push(`frontend/src/${rel}:${lineOf(source, match.index)} ${guidance}: ${JSON.stringify(match[0])}`);
      if (!pattern.global) break;
    }
  }
}

for (const [rel, needle, label] of requiredContracts) {
  const source = readFileSync(join(src, rel), "utf8");
  if (!source.includes(needle)) failures.push(`frontend/src/${rel}: missing glossary contract: ${label}`);
}

for (const [rel, needle, label] of backendRequiredContracts) {
  const source = readFileSync(join(root, rel), "utf8");
  if (!source.includes(needle)) failures.push(`${rel}: missing glossary contract: ${label}`);
}

for (const rel of backendGlossaryFiles) {
  const source = readFileSync(join(root, rel), "utf8");
  for (const [pattern, guidance] of backendForbidden) {
    pattern.lastIndex = 0;
    let match;
    while ((match = pattern.exec(source)) !== null) {
      failures.push(`${rel}:${lineOf(source, match.index)} ${guidance}: ${JSON.stringify(match[0])}`);
      if (!pattern.global) break;
    }
  }
}

if (scanned < 200) failures.push(`only scanned ${scanned} TypeScript files; expected the full frontend tree`);

if (failures.length) {
  console.error("UI glossary: FAILED");
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}

console.log(`UI glossary: ${scanned} files use canonical resource and authorization terminology`);
