#!/usr/bin/env node
// Structural guard: every column an operator can see, an operator can filter by.
//
// The first version asked a weaker question, because the system could only answer a weaker
// one: it treated a count column as legitimately exempt on the reasoning that "which has the
// most" is answered by sorting. That stopped being true when number, date and text kinds
// arrived, and the exemption then waived exactly the columns the rule now requires. Inverted.
//
// The defect behind all of this: 18 of 24 tables offered zero or one option in "Add filter",
// on pages whose tables carry five to ten columns.
//
// Run: node scripts/checks/check-table-filters.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

const RESERVED = ['q', 'sort', 'dir', 'show', 'tab'];

/** Paths are relative to `frontend/src`, so the primitives live under this prefix, not `ui/`. */
const UI_DIR = 'components/ui/';

// Node and project are the dimensions the product is organised around, so a column that IS one
// must say so. Every rule keys off the declared role, but the guard has to notice the spelling
// to know when a role is missing.
const NODE_FIELDS = new Set(['node', 'host', 'node_name']);
const PROJECT_FIELDS = new Set(['project', 'projects']);

// Where the same spelling is the row's own identity rather than a dimension.
// `clusters.cluster` is the row; `certificates.cluster` is the node it belongs to. No
// name-based rule can tell those apart, which is exactly why `role` is declared, not inferred.
const ROLE_EXEMPT = new Map([
  ['app/inventory/clusters/page.tsx', 'a cluster row IS the node'],
  ['app/inventory/clustering/members/page.tsx', 'server_name is a host inside the selected cluster; node is the reserved selector'],
  ['components/dashboard/projects-section.tsx', 'a project card row IS the project'],
]);

/** Non-pinned facets rendered at rest. Node and project are structural and unbudgeted. */
const MAX_RESTING_FACETS = 2;

// Terminal find-bars, create-form pickers, the tag popover. Not table toolbars.
// Was 5 until the API key cluster picker (components/cluster-access-selector.tsx) adopted
// SearchInput, which it should have used from the start: it is the write-side twin of the
// cluster-access panel and had hand-rolled the same control one file away.
// The four left are cluster-location-dialog, console/iso-manager-panel, groups/edit-group-dialog
// and tag-manager.
// Ratchet: may fall, never rise.
const HANDROLLED_SEARCH_BASELINE = 4;

// important: every rule below keys off `defineTable`, so a table that renders TableBody
// directly is not exempt from the contract, it is invisible to the check. Five files are in
// that state today, one of which (components/vm-table.tsx) is dead code. Counting them keeps
// the headline number from reading as "every table in the app".
//
// Fell from six when the Snapshots page moved its table body into a component beside its own
// defineTable. That was forced rather than chosen: extracting the body and leaving the
// definition behind would have made it seven and failed the build.
// Ratchet: may fall, never rise.
const UNMANAGED_TABLE_BASELINE = 5;

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) yield* walk(full);
    else if (/\.(tsx|jsx)$/.test(entry)) yield full;
  }
}

/** Pull the column declarations out of every defineTable call in a file. */
function readTables(code) {
  const tables = [];
  let from = 0;
  for (;;) {
    const start = code.indexOf('defineTable<', from);
    if (start === -1) break;
    const columnsAt = code.indexOf('columns: [', start);
    if (columnsAt === -1) break;

    let depth = 0;
    let end = -1;
    for (let i = code.indexOf('[', columnsAt); i < code.length; i++) {
      if (code[i] === '[') depth++;
      else if (code[i] === ']') {
        depth--;
        if (depth === 0) { end = i; break; }
      }
    }
    if (end === -1) break;
    from = end;

    const block = code.slice(columnsAt, end);
    const columns = [];
    // note: splitting on `field:` puts everything BEFORE a column's field name into the
    // PREVIOUS column's chunk, and a waiver comment is naturally written above `field:`. The
    // per-column waiver therefore never matched its own column; it was the file-wide fallback
    // that made my-keys pass, and removing that fallback is what exposed this. `lead` is the
    // text from the brace that opens this column up to its field name.
    const parts = block.split(/field:\s*"/);
    for (let i = 1; i < parts.length; i += 1) {
      const part = parts[i];
      const prev = parts[i - 1];
      const braceAt = prev.lastIndexOf('{');
      const lead = braceAt === -1 ? '' : prev.slice(braceAt);
      const field = part.slice(0, part.indexOf('"'));
      const body = lead + part.slice(part.indexOf('"') + 1);
      const label = (body.match(/label:\s*"([^"]*)"/) ?? [])[1] ?? '';
      // note: not /\n\s*facet:/. A column written on one line has no newline before its flags,
      // and a guard that only recognises the formatting its author had in mind is the blind
      // spot table-conventions.md warns about. This file has hit it three times.
      columns.push({
        field,
        label,
        hasFacet: /\bfacet:/.test(body),
        hasFilter: /\bfilter:/.test(body),
        usesFromSort: /\bfilter:\s*fromSort\s*\(/.test(body),
        hasSearch: /\bsearch:/.test(body),
        hasSort: /\bsort:/.test(body),
        primary: /primary:\s*true/.test(body),
        role: (body.match(/role:\s*"(node|project)"/) ?? [])[1],
        waived: /\/\/\s*filter-coverage:/.test(body),
      });
    }
    if (columns.length) tables.push(columns);
  }
  return tables;
}

const offenders = [];
let handrolled = 0;
const handrolledFiles = [];
let unmanaged = 0;
const unmanagedFiles = [];
let tableCount = 0;
let columnCount = 0;
let filterableCount = 0;

for (const file of walk(src)) {
  const code = readFileSync(file, 'utf8');
  const rel = relative(src, file);
  const relPosix = rel.split(sep).join('/');

  const isPrimitive = [
    'ui/search-input.tsx',
    'ui/text-filter.tsx',
    'ui/faceted-filter.tsx',
    'ui/add-filter-menu.tsx',
  ].some((p) => relPosix.endsWith(p));

  const hasHandrolledSearch =
    !isPrimitive &&
    (/<Search\b[^>]*absolute/.test(code) || /<Input[^>]*placeholder="Search/.test(code));

  if (hasHandrolledSearch) {
    if (relPosix.startsWith('app/') && /<Table\b/.test(code)) {
      offenders.push(`${rel} hand-rolls a search input on a page that renders a table`);
    } else {
      handrolled += 1;
      handrolledFiles.push(rel);
    }
  }

  // A table the contract never reached. `components/ui/` holds the primitives it is built from,
  // and a wrapper primitive that renders TableBody itself is not an unmanaged table.
  // note: the prefix is `components/ui/`, not `ui/`. relPosix is relative to `frontend/src`, so
  // every path here starts at `app/` or `components/`. The first version of this guard tested
  // `ui/` and was inert, passing only because no primitive rendered a TableBody at the time.
  if (!relPosix.startsWith(UI_DIR) && /<TableBody\b/.test(code) && !/defineTable</.test(code)) {
    unmanaged += 1;
    unmanagedFiles.push(rel);
  }

  const roleExempt = ROLE_EXEMPT.has(relPosix);

  for (const columns of readTables(code)) {
    tableCount += 1;
    const roles = new Map();
    let restingDiscretionary = 0;

    for (const column of columns) {
      columnCount += 1;
      const filterable = column.hasFacet || column.hasFilter;
      if (filterable) filterableCount += 1;

      if (RESERVED.includes(column.field)) {
        offenders.push(`${rel}: column "${column.field}" collides with a reserved query key`);
      }

      // Rule 0: `fromSort()` derives the filter FROM the sort accessor, so a column using it
      // must declare one. table-filter.ts:398 throws when it does not, at module scope, which
      // means the whole importing module dies rather than one cell. A shared component took
      // six inventory pages to "Failed to load page" this way: zero rows, zero requests.
      //
      // important: a hard ban, never a ratchet. The runtime treats it as unconditionally
      // fatal, so there is no legitimate existing case to grandfather. tsc cannot see it
      // either, because both properties are individually well-typed and only their
      // combination is invalid, which is exactly the class this file exists to catch.
      if (column.usesFromSort && !column.hasSort) {
        offenders.push(
          `${rel}: column "${column.field}" uses fromSort() but declares no sort accessor. ` +
            'This throws at module scope and takes every page importing it down.',
        );
      }

      // Rule 1: a rendered column is filterable, searchable, or carries a written waiver.
      //
      // important: the waiver is per COLUMN. It used to also accept a `// filter-coverage:`
      // comment anywhere in the file, which meant the note on groups/page.tsx explaining why
      // that table has no project dimension silently exempted all eight of its columns from
      // this rule. A waiver for a column that does not exist cannot trip this rule anyway, so
      // the file-wide form bought nothing and disabled the check wholesale.
      if (column.hasSort && !filterable && !column.hasSearch && !column.waived) {
        offenders.push(
          `${rel}: column "${column.field}" has a sort accessor but no facet, filter or search`,
        );
      }

      // Rule 2: a node or project column declares which dimension it is, so it is pinned.
      const looksNode = NODE_FIELDS.has(column.field) || /^(Node|Cluster)$/.test(column.label);
      const looksProject = PROJECT_FIELDS.has(column.field) || column.label === 'Project';
      if (filterable && (looksNode || looksProject) && !column.role && !roleExempt && !column.waived) {
        const want = looksNode ? 'node' : 'project';
        offenders.push(
          `${rel}: column "${column.field}" is the ${want} dimension but declares no role. ` +
            `Add role: "${want}" so it is pinned everywhere.`,
        );
      }

      if (column.role) {
        if (roles.has(column.role)) {
          offenders.push(
            `${rel}: role "${column.role}" declared on both "${roles.get(column.role)}" and "${column.field}"`,
          );
        }
        roles.set(column.role, column.field);
      }

      if (column.primary && !column.role) restingDiscretionary += 1;
    }

    // Rule 3: the toolbar shows node, project and at most two other controls.
    if (restingDiscretionary > MAX_RESTING_FACETS) {
      offenders.push(
        `${rel}: ${restingDiscretionary} non-dimension facets marked primary, cap is ${MAX_RESTING_FACETS}`,
      );
    }
  }
}

if (handrolled > HANDROLLED_SEARCH_BASELINE) {
  offenders.push(
    `hand-rolled search inputs outside the table surface: ${handrolled} (baseline ${HANDROLLED_SEARCH_BASELINE}, may fall and never rise)`,
  );
  for (const f of handrolledFiles) offenders.push(`  ${f}`);
}

if (unmanaged > UNMANAGED_TABLE_BASELINE) {
  offenders.push(
    `tables rendered without defineTable: ${unmanaged} (baseline ${UNMANAGED_TABLE_BASELINE}, may fall and never rise). ` +
      'A table outside the contract is not checked by any rule in this file.',
  );
  for (const f of unmanagedFiles) offenders.push(`  ${f}`);
}

// A scan over nothing passes for the wrong reason.
if (tableCount < 20) {
  offenders.push(`only ${tableCount} tables found, expected the scan to reach more`);
}

if (offenders.length) {
  console.error('Table filter coverage:');
  for (const o of offenders) console.error(`  ${o}`);
  console.error('\nEvery column an operator can see should be one they can filter by.');
  console.error('app/inventory/networks/page.tsx is the reference.');
  process.exit(1);
}

console.log(`table filters: ${tableCount} tables, ${columnCount} columns, ${filterableCount} filterable`);
console.log(`hand-rolled search inputs outside the table surface: ${handrolled} (baseline ${HANDROLLED_SEARCH_BASELINE})`);
console.log(
  `tables rendered without defineTable and therefore unchecked: ${unmanaged} (baseline ${UNMANAGED_TABLE_BASELINE})`,
);
for (const f of unmanagedFiles) console.log(`  ${f}`);
