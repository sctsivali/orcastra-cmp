#!/usr/bin/env node
/**
 * Guard the console's byte contract: one system on every screen, the one the account chose.
 *
 * An account reads decimal (kB, MB, GB) or binary (KiB, MiB, GiB). Mixing the two on one screen
 * makes one disk read 10GiB in a column and 10.74 GB on its meter, so every size goes through the
 * shared formatters, bound to the account's system by useByteFormat(). The unit tables live in
 * lib/byte-units.ts and nowhere else; a raw formatter imported by a component would print decimal
 * whatever the account chose. Keeping this parser-free lets the CI guards job run before frontend
 * dependencies are installed.
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

// Where a binary label may be written: the one unit table, and the parser that reads what LXD stores.
const WIRE_FILES = new Set([
  "lib/byte-units.ts", // the decimal and binary unit tables every formatter reads
  "lib/lxd-limits.ts", // parseLxdSize reads whatever LXD stores
]);

// Modules that may import the raw formatters, because they take the system as an argument and hand
// it on. Everything else gets them bound to the account's system from useByteFormat().
const RAW_FORMATTER_MODULES = new Set([
  "lib/utils.ts",
  "lib/lxd-limits.ts",
  "lib/byte-format.ts",
  "components/member-resources/format.ts",
]);
const RAW_FORMATTER_IMPORTS = [
  { from: "@/lib/utils", names: ["formatBytes", "formatByteQuota", "formatNetworkRate"] },
  { from: "@/lib/lxd-limits", names: ["formatLxdSize", "formatConfigValue", "editableLimit"] },
];

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

const byteUnitsSource = readFileSync(join(src, "lib", "byte-units.ts"), "utf8");
const requiredUtilsContracts = [
  ["const scale = BYTE_SCALES[toByteUnitSystem(typeof options === \"object\" ? options.units : undefined)];", "the one size formatter, on the account's scale"],
  ["scaleBytes(bytesPerSecond, scale.base, scale.units", "the rate formatter on the same scale"],
  ["scaleByteQuota(usedBytes, limitBytes, scale.base, scale.units", "the quota formatter on the same scale"],
];
for (const [needle, label] of [
  ['decimal: { base: 1000, units: ["B", "kB", "MB", "GB", "TB", "PB"] }', "the decimal unit table"],
  ['binary: { base: 1024, units: ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] }', "the binary unit table"],
  ['export const DEFAULT_BYTE_UNITS: ByteUnitSystem = "binary";', "binary as the default"],
]) {
  if (!byteUnitsSource.includes(needle)) failures.push(`frontend/src/lib/byte-units.ts: missing ${label}`);
}
if (/["'](?:kB|MB|GB|TB|PB|KiB|MiB|GiB|TiB|PiB)["']/.test(stripComments(utils))) {
  failures.push("frontend/src/lib/utils.ts: a unit list is written here; the tables live in lib/byte-units.ts");
}

for (const retired of ["formatMemory", "formatMemoryQuota", "formatLxdLimit", "IEC_BYTE_UNITS", "SI_BYTE_UNITS"]) {
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
  'FORM_SIZE_UNITS[formUnitSystem(editMaxRamUnit)].map(',
  'FORM_SIZE_UNITS[formUnitSystem(editMaxStorageUnit)].map(',
  'byteFormat(unitsRef.current).editableLimit(',
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
  'maxRamUnit: defaultFormUnit',
  'maxStorageUnit: defaultFormUnit',
  'FORM_UNIT_BYTES[maxRamUnitInput]',
  'FORM_UNIT_BYTES[maxStorageUnitInput]',
  'FORM_SIZE_UNITS[formUnitSystem(watch("maxRamUnit"))].map(',
  'FORM_SIZE_UNITS[formUnitSystem(watch("maxStorageUnit"))].map(',
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
  'f"{resources.get(\'cpu_total\', 0)} CPU threads"',
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
const byteUnits = loadNumericModule(join(src, "lib", "byte-units.ts"), (specifier) => {
  throw new Error(`Unexpected runtime import in byte units: ${specifier}`);
});
const numeric = loadNumericModule(utilsPath, (specifier) =>
  specifier === "@/lib/byte-units" ? byteUnits : requireFromFrontend(specifier),
);
for (const format of [numeric.formatBytes, numeric.formatNetworkRate]) {
  for (const value of [NaN, Infinity, -Infinity, -1, null, undefined]) assert.equal(format(value), "\u2014");
  for (const value of [0, 0.5, 1, Number.MIN_VALUE, Number.MAX_VALUE]) {
    assert.doesNotMatch(format(value), /NaN|undefined|Infinity/);
  }
}
assert.equal(numeric.formatBytes(0.5), "0.5 B");
assert.equal(numeric.formatNetworkRate(0.5), "0.5 B/s");
assert.equal(numeric.formatBytes(1000, { units: "decimal" }), "1 kB");
assert.equal(numeric.formatBytes(8589934592, { units: "decimal" }), "8.59 GB");
assert.equal(numeric.formatBytes(8589934592), "8 GiB", "no system given reads the default, binary");
assert.equal(numeric.formatNetworkRate(12500, 2, "decimal"), "12.5 kB/s");
assert.equal(numeric.formatBytes(0), "0 B");
assert.equal(numeric.formatBytes(0, { zeroDisplay: "empty" }), "\u2014");
assert.equal(numeric.formatBytes(NaN, { split: true }).unit, "");
for (const decimals of [NaN, Infinity, -1, 300, 1.5]) assert.doesNotThrow(() => numeric.formatBytes(0.5, { decimals }));
for (const format of [numeric.formatFrequency, numeric.formatLinkSpeed, numeric.formatCpuTime, numeric.formatElapsed]) {
  for (const value of [NaN, Infinity, -Infinity, -1, null, undefined]) assert.equal(format(value), "\u2014");
}
assert.equal(numeric.formatCpuTime(0), "0s");
// A single time is glued and a compound keeps its two largest units, never "0m 5s" or "8h 46m 10s".
for (const [seconds, text] of [[0, "0s"], [0.4, "0s"], [5, "5s"], [125, "2m 5s"], [31570, "8h 46m"], [4330000, "50d 2h"]]) {
  assert.equal(numeric.formatElapsed(seconds), text);
}
assert.equal(numeric.formatCpuTime(31570e9), numeric.formatElapsed(31570));
assert.deepEqual(numeric.formatByteQuota(NaN, NaN), { value: "\u2014", unit: "" });
// The binary system: same rules, base 1024, IEC labels.
const binary = { units: "binary" };
assert.equal(numeric.formatBytes(8589934592, binary), "8 GiB");
assert.equal(numeric.formatBytes(1024, binary), "1 KiB");
assert.equal(numeric.formatBytes(1000, binary), "1000 B");
assert.equal(numeric.formatBytes(1536 * 1024 ** 2, binary), "1.5 GiB");
assert.equal(numeric.formatBytes(0, binary), "0 B");
assert.equal(numeric.formatBytes(0, { ...binary, zeroDisplay: "empty" }), "\u2014");
assert.equal(numeric.formatBytes(NaN, binary), "\u2014");
assert.deepEqual(numeric.formatBytes(4294967296, { ...binary, split: true }), { value: "4", unit: "GiB" });
assert.equal(numeric.formatBytes(8589934592, { units: "decimal" }), "8.59 GB");
assert.equal(numeric.formatBytes(8589934592, { units: "bogus" }), "8 GiB", "an unknown system reads the default, binary");
assert.equal(numeric.formatNetworkRate(12800, 2, "binary"), "12.5 KiB/s");
assert.equal(numeric.formatNetworkRate(12500, 2, "decimal"), "12.5 kB/s");
assert.deepEqual(numeric.formatByteQuota(2 * 1024 ** 3, 8 * 1024 ** 3, 1, "binary"), { value: "2 of 8", unit: "GiB" });
assert.deepEqual(numeric.formatByteQuota(2e9, 8e9, 1, "decimal"), { value: "2 of 8", unit: "GB" });
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
  if (specifier === "@/lib/byte-units") return byteUnits;
  throw new Error(`Unexpected runtime import in LXD limits: ${specifier}`);
});
const { editableLimit, formatLxdSize, formatConfigValue } = limits;
assert.deepEqual(editableLimit(undefined), { value: 0, unit: "GiB", preservedRaw: null }, "a new limit starts in the default, GiB");
assert.equal(formatLxdSize("8GiB"), "8 GiB", "no system given reads the default, binary");
assert.deepEqual(editableLimit(undefined, "decimal"), { value: 0, unit: "GB", preservedRaw: null });
assert.deepEqual(editableLimit("8GB", "decimal"), { value: 8, unit: "GB", preservedRaw: "8GB" });
assert.deepEqual(editableLimit("512MB", "decimal"), { value: 512, unit: "MB", preservedRaw: "512MB" });
assert.deepEqual(editableLimit("2TB", "decimal"), { value: 2, unit: "TB", preservedRaw: "2TB" });
assert.deepEqual(editableLimit("50%", "decimal"), { value: 0, unit: "GB", preservedRaw: "50%" });
assert.equal(editableLimit("8GiB", "decimal").preservedRaw, "8GiB", "an untouched binary limit stays as stored");
assert.ok(Math.abs(editableLimit("8GiB", "decimal").value - 8.589934592) < 1e-10);
assert.equal(editableLimit("1024", "decimal").preservedRaw, "1024");
assert.equal(formatLxdSize("8GiB", "decimal"), "8.59 GB");
assert.equal(formatLxdSize("10GB", "decimal"), "10 GB");
assert.equal(formatLxdSize("8589934592B", "decimal"), "8.59 GB", "a spec edit stores bare bytes");
assert.equal(formatLxdSize("512 MiB", "decimal"), "536.87 MB");
assert.equal(formatLxdSize("50%", "decimal"), "50%");
assert.equal(formatLxdSize("not a size", "decimal"), "not a size");
assert.equal(formatLxdSize(undefined, "decimal"), "");
assert.equal(formatConfigValue("limits.memory", "8GiB", "decimal"), "8.59 GB");
assert.equal(formatConfigValue("limits.cpu", "4", "decimal"), "4", "a count is not bytes");
assert.equal(formatConfigValue("limits.read", "30iops", "decimal"), "30iops");
assert.equal(formatConfigValue("user.note", "8GiB", "decimal"), "8GiB", "an authored value is left alone");
// The binary system reads a binary limit exactly and keeps the stored spelling until the field changes.
assert.deepEqual(editableLimit(undefined, "binary"), { value: 0, unit: "GiB", preservedRaw: null });
assert.deepEqual(editableLimit("8GiB", "binary"), { value: 8, unit: "GiB", preservedRaw: "8GiB" });
assert.deepEqual(editableLimit("512MiB", "binary"), { value: 512, unit: "MiB", preservedRaw: "512MiB" });
assert.deepEqual(editableLimit("2TiB", "binary"), { value: 2, unit: "TiB", preservedRaw: "2TiB" });
assert.deepEqual(editableLimit("50%", "binary"), { value: 0, unit: "GiB", preservedRaw: "50%" });
assert.equal(editableLimit("8GB", "binary").preservedRaw, "8GB", "an untouched decimal limit stays as stored");
assert.ok(Math.abs(editableLimit("8GB", "binary").value - 7.450580596923828) < 1e-9);
assert.equal(formatLxdSize("8GiB", "binary"), "8 GiB");
assert.equal(formatLxdSize("512 MiB", "binary"), "512 MiB");
assert.equal(formatLxdSize("10GB", "binary"), "9.31 GiB");
assert.equal(formatLxdSize("50%", "binary"), "50%");
assert.equal(formatConfigValue("limits.memory", "8GiB", "binary"), "8 GiB");
assert.equal(formatConfigValue("limits.cpu", "4", "binary"), "4");
const bound = loadNumericModule(join(src, "lib", "byte-format.ts"), (specifier) => {
  if (specifier === "@/lib/utils") return numeric;
  if (specifier === "@/lib/byte-units") return byteUnits;
  if (specifier === "@/lib/lxd-limits") return limits;
  throw new Error(`Unexpected runtime import in byte format: ${specifier}`);
});
assert.equal(bound.byteFormat("binary"), bound.byteFormat("binary"), "one bound object per system");
assert.equal(bound.byteFormat("nonsense"), bound.byteFormat("binary"), "an unknown system reads the default, binary");

// One default, written in three places that cannot import each other at runtime: the unit table,
// the request transport (type-only import) and the backend's personal view.
const defaultOf = (path, pattern) => pattern.exec(readFileSync(join(root, path), "utf8"))?.[1];
const defaults = {
  "frontend/src/lib/byte-units.ts": defaultOf("frontend/src/lib/byte-units.ts", /DEFAULT_BYTE_UNITS: ByteUnitSystem = "(\w+)"/),
  "frontend/src/lib/api.ts": defaultOf("frontend/src/lib/api.ts", /let _byteUnits: ByteUnitSystem = "(\w+)"/),
  "backend/app/routers/user_preferences.py": defaultOf("backend/app/routers/user_preferences.py", /^DEFAULT_BYTE_UNITS = "(\w+)"/m),
};
assert.deepEqual(new Set(Object.values(defaults)), new Set(["binary"]), `the byte units default differs: ${JSON.stringify(defaults)}`);
assert.equal(bound.byteFormat("binary").formatBytes(8589934592), "8 GiB");
assert.equal(bound.byteFormat("binary").formatBytes(1536, 0), "2 KiB", "a number argument is still the decimals");
assert.deepEqual(bound.byteFormat("binary").formatBytes(2048, { split: true }), { value: "2", unit: "KiB" });
assert.equal(bound.byteFormat("decimal").formatNetworkRate(12500), "12.5 kB/s");
assert.equal(bound.byteFormat("binary").formatNetworkRate(12800), "12.5 KiB/s");
assert.deepEqual([...bound.byteFormat("binary").formUnits], ["MiB", "GiB", "TiB"]);
assert.deepEqual([...bound.byteFormat("decimal").formUnits], ["MB", "GB", "TB"]);
assert.equal(bound.byteFormat("binary").defaultFormUnit, "GiB");
assert.equal(bound.byteFormat("binary").formatLxdSize("4GiB"), "4 GiB");
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

  if (!RAW_FORMATTER_MODULES.has(rel)) {
    for (const { from, names } of RAW_FORMATTER_IMPORTS) {
      const pattern = new RegExp(`import\\s*(?:type\\s*)?\\{([^}]*)\\}\\s*from\\s*["']${from.replace(/[/@.-]/g, "\\$&")}["']`, "g");
      let imported;
      while ((imported = pattern.exec(source)) !== null) {
        const bound = imported[1].split(",").map((part) => part.trim().split(/\s+as\s+/)[0].replace(/^type\s+/, ""));
        for (const name of names.filter((candidate) => bound.includes(candidate))) {
          failures.push(
            `${displayPath}:${lineOf(source, imported.index)} imports ${name} from ${from}, which prints decimal whatever the account chose; use useByteFormat()`,
          );
        }
      }
    }
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
  console.error("  Every size on screen goes through useByteFormat(): formatBytes, formatByteQuota, formatNetworkRate or formatLxdSize.");
  process.exit(1);
}

console.log(`byte units: ${scanned} files print sizes in the account's one system`);
