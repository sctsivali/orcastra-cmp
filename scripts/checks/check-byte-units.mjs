#!/usr/bin/env node
/**
 * Guard the console's semantic byte contract.
 *
 * Storage, images, files, transfers, and network counters use decimal SI labels. Memory,
 * swap, VRAM, and explicit LXD binary limits use binary IEC labels. Keeping this parser-free
 * lets the CI guards job run before frontend dependencies are installed.
 */

import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = join(root, "frontend", "src");
const utilsPath = join(src, "lib", "utils.ts");
const utils = readFileSync(utilsPath, "utf8");

const SI = /\b(?:KB|MB|GB|TB|PB)(?:\/s)?\b/;
const IEC = /\b(?:KiB|MiB|GiB|TiB|PiB)(?:\/s)?\b/;
const SCALE_1024 = /Math\.pow\(\s*1024|1024\s*\*\*|\/\s*1024(?:\s*\/\s*1024)+|\/\s*\(1024\s*\*|\bk\s*=\s*1024\b/;
const SCALE_1000 = /Math\.pow\(\s*1000|1000\s*\*\*|\/\s*1000(?:\s*\/\s*1000)+|\/\s*\(1000\s*\*|\bk\s*=\s*1000\b/;
const MEMORY_ARG = /\b(?:memory|ram|swap|vram)\w*/i;
const STORAGE_ARG =
  /\b(?:storage|disk|space|pool)\w*|\b(?:image|file|transfer|network)\w*(?:_|\.|\?\.)?(?:size|bytes)\w*|\bbytes_(?:rx|tx)\w*/i;

const PRIVATE_DISPLAY_FORMATTER =
  /\b(?:function\s+|const\s+)(format(?:Bytes|Memory|Size|Rate|NetworkSpeed|BytesAuto|Quota))\b/g;
const PRIVATE_FORMATTER_ALLOWLIST = new Set([
  // Compatibility re-exports only. The implementation delegates to the semantic shared helpers.
  "components/member-resources/format.ts",
  // Explicit LXD limit strings are a separate binary-input contract, not display arithmetic.
  "lib/lxd-limits.ts",
]);

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* walk(path);
    else if (/\.tsx?$/.test(name)) yield path;
  }
}

function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

function lineOf(source, index) {
  return source.slice(0, index).split("\n").length;
}

function windows(lines, pattern) {
  const hits = [];
  lines.forEach((line, index) => {
    pattern.lastIndex = 0;
    if (pattern.test(line)) {
      hits.push({ line: index + 1, text: lines.slice(Math.max(0, index - 7), index + 8).join("\n") });
    }
  });
  return hits;
}

function crossedFormatters(source) {
  const hits = [];
  const call = /\b(formatBytes|formatMemory|formatLxdLimit|formatBytesCompact|formatMemoryCompact|formatByteQuota|formatMemoryQuota)\(\s*([^)]{0,180}?)\s*[,)]/gs;
  let match;
  while ((match = call.exec(source)) !== null) {
    const [, formatter, argument] = match;
    if (formatter === "formatLxdLimit") continue;
    const decimal = formatter === "formatBytes" || formatter === "formatBytesCompact" || formatter === "formatByteQuota";
    if (decimal && MEMORY_ARG.test(argument) && !STORAGE_ARG.test(argument)) {
      hits.push({ line: lineOf(source, match.index), why: `${formatter} receives a memory value` });
    }
    if (!decimal && STORAGE_ARG.test(argument) && !MEMORY_ARG.test(argument)) {
      hits.push({ line: lineOf(source, match.index), why: `${formatter} receives a storage or network value` });
    }
  }
  return hits;
}

const failures = [];
let scanned = 0;

const requiredUtilsContracts = [
  ['const SI_BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]', "decimal SI unit list"],
  ['const IEC_BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]', "binary IEC unit list"],
  ["scaleBytes(bytes, 1000, SI_BYTE_UNITS", "decimal storage formatter"],
  ["scaleBytes(bytes, 1024, IEC_BYTE_UNITS", "binary memory formatter"],
  ["export function formatLxdLimit", "explicit LXD binary-limit formatter"],
  ["scaleBytes(bytesPerSecond, 1000, SI_BYTE_UNITS", "decimal network-rate formatter"],
  ["scaleByteQuota(usedBytes, limitBytes, 1000, SI_BYTE_UNITS", "decimal storage quota formatter"],
  ["scaleByteQuota(usedBytes, limitBytes, 1024, IEC_BYTE_UNITS", "binary memory quota formatter"],
];

const limitCompatibility = readFileSync(join(src, "lib", "lxd-limits.ts"), "utf8");
if (!limitCompatibility.includes("return formatLxdLimit(bytes, { decimals: 1 })")) {
  failures.push("frontend/src/lib/lxd-limits.ts: formatLimitBytes must delegate to formatLxdLimit");
}
const operationSummary = readFileSync(
  join(src, "components", "instance-specification", "operation-summary.tsx"),
  "utf8",
);
if (!operationSummary.includes("formatLxdLimit(disk)")) {
  failures.push("instance specification root-disk readback must preserve the LXD binary unit");
}
const fileTransfer = readFileSync(
  join(root, "backend", "app", "routers", "file_transfer.py"),
  "utf8",
);
for (const snippet of [
  "MAX_UPLOAD_SIZE_MB = 100",
  "MAX_UPLOAD_SIZE = MAX_UPLOAD_SIZE_MB * 1_000_000",
  'detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_MB} MB."',
]) {
  if (!fileTransfer.includes(snippet)) {
    failures.push(`backend/app/routers/file_transfer.py: missing decimal file-size contract ${JSON.stringify(snippet)}`);
  }
}
if (/MAX_UPLOAD_SIZE\s*=\s*100\s*\*\s*1024/.test(fileTransfer)) {
  failures.push("backend/app/routers/file_transfer.py: MB-labelled upload limit must use decimal bytes");
}
const extensionManifest = readFileSync(
  join(root, "backend", "app", "core", "extension_manifest.py"),
  "utf8",
);
const publishAddon = readFileSync(
  join(src, "components", "extensions", "publish-addon-dialog.tsx"),
  "utf8",
);
if (!extensionManifest.includes("MANIFEST_MAX_BYTES = 16_000")) {
  failures.push("backend extension manifest: the documented 16 KB cap must equal 16,000 bytes");
}
if (!publishAddon.includes("const MAX_MANIFEST_BYTES = 16_000")) {
  failures.push("publish add-on dialog: the documented 16 KB cap must equal 16,000 bytes");
}
if (/MANIFEST_MAX_BYTES\s*=\s*16\s*\*\s*1024/.test(extensionManifest)) {
  failures.push("backend extension manifest: a KB-labelled file cap must not use binary scaling");
}
if (/MAX_MANIFEST_BYTES\s*=\s*16\s*\*\s*1024/.test(publishAddon)) {
  failures.push("publish add-on dialog: a KB-labelled file cap must not use binary scaling");
}
const projectEdit = readFileSync(
  join(src, "components", "dashboard", "project-card-dialogs.tsx"),
  "utf8",
);
for (const snippet of [
  'editableLxdLimit(config["limits.memory"])',
  'editableStorageLimit(config["limits.disk"])',
  'type StorageLimitUnit',
  '<SelectItem value="MB">MB</SelectItem>',
  '<SelectItem value="GB">GB</SelectItem>',
  'if (ramLimitDirty)',
  'if (storageLimitDirty)',
  'Current daemon value:',
]) {
  if (!projectEdit.includes(snippet)) {
    failures.push(`project limit editor is missing safe compatibility contract ${JSON.stringify(snippet)}`);
  }
}
if (/match\(\/\^\(\\d\+[^\n]+GiB\|MiB/.test(projectEdit)) {
  failures.push("project limit editor must not accept only GiB/MiB while silently clearing other LXD units");
}
const projectCreate = readFileSync(join(src, "components", "project-create-form.tsx"), "utf8");
for (const snippet of [
  'maxStorageUnit: "GB"',
  'maxStorageUnitInput === "MB" ? 1_000_000 : 1_000_000_000',
  'watch("maxStorageUnit") === "MB" ? 1_000_000 : 1_000_000_000',
  '<SelectItem value="MB">MB</SelectItem>',
  '<SelectItem value="GB">GB</SelectItem>',
]) {
  if (!projectCreate.includes(snippet)) {
    failures.push(`project create storage is missing decimal SI contract ${JSON.stringify(snippet)}`);
  }
}
const backendProjects = readFileSync(join(root, "backend", "app", "routers", "projects.py"), "utf8");
for (const snippet of [
  '"MB": 10**6',
  '"GB": 10**9',
  '"MIB": 1024**2',
  '"GIB": 1024**3',
  '(("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024))',
  '(("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3))',
  'f"{resources.get(\'cpu_total\', 0)} hardware threads"',
]) {
  if (!backendProjects.includes(snippet)) {
    failures.push(`backend project quotas are missing semantic unit contract ${JSON.stringify(snippet)}`);
  }
}

const backendByteFiles = [
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
  "backend/app/core/host_detail.py",
  "backend/app/services/lxd/node_resources.py",
  "backend/app/services/lxd/resources.py",
];
for (const rel of backendByteFiles) {
  const source = readFileSync(join(root, rel), "utf8");
  const lines = source.split("\n");
  for (const hit of windows(lines, SCALE_1024)) {
    if (SI.test(hit.text) && !IEC.test(hit.text)) {
      failures.push(`${rel}:${hit.line} scales by 1024 beside a decimal SI label`);
    }
  }
  if (/\bdef\s+format_bytes\s*\(/.test(source)) {
    failures.push(`${rel}: defines a private ambiguous format_bytes helper`);
  }
}

const requireFromFrontend = createRequire(resolve(root, "frontend/package.json"));
const ts = requireFromFrontend("typescript");
function loadNumericModule(path, imports) {
  const output = ts.transpileModule(readFileSync(path, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: path,
  }).outputText;
  const loaded = { exports: {} };
  new Function("require", "module", "exports", output)(imports, loaded, loaded.exports);
  return loaded.exports;
}
const numeric = loadNumericModule(utilsPath, requireFromFrontend);
for (const format of [numeric.formatBytes, numeric.formatMemory, numeric.formatLxdLimit, numeric.formatNetworkRate]) {
  for (const value of [NaN, Infinity, -Infinity, -1, null, undefined]) assert.equal(format(value), "\u2014");
  for (const value of [0, 0.5, 1, Number.MIN_VALUE, Number.MAX_VALUE]) {
    assert.doesNotMatch(format(value), /NaN|undefined|Infinity/);
  }
}
assert.equal(numeric.formatBytes(0.5), "0.5 B");
assert.equal(numeric.formatNetworkRate(0.5), "0.5 B/s");
assert.equal(numeric.formatBytes(1000), "1 KB");
assert.equal(numeric.formatMemory(1024), "1 KiB");
assert.equal(numeric.formatBytes(0), "0 B");
assert.equal(numeric.formatBytes(0, { zeroDisplay: "empty" }), "\u2014");
assert.equal(numeric.formatBytes(NaN, { split: true }).unit, "");
for (const decimals of [NaN, Infinity, -1, 300, 1.5]) assert.doesNotThrow(() => numeric.formatBytes(0.5, { decimals }));
for (const format of [numeric.formatFrequency, numeric.formatLinkSpeed, numeric.formatCpuTime]) {
  for (const value of [NaN, Infinity, -Infinity, -1, null, undefined]) assert.equal(format(value), "\u2014");
}
assert.equal(numeric.formatCpuTime(0), "0s");
assert.deepEqual(numeric.formatByteQuota(NaN, NaN), { value: "\u2014", unit: "" });
const tones = loadNumericModule(join(src, "lib", "usage-tone.ts"), requireFromFrontend);
const { capacityReading } = loadNumericModule(join(src, "lib", "capacity-reading.ts"), (name) => {
  if (name === "@/lib/usage-tone") return tones;
  throw new Error(`Unexpected capacity import: ${name}`);
});
for (const input of [null, {}, { total: 100 }, { used: "", total: 100 }, { used: false, total: 100 }, { used: -1, total: 100 }, { used: NaN, total: 100 }, { used: 1, total: Infinity }, { used: 1, total: 0 }]) {
  assert.equal(capacityReading(input), null);
}
assert.deepEqual(capacityReading({ used: 0, total: 0 }), { used: 0, total: 0, percent: 0 });
assert.deepEqual(capacityReading({ used: "25", total: "100" }), { used: 25, total: 100, percent: 25 });
assert.equal(capacityReading({ used: 120, total: 100 }).percent, 100);
const limitsPath = join(src, "lib", "lxd-limits.ts");
const limitsOutput = ts.transpileModule(readFileSync(limitsPath, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  fileName: limitsPath,
}).outputText;
const limitsModule = { exports: {} };
new Function("require", "module", "exports", limitsOutput)(
  (specifier) => {
    if (specifier === "@/lib/utils") return { formatLxdLimit: () => "unused" };
    throw new Error(`Unexpected runtime import in LXD limits: ${specifier}`);
  },
  limitsModule,
  limitsModule.exports,
);
const { editableLxdLimit, editableStorageLimit } = limitsModule.exports;
assert.deepEqual(editableLxdLimit(undefined), { value: 0, unit: "GiB", preservedRaw: null });
assert.deepEqual(editableLxdLimit("8GiB"), { value: 8, unit: "GiB", preservedRaw: "8GiB" });
assert.equal(editableLxdLimit("8GB").preservedRaw, "8GB");
assert.ok(Math.abs(editableLxdLimit("8GB").value - 7.45058059692) < 1e-10);
assert.equal(editableLxdLimit("512MB").preservedRaw, "512MB");
assert.equal(editableLxdLimit("1024").preservedRaw, "1024");
assert.deepEqual(editableLxdLimit("50%"), { value: 0, unit: "GiB", preservedRaw: "50%" });
assert.deepEqual(editableStorageLimit(undefined), { value: 0, unit: "GB", preservedRaw: null });
assert.deepEqual(editableStorageLimit("8GB"), { value: 8, unit: "GB", preservedRaw: "8GB" });
assert.equal(editableStorageLimit("8GiB").preservedRaw, "8GiB");
assert.ok(Math.abs(editableStorageLimit("8GiB").value - 8.589934592) < 1e-10);
assert.deepEqual(editableStorageLimit("512MB"), { value: 512, unit: "MB", preservedRaw: "512MB" });
assert.deepEqual(editableStorageLimit("50%"), { value: 0, unit: "GB", preservedRaw: "50%" });
const tableFilters = readFileSync(join(src, "hooks", "use-table-filters.ts"), "utf8");
if (!tableFilters.includes('if (unit === "bps") return formatNetworkRate(value)')) {
  failures.push("table-filter network bounds must preserve the API's bytes-per-second value");
}
if (/unit === ["']bps["'][\s\S]{0,100}value\s*\/\s*8/.test(tableFilters)) {
  failures.push("table-filter network bounds must not divide the bytes-per-second API value by 8");
}

for (const [needle, label] of requiredUtilsContracts) {
  if (!utils.includes(needle)) failures.push(`frontend/src/lib/utils.ts: missing ${label}`);
}

for (const file of walk(src)) {
  scanned += 1;
  const rel = relative(src, file).split(sep).join("/");
  const raw = readFileSync(file, "utf8");
  const source = stripComments(raw);
  const displayPath = `frontend/src/${rel}`;

  if (/formatBytes\([^\n;]{0,180}\)\s*\}?\s*\/s\b/.test(source)) {
    failures.push(`${displayPath}: formats a rate with formatBytes(...)/s; use formatNetworkRate`);
  }

  for (const hit of crossedFormatters(source)) {
    failures.push(`${displayPath}:${hit.line} ${hit.why}; use the matching semantic formatter`);
  }

  if (rel !== "lib/utils.ts" && !PRIVATE_FORMATTER_ALLOWLIST.has(rel)) {
    PRIVATE_DISPLAY_FORMATTER.lastIndex = 0;
    let match;
    while ((match = PRIVATE_DISPLAY_FORMATTER.exec(source)) !== null) {
      failures.push(
        `${displayPath}:${lineOf(source, match.index)} defines private ${match[1]}; use frontend/src/lib/utils.ts`,
      );
    }
  }

  const lines = source.split("\n");
  for (const hit of windows(lines, SCALE_1024)) {
    if (SI.test(hit.text) && !IEC.test(hit.text)) {
      failures.push(`${displayPath}:${hit.line} scales by 1024 beside a decimal SI label`);
    }
  }
  for (const hit of windows(lines, SCALE_1000)) {
    if (IEC.test(hit.text) && !SI.test(hit.text)) {
      failures.push(`${displayPath}:${hit.line} scales by 1000 beside a binary IEC label`);
    }
  }
}

if (scanned < 200) failures.push(`only scanned ${scanned} TypeScript files; expected the full frontend tree`);

if (failures.length) {
  console.error("byte units: FAILED");
  for (const failure of failures) console.error(`  ${failure}`);
  console.error("  Use formatBytes/formatByteQuota for decimal storage and network values.");
  console.error("  Use formatMemory/formatMemoryQuota for binary memory, swap, and VRAM values.");
  process.exit(1);
}

console.log(`byte units: ${scanned} files follow the SI storage/network and IEC memory contract`);
