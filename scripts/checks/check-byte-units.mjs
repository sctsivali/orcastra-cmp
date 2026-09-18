#!/usr/bin/env node
/**
 * A byte size must be scaled the way its label says.
 *
 * Fourteen formatters in this console divided by 1024 and printed "GB", so a 1050 GB pool read
 * as 977.89 GB and no screen agreed with LXD, the disk, or the API. They were unified onto
 * lib/utils.formatBytes (decimal, SI labels) and lib/utils.formatMemory (binary, IEC labels).
 *
 * The rule this enforces: a scaling expression over 1024 may not sit in the same function as an
 * SI unit label, and a scaling expression over 1000 may not sit in the same function as an IEC
 * label. Anything a user types with an explicit unit is out of scope, because the LXD API
 * defines those in binary; those sites label themselves GiB and are therefore already correct.
 *
 * documentation/ui/byte-units.md carries the reasoning.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = "frontend/src";
const SI = /(["'`])(?:KB|MB|GB|TB|PB)\1|\b(?:KB|MB|GB|TB|PB)\b/;
const IEC = /(["'`])(?:KiB|MiB|GiB|TiB|PiB)\1|\b(?:KiB|MiB|GiB|TiB|PiB)\b/;
const SCALE_1024 = /Math\.pow\(\s*1024|1024\s*\*\*|\/\s*1024(?:\s*\/\s*1024)+|k\s*=\s*1024\b/;
const SCALE_1000 = /Math\.pow\(\s*1000|1000\s*\*\*|\/\s*1000(?:\s*\/\s*1000)+|k\s*=\s*1000\b/;

// Blocks that legitimately pair binary maths with a binary label, or that are constants rather
// than display. Keyed by file, valued by the count of allowed windows.
const BASELINE = {
  "frontend/src/components/dashboard/project-card.tsx": 1, // formatQuota: GiB in, GiB out
  "frontend/src/components/project-create-form.tsx": 2, // the form's own GiB/MiB inputs
};

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* walk(p);
    else if (/\.tsx?$/.test(name)) yield p;
  }
}

/** Windows of 14 lines around each scaling expression, which is where its label lives. */
function windows(lines, re) {
  const out = [];
  lines.forEach((line, i) => {
    if (re.test(line)) {
      out.push({ line: i + 1, text: lines.slice(Math.max(0, i - 7), i + 7).join("\n") });
    }
  });
  return out;
}

/**
 * The second rule, and the one the first cannot see: a memory value handed to the decimal
 * formatter, or a size on disk handed to the binary one. No literal 1024 appears, so the
 * window scan above is blind to it. This shipped twice while the two functions were being
 * split, once as "26.84 GiB / 70.87 GB" in a single meter, because the call spanned lines.
 */
const MEMORY_ARG = /\b(memory|Memory|ram|RAM|swap|Swap)\w*/;
const STORAGE_ARG = /\b(storage|Storage|disk|Disk|space_|pool)\w*/;

function crossedFormatters(source) {
  const hits = [];
  const call = /\b(formatBytes|formatMemory|formatBytesCompact|formatMemoryCompact)\(\s*([^)]{0,140}?)\s*[,)]/gs;
  let m;
  while ((m = call.exec(source)) !== null) {
    const [, fn, arg] = m;
    const line = source.slice(0, m.index).split("\n").length;
    const decimal = fn === "formatBytes" || fn === "formatBytesCompact";
    if (decimal && MEMORY_ARG.test(arg) && !STORAGE_ARG.test(arg)) {
      hits.push({ line, why: `${fn} on a memory value; use formatMemory` });
    }
    if (!decimal && STORAGE_ARG.test(arg) && !MEMORY_ARG.test(arg)) {
      hits.push({ line, why: `${fn} on a size on disk; use formatBytes` });
    }
  }
  return hits;
}

const offenders = [];
const counts = {};

for (const file of walk(ROOT)) {
  const source = readFileSync(file, "utf8");
  const lines = source.split("\n");
  for (const hit of crossedFormatters(source)) {
    counts[file] = (counts[file] || 0) + 1;
    offenders.push(`${file}:${hit.line} ${hit.why}`);
  }
  for (const w of windows(lines, SCALE_1024)) {
    if (SI.test(w.text) && !IEC.test(w.text)) {
      counts[file] = (counts[file] || 0) + 1;
      offenders.push(`${file}:${w.line} scales by 1024 next to an SI label`);
    }
  }
  for (const w of windows(lines, SCALE_1000)) {
    if (IEC.test(w.text) && !SI.test(w.text)) {
      counts[file] = (counts[file] || 0) + 1;
      offenders.push(`${file}:${w.line} scales by 1000 next to an IEC label`);
    }
  }
}

const regressions = offenders.filter((o) => {
  const file = o.split(":")[0];
  return (counts[file] || 0) > (BASELINE[file] || 0);
});

const total = Object.values(counts).reduce((a, b) => a + b, 0);
const allowed = Object.values(BASELINE).reduce((a, b) => a + b, 0);
console.log(`byte units: ${total} binary-or-decimal mismatch window(s), ${allowed} allowed by baseline`);

if (regressions.length) {
  console.error("byte units: a size must be scaled the way its label says");
  for (const r of regressions) console.error(`  ${r}`);
  console.error("  Use formatBytes (decimal, KB/MB/GB) or formatMemory (binary, KiB/MiB/GiB)");
  console.error("  from lib/utils. See documentation/ui/byte-units.md");
  process.exit(1);
}

console.log("byte units: every scaling expression matches the unit it prints");
