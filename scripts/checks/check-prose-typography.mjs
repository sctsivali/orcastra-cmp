#!/usr/bin/env node
/**
 * Structural guard for dash typography in prose.
 *
 * The stop-instance confirm dialog shipped "Cuts power immediately — like pulling the plug",
 * and the same dash was in 83 other places: 58 more across frontend/src and backend/app, plus
 * 36 en dashes holding up the API route list in README.md. Nothing in the repo could see any
 * of it. tsc and eslint do not read English, and none of the sixteen structural guards looks
 * at punctuation.
 *
 * The rule is not "no U+2014 anywhere", because one use of the glyph is load-bearing:
 * EMPTY_VALUE in components/ui/table-text.tsx is the single placeholder for a value the API
 * did not return, chosen over five competing spellings, mirrored as an escaped "—" in
 * lib/utils.ts, and asserted by five Playwright specs with an anchored /^—$/. Six more call
 * sites hardcode the bare glyph. A blanket ban would have to list all of them, and the list
 * would rot.
 *
 * So the rule keys on shape instead of location: a dash is legal only when it is the ENTIRE
 * string literal. "—" is a placeholder. "a — b" is punctuation. That one distinction covers
 * every sanctioned site including the comment in table-text.tsx that quotes the glyph as
 * data, and it needs no allowlist to maintain.
 *
 * En dash and horizontal bar are banned on the same terms, so the obvious workaround closes
 * with the original rather than a commit later.
 *
 * Advisory by decision of the owner: it reports and exits 0. Flip HARD_FAIL to true to make
 * it a gate.
 *
 * Deliberately parser-free: the CI guards job runs with no `npm ci`, so importing typescript
 * from frontend/node_modules would fail there. Regex only.
 *
 * Modelled on scripts/checks/check-table-typography.mjs.
 */
import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { join, relative } from "node:path";

const REPO = new URL("../..", import.meta.url).pathname;

const HARD_FAIL = false;

// note: a guard has to name the characters it bans, and the header above quotes the defect it
// was written for, so this file cannot satisfy its own rule. That is why ROOTS below does not
// include scripts/. The constants are escapes rather than literals so that at least the
// operative code carries none.
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

// A dash standing alone inside quotes is the empty-value placeholder, not punctuation.
// Matches a dash alone between matching quotes, for any of the three characters.
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
