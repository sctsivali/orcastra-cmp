#!/usr/bin/env node
/**
 * Structural guard for dash typography in prose.
 *
 * tsc and eslint do not read English, so nothing else catches an em dash, en dash or
 * horizontal bar used as punctuation in frontend/src, backend/app or README.md. A dash is
 * legal only as the entire string literal: the EMPTY_VALUE placeholder (lib/utils.ts,
 * re-exported by components/ui/table-text.tsx) for a value the API did not return, which
 * some call sites spell as the bare glyph.
 *
 * Advisory: it reports and exits 0. Flip HARD_FAIL to true to make it a gate.
 */
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { join, relative } from "node:path";

const REPO = new URL("../..", import.meta.url).pathname;

const HARD_FAIL = false;

// This file has to name the characters it bans, so ROOTS below leaves out scripts/. The
// constants are escapes so the operative code carries none.
const EM_DASH = "\u2014";
const EN_DASH = "\u2013";
const HORIZONTAL_BAR = "\u2015";

const BANNED = [
  { char: EM_DASH, name: "em dash", code: "U+2014" },
  { char: EN_DASH, name: "en dash", code: "U+2013" },
  { char: HORIZONTAL_BAR, name: "horizontal bar", code: "U+2015" },
];

const ROOTS = [
  { dir: "frontend/src", exts: [".ts", ".tsx"] },
  { dir: "backend/app", exts: [".py"] },
];
const FILES = ["README.md"];

const SKIP_DIRS = new Set(["node_modules", ".next", ".git", "__pycache__", "dist", "build"]);

// The empty-value placeholder: any of the three characters alone between matching quotes.
const STANDALONE = new RegExp(
  `(["'\`])[${EM_DASH}${EN_DASH}${HORIZONTAL_BAR}]\\1`,
  "g",
);

function walk(dir, exts, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (SKIP_DIRS.has(entry)) continue;
      walk(full, exts, out);
    } else if (exts.some((e) => entry.endsWith(e))) {
      out.push(full);
    }
  }
  return out;
}

const targets = [];
for (const { dir, exts } of ROOTS) {
  const full = join(REPO, dir);
  if (existsSync(full)) targets.push(...walk(full, exts));
}
for (const f of FILES) {
  const full = join(REPO, f);
  if (existsSync(full)) targets.push(full);
}

const violations = [];
let scanned = 0;
let placeholders = 0;

for (const file of targets) {
  scanned += 1;
  const lines = readFileSync(file, "utf8").split("\n");
  lines.forEach((line, i) => {
    // Drop the sanctioned placeholder form first, then judge what is left.
    const stripped = line.replace(STANDALONE, (m) => {
      placeholders += 1;
      return "";
    });
    for (const { char, name, code } of BANNED) {
      if (!stripped.includes(char)) continue;
      violations.push({
        where: `${relative(REPO, file)}:${i + 1}`,
        name,
        code,
        text: line.trim().slice(0, 110),
      });
      break;
    }
  });
}

console.log(
  `prose typography: ${scanned} files scanned, ${placeholders} standalone placeholder glyph(s) allowed, ` +
    `${violations.length} prose dash(es) found`
);

if (violations.length) {
  const report = HARD_FAIL ? console.error : console.log;
  report(
    `\n${violations.length} dash(es) are being used as sentence punctuation. Rework the ` +
      `sentence rather than swapping the character: split it in two, or use a comma, a colon ` +
      `or parentheses. A hyphen is only right where the dash separated a label from its value.`
  );
  report(
    `A dash is allowed when it is the whole string literal, which is the EMPTY_VALUE ` +
      `placeholder in components/ui/table-text.tsx.`
  );
  for (const v of violations) {
    report(`  ${v.where}  ${v.name} ${v.code}`);
    report(`    ${v.text}`);
  }
  if (HARD_FAIL) process.exit(1);
}
