#!/usr/bin/env node
/**
 * Guard the console's byte contract: one standard, the GB family, on every screen.
 *
 * One disk used to read 10GiB in a column and 10.74 GB on its meter, because memory and limits were
 * binary and storage was decimal. documentation/ui/byte-units.md has the decision. Binary labels
 * survive only where they are wire format: parsers that read what LXD stores. Keeping this
 * parser-free lets the CI guards job run before frontend dependencies are installed.
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

const SI = /\b(?:kB|MB|GB|TB|PB)(?:\/s)?\b/;
const IEC = /\b(?:KiB|MiB|GiB|TiB|PiB)(?:\/s)?\b/;
// A number or an interpolation stuck to a unit: `8GB`, `${size}GB`. A wire join writes
// `${value}${unit}`, which has no literal unit in it.
const GLUED = /(?:\b\d+(?:\.\d+)?|\})(?:kB|MB|GB|TB|PB)(?:\/s)?\b/;
const SCALE_1024 = /Math\.pow\(\s*1024|1024\s*\*\*|\/\s*1024(?:\s*\/\s*1024)+|\/\s*\(1024\s*\*|\bk\s*=\s*1024\b/;

const PRIVATE_DISPLAY_FORMATTER =
  /\b(?:function\s+|const\s+)(format(?:Bytes|Memory|Size|Rate|NetworkSpeed|BytesAuto|Quota))\b/g;
const PRIVATE_FORMATTER_ALLOWLIST = new Set([
  // Compatibility re-exports only. The implementation delegates to the shared helpers.
  "components/member-resources/format.ts",
]);

// Where a binary label is a value on the wire, or read off it. Nothing here reaches a screen as text.
const WIRE_FILES = new Set([
  "lib/lxd-limits.ts", // parseLxdSize reads whatever LXD stores
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

const failures = [];
let scanned = 0;

const requiredUtilsContracts = [
  ['const SI_BYTE_UNITS = ["B", "kB", "MB", "GB", "TB", "PB"]', "the one unit list"],
  ["scaleBytes(bytes, 1000, SI_BYTE_UNITS", "the one size formatter"],
  ["scaleBytes(bytesPerSecond, 1000, SI_BYTE_UNITS", "the rate formatter on the same scale"],
  ["scaleByteQuota(usedBytes, limitBytes, 1000, SI_BYTE_UNITS", "the quota formatter on the same scale"],
];

for (const retired of ["formatMemory", "formatMemoryQuota", "formatLxdLimit", "IEC_BYTE_UNITS"]) {
  if (utils.includes(retired)) {
    failures.push(`frontend/src/lib/utils.ts: ${retired} is back, and with it a second byte standard`);
  }
}

const fileLimits = readFileSync(
  join(root, "backend", "app", "core", "config.py"),
  "utf8",
);
if (!fileLimits.includes("default=20_000_000_000,")) {
  failures.push("the file explorer transfer limit must stay in decimal bytes, matching its label");
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
  'editableLimit(config["limits.memory"])',
  'editableLimit(config["limits.disk"])',
  'FORM_SIZE_UNITS.map(',
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
  'maxRamUnit: "GB"',
  'maxStorageUnit: "GB"',
  'FORM_UNIT_BYTES[maxRamUnitInput as FormSizeUnit]',
  'FORM_UNIT_BYTES[maxStorageUnitInput as FormSizeUnit]',
  'FORM_SIZE_UNITS.map(',
]) {
  if (!projectCreate.includes(snippet)) {
    failures.push(`project create is missing the form unit contract ${JSON.stringify(snippet)}`);
  }
}
const backendProjects = readFileSync(join(root, "backend", "app", "routers", "projects.py"), "utf8");
for (const snippet of [
  '"MB": 10**6',
  '"GB": 10**9',
  '"MIB": 1024**2',
  '"GIB": 1024**3',
  'format_size(ram_bytes)',
  'f"{resources.get(\'cpu_total\', 0)} hardware threads"',
]) {
  if (!backendProjects.includes(snippet)) {
    failures.push(`backend project quotas are missing unit contract ${JSON.stringify(snippet)}`);
  }
}

// Backend text a person reads goes through one function. A parser or a wire example is not display.
const DISPLAY_PRINT = /f"[^"\n]*\{[^}\n]*\}\s*(?:KiB|MiB|GiB|TiB)\b/;
function* walkPython(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* walkPython(path);
    else if (name.endsWith(".py")) yield path;
  }
}
for (const file of walkPython(join(root, "backend", "app"))) {
  const lines = readFileSync(file, "utf8").split("\n");
  lines.forEach((text, index) => {
    if (!DISPLAY_PRINT.test(text) || /logger\.|^\s*#/.test(text)) return;
    // A log call spread over lines carries its arguments below `logger.x(`.
    if (lines.slice(Math.max(0, index - 12), index).some((above) => /logger\.\w+\(/.test(above))) return;
    failures.push(`${relative(root, file)}:${index + 1} prints a binary unit by hand; use format_size`);
  });
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
  "backend/app/services/monitoring_memory.py",
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
for (const format of [numeric.formatBytes, numeric.formatNetworkRate]) {
  for (const value of [NaN, Infinity, -Infinity, -1, null, undefined]) assert.equal(format(value), "\u2014");
  for (const value of [0, 0.5, 1, Number.MIN_VALUE, Number.MAX_VALUE]) {
    assert.doesNotMatch(format(value), /NaN|undefined|Infinity/);
  }
}
assert.equal(numeric.formatBytes(0.5), "0.5 B");
assert.equal(numeric.formatNetworkRate(0.5), "0.5 B/s");
assert.equal(numeric.formatBytes(1000), "1 kB");
assert.equal(numeric.formatBytes(8589934592), "8.59 GB");
assert.equal(numeric.formatNetworkRate(12500), "12.5 kB/s");
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
const limits = loadNumericModule(join(src, "lib", "lxd-limits.ts"), (specifier) => {
  if (specifier === "@/lib/utils") return numeric;
  throw new Error(`Unexpected runtime import in LXD limits: ${specifier}`);
});
const { editableLimit, formatLxdSize, formatConfigValue } = limits;
assert.deepEqual(editableLimit(undefined), { value: 0, unit: "GB", preservedRaw: null });
assert.deepEqual(editableLimit("8GB"), { value: 8, unit: "GB", preservedRaw: "8GB" });
assert.deepEqual(editableLimit("512MB"), { value: 512, unit: "MB", preservedRaw: "512MB" });
assert.deepEqual(editableLimit("2TB"), { value: 2, unit: "TB", preservedRaw: "2TB" });
assert.deepEqual(editableLimit("50%"), { value: 0, unit: "GB", preservedRaw: "50%" });
assert.equal(editableLimit("8GiB").preservedRaw, "8GiB", "an untouched binary limit stays as stored");
assert.ok(Math.abs(editableLimit("8GiB").value - 8.589934592) < 1e-10);
assert.equal(editableLimit("1024").preservedRaw, "1024");
assert.equal(formatLxdSize("8GiB"), "8.59 GB");
assert.equal(formatLxdSize("10GB"), "10 GB");
assert.equal(formatLxdSize("8589934592B"), "8.59 GB", "a spec edit stores bare bytes");
assert.equal(formatLxdSize("512 MiB"), "536.87 MB");
assert.equal(formatLxdSize("50%"), "50%");
assert.equal(formatLxdSize("not a size"), "not a size");
assert.equal(formatLxdSize(undefined), "");
assert.equal(formatConfigValue("limits.memory", "8GiB"), "8.59 GB");
assert.equal(formatConfigValue("limits.cpu", "4"), "4", "a count is not bytes");
assert.equal(formatConfigValue("limits.read", "30iops"), "30iops");
assert.equal(formatConfigValue("user.note", "8GiB"), "8GiB", "an authored value is left alone");
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
    if (SI.test(hit.text) && !WIRE_FILES.has(rel)) {
      failures.push(`${displayPath}:${hit.line} scales by 1024 beside a decimal label`);
    }
  }
  if (!WIRE_FILES.has(rel)) {
    lines.forEach((text, index) => {
      if (IEC.test(text)) failures.push(`${displayPath}:${index + 1} shows a binary unit; use formatBytes`);
      if (GLUED.test(text)) failures.push(`${displayPath}:${index + 1} glues a value to its unit`);
    });
  }
}

if (scanned < 200) failures.push(`only scanned ${scanned} TypeScript files; expected the full frontend tree`);

if (failures.length) {
  console.error("byte units: FAILED");
  for (const failure of failures) console.error(`  ${failure}`);
  console.error("  Every size on screen goes through formatBytes, formatByteQuota or formatLxdSize.");
  process.exit(1);
}

console.log(`byte units: ${scanned} files print one standard, the GB family`);
