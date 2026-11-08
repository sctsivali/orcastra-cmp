#!/usr/bin/env node
/**
 * Structural guard: a number a reader compares is not set in monospace.
 *
 * The Sessions page set its total time and its transfer rates in Geist Mono, its counts in the
 * body face, at three sizes in one strip, while every other stat tile and every other table set
 * sizes, rates, counts and durations in the body face. Monospace is for identifiers (ids, hashes,
 * fingerprints, keys, paths, raw config); tabular figures (`tabular-nums`) are what keep digits
 * aligned without changing the face. documentation/ui/table-conventions.md, "The typography
 * contract".
 *
 * Two rules, both over JSX with a literal className:
 *   1. `font-mono` together with a display size (`text-lg` and up) is a headline number or
 *      label in monospace. Nothing in the console needs one.
 *   2. `font-mono` on an element whose content calls one of the shared number formatters below.
 *      The content is read up to the element's first closing tag of the same name, so a value
 *      two levels inside a mono wrapper of the same tag name can be missed. Template-literal and
 *      cn(...) classNames are skipped and counted, so that blind spot shows in the output.
 *
 * Modelled on scripts/checks/check-table-typography.mjs.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../../frontend/src", import.meta.url).pathname;

const NUMBER_FORMATTERS = [
  "formatBytes",
  "formatByteQuota",
  "formatNetworkRate",
  "formatCount",
  "formatCpuTime",
  "formatElapsed",
  "formatCpuThreads",
  "formatFrequency",
  "formatLinkSpeed",
  "formatVcpuCount",
  "formatVcpuAmount",
  "formatVcpuBusy",
  "formatSampleWindow",
];

// Each entry needs its reason. Terminal chrome is the one place monospace is the face of the
// whole surface, numbers included.
const ALLOWED_FILES = new Map([
  [
    "components/console/resource-bar.tsx",
    "the console's status line: terminal chrome, set in the terminal's face like the session around it",
  ],
  [
    "components/console/instance-resources.tsx",
    "the resource chips and panel the console status line renders: the same fixed-dark terminal chrome",
  ],
]);

const DISPLAY_SIZE = /(^|\s)(?:[a-z]+:)?text-(?:lg|xl|[2-9]xl)(?=\s|$)/;
const MONO = /(^|\s)font-mono(?=\s|$)/;
const FORMATTER_CALL = new RegExp(`\\b(?:${NUMBER_FORMATTERS.join("|")})\\(`);

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "node_modules" || entry === ".next") continue;
      walk(full, out);
    } else if (entry.endsWith(".tsx")) {
      out.push(full);
    }
  }
  return out;
}

const violations = [];
let inspected = 0;
let skipped = 0;

for (const file of walk(ROOT)) {
  const rel = relative(ROOT, file);
  if (ALLOWED_FILES.has(rel)) continue;
  const src = readFileSync(file, "utf8");
  const lineOf = (index) => src.slice(0, index).split("\n").length;

  for (const m of src.matchAll(/<([A-Za-z][\w.]*)\b[^<>]*?className="([^"]*)"[^<>]*?>/gs)) {
    const [opening, tag, cls] = m;
    if (!MONO.test(cls)) continue;
    inspected += 1;
    const line = lineOf(m.index);
    if (DISPLAY_SIZE.test(cls)) {
      violations.push({ file: rel, line, why: `font-mono at a display size: "${cls}"` });
      continue;
    }
    if (opening.endsWith("/>")) continue;
    const start = m.index + opening.length;
    const close = src.indexOf(`</${tag}>`, start);
    const content = src.slice(start, close === -1 ? start : close);
    const call = content.match(FORMATTER_CALL);
    if (call) {
      violations.push({ file: rel, line, why: `font-mono around ${call[0]}...) in <${tag}>` });
    }
  }
  for (const m of src.matchAll(/className=\{[^}]*font-mono/g)) {
    void m;
    skipped += 1;
  }
}

console.log(
  `number typography: ${inspected} literal font-mono elements inspected, ${skipped} dynamic ` +
    `classNames skipped, ${violations.length} violations, ${ALLOWED_FILES.size} allowed file(s)`
);

if (violations.length) {
  console.log(
    "\nA count, size, rate or duration is set in the body face with tabular-nums, not in " +
      "monospace. Monospace is for identifiers (documentation/ui/table-conventions.md):"
  );
  for (const v of violations) console.log(`  ${v.file}:${v.line}  ${v.why}`);
  process.exit(1);
}
