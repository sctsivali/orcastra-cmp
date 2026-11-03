#!/usr/bin/env node
/**
 * Structural guard for table cell typography.
 *
 * The console had 27 tables whose cells each invented their own size, weight, colour and
 * font family. The baseline now lives once on TableCell in components/ui/table.tsx, so a
 * call site should only ever add layout: weight on the identifier column, monospace,
 * alignment, width and responsive visibility.
 *
 * This flags any other class on a <TableCell> or <TableHead> with a literal className.
 * Cells whose className is a template literal or a cn(...) call are skipped and counted,
 * so the blind spot is visible rather than silently zero.
 *
 * Modelled on scripts/checks/check-submit-guards.mjs.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../../frontend/src", import.meta.url).pathname;

const ALLOWED = new Set([
  "font-medium",   // identifier column only
  "font-mono",
  "text-left",
  "text-right",
  "text-center",
  "text-muted-foreground",
  "align-top",
  "align-middle",
  "whitespace-nowrap",
  "break-all",
  "break-words",
  "truncate",
  "hidden",
  "relative",
  "p-0",           // TableEmptyRow hands padding to EmptyState
]);

const ALLOWED_PREFIX = [
  /^(max-w-|min-w-|w-)/,
  /^(sm|md|lg|xl):(table-cell|hidden|flex)$/,
  /^\[&/,
];

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

const ok = (cls) => ALLOWED.has(cls) || ALLOWED_PREFIX.some((re) => re.test(cls));

const violations = [];
let skipped = 0;
let inspected = 0;

for (const file of walk(ROOT)) {
  const src = readFileSync(file, "utf8");
  const lines = src.split("\n");

  const literal = /<(TableCell|TableHead|SortableTableHead)\b[^>]*?className="([^"]*)"/gs;
  let m;
  while ((m = literal.exec(src)) !== null) {
    inspected += 1;
    const line = src.slice(0, m.index).split("\n").length;
    const bad = m[2].split(/\s+/).filter(Boolean).filter((c) => !ok(c));
    if (bad.length) {
      violations.push({
        file: relative(ROOT, file),
        line,
        el: m[1],
        bad,
        text: lines[line - 1]?.trim().slice(0, 90) ?? "",
      });
    }
  }

  const dynamic = /<(TableCell|TableHead|SortableTableHead)\b[^>]*?className=\{/gs;
  while (dynamic.exec(src) !== null) skipped += 1;
}

const byFile = new Map();
for (const v of violations) byFile.set(v.file, (byFile.get(v.file) ?? 0) + 1);

// --- rule 2: no arbitrary font sizes ------------------------------------------------
// The cell check above only sees TableCell/TableHead classNames. It cannot see a
// `text-[10px]` on a Badge nested inside a cell, which is how sixteen undersized chips
// survived the first sweep. Arbitrary sizes are banned outright: they sit between two
// steps of a scale that exists so sizes stay comparable, and below the readable floor
// for text a user is expected to act on.
const arbitrary = [];
for (const file of walk(ROOT)) {
  const src = readFileSync(file, "utf8");
  const lines = src.split("\n");
  lines.forEach((line, i) => {
    const m = line.match(/text-\[\d+(px|rem)\]/);
    if (m) arbitrary.push({ file: relative(ROOT, file), line: i + 1, cls: m[0] });
  });
}

// --- rule 3: a ratchet on badges inside table bodies ---------------------------------
// Status and Severity columns keep their Badge by an explicit product decision, so a hard
// ban would fail on purpose-built code. A ratchet instead: the count may fall, never rise.
// Raise BADGE_BASELINE only with a reason.
// 17 to 16: the clusters page rendered the hypervisor type as a pill. Type is not Status or
// Severity, so it was never one of the two exceptions this baseline exists to hold.
// 16 to 17: the Installed add-ons list became a table, and its Status column carries the
// installation's state. That is Status, so it is one of the two exceptions rather than a new
// pill: the same value was already a Badge while the list was a feed, and converting the markup
// is not a reason to start spelling it differently.
// 17 to 15: the dead components/vm-table.tsx was deleted with its two status pills.
// 15 to 16: the Synced Organizations table (components/integrations/synced-organizations-panel.tsx)
// has an Owner State column, the owner key's status, so it is the Status exception. The badge is
// what an administrator scans the list for: which organizations sit behind a key that stopped working.
const BADGE_BASELINE = 16;
// A ratchet, not a ban. The remaining occurrences live on chart, console and dialog-form
// surfaces rather than in table cells: the on-screen keyboard's legends are laid out to a
// fixed key size, and the monitoring charts pack labels into fixed gutters. Bumping those
// needs a layout pass on those pages, which is its own change. May fall, never rise.
// Lowered 75 -> 68. Seven text-[10px] cleared as a side effect of the value-reveal pass: the
// tenant policy description and its Badge, the terminal tray host line, and four more that moved
// onto the scale when their surrounding markup was rewritten. The rest still live on chart and
// console surfaces laid out to fixed gutters, which need a layout pass rather than a find and
// replace. May fall, never rise.
// 68 to 67: the dead components/vm-table.tsx was deleted with its one arbitrary size.
const ARBITRARY_BASELINE = 41;
let badgesInBodies = 0;
for (const file of walk(ROOT)) {
  const src = readFileSync(file, "utf8");
  for (const body of src.matchAll(/<TableBody>([\s\S]*?)<\/TableBody>/g)) {
    badgesInBodies += (body[1].match(/<Badge\b/g) ?? []).length;
  }
}

// --- rule 4: no click handler on a table cell ----------------------------------------
// A td is not focusable, gets no focus ring, and is invisible to keyboard and switch users.
// Wrap the text in TableLink instead, which renders a real button or anchor. This is a hard
// ban rather than a ratchet: there is no case where a bare <TableCell onClick> is correct.
//
// <TableRow onClick> is the same defect one element up, and it hid behind this rule for a
// while because the check only looked at cells. A tr is no more focusable than a td, so a
// whole-row click is mouse-only too, and it is worse in one respect: the row usually carries
// cursor-pointer, so it advertises an affordance that keyboard users cannot reach. The
// warnings page shipped exactly that, and its card even told every reader to click a row.
//
// <TableHead onClick> is the third. It hid for longer and cost more: SortableTableHead put the
// handler on the th, so column sorting was mouse-only in all 26 tables at once, and this rule
// could not see it because it listed the two element names where the defect had been found
// rather than every element that can carry it. The header now renders a real button inside the
// th, with aria-sort on the th where the ARIA spec puts it.
const clickableCells = [];
for (const file of walk(ROOT)) {
  const src = readFileSync(file, "utf8");
  for (const m of src.matchAll(/<Table(?:Cell|Row|Head)\b[^>]*?onClick=/gs)) {
    clickableCells.push({
      file: relative(ROOT, file),
      line: src.slice(0, m.index).split("\n").length,
      tag: m[0].startsWith("<TableRow")
        ? "TableRow"
        : m[0].startsWith("<TableHead")
          ? "TableHead"
          : "TableCell",
    });
  }
}

console.log(
  `table typography: ${inspected} literal cells inspected, ${skipped} dynamic skipped, ` +
    `${violations.length} violations across ${byFile.size} files`
);
console.log(
  `arbitrary font sizes: ${arbitrary.length} (baseline ${ARBITRARY_BASELINE})` +
    `   badges in table bodies: ${badgesInBodies} (baseline ${BADGE_BASELINE})` +
    `   mouse-only rows and cells: ${clickableCells.length}`
);

// A ratchet rather than a ban. The remaining occurrences live on chart, console and
// dialog-form surfaces, not in table cells: the on-screen keyboard's legends are laid out
// to a fixed key size, and the monitoring charts pack labels into fixed gutters. Bumping
// those needs a layout pass on those pages, which is its own change. The count may fall,
// never rise. Lower ARBITRARY_BASELINE as they are cleared.
if (arbitrary.length > ARBITRARY_BASELINE) {
  console.log(
    `\nArbitrary font sizes rose from ${ARBITRARY_BASELINE} to ${arbitrary.length}. ` +
      `These sit between two steps of a scale that exists so sizes stay comparable, and ` +
      `below the readable floor for text a user acts on. Use the scale:`
  );
  const shown = arbitrary.slice(0, 20);
  for (const a of shown) console.log(`  ${a.file}:${a.line}  ${a.cls}`);
  if (arbitrary.length > shown.length) console.log(`  ... and ${arbitrary.length - shown.length} more`);
}

if (badgesInBodies > BADGE_BASELINE) {
  console.log(
    `\nBadges inside table bodies rose from ${BADGE_BASELINE} to ${badgesInBodies}. ` +
      `Table data cells render plain text. Only Status and Severity keep a Badge, pending ` +
      `a separate decision. If you added one deliberately, raise BADGE_BASELINE and say why.`
  );
}

if (clickableCells.length) {
  console.log(
    `\n${clickableCells.length} table element(s) carry an onClick. None of tr, td or th is ` +
      `focusable, so the control is mouse-only. Use TableLink in a body cell, and a real button ` +
      `inside the th for a header:`
  );
  for (const c of clickableCells) console.log(`  ${c.file}:${c.line}  <${c.tag} onClick>`);
}

if (
  arbitrary.length > ARBITRARY_BASELINE ||
  badgesInBodies > BADGE_BASELINE ||
  clickableCells.length
) {
  process.exit(1);
}

if (violations.length) {
  for (const [file, n] of [...byFile.entries()].sort((a, b) => b[1] - a[1])) {
    console.log(`  ${String(n).padStart(3)}  ${file}`);
  }
  console.log(
    "\nCell classes may only be: weight on the identifier column, font-mono, alignment,\n" +
      "width, responsive visibility. Size, colour and padding come from TableCell.\n" +
      "See documentation/ui/frontend-design-language.md, Anti-slop rules."
  );
  process.exit(1);
}

console.log("table typography: all cells on the shared baseline");
