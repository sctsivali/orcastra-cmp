#!/usr/bin/env node
// Structural guard: every column an operator can see, an operator can filter by.
//
// A column with a sort accessor also needs a facet, a filter, a search or a written waiver.
// Sorting does not replace filtering: the number, date and text filter kinds cover count and
// date columns too.
//
// Run: node scripts/checks/check-table-filters.mjs [repo-root]

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = join(root, 'frontend', 'src');

const RESERVED = ['q', 'sort', 'dir', 'show', 'tab'];

/** Paths are relative to `frontend/src`, so the primitives prefix starts at `components/`. */
const UI_DIR = 'components/ui/';

// Node and project are the dimensions the product is organised around, so a column that IS one
// must say so. Every rule keys off the declared role, but the guard has to notice the spelling
// to know when a role is missing.
const NODE_FIELDS = new Set(['node', 'host', 'node_name']);
const PROJECT_FIELDS = new Set(['project', 'projects']);

// Where the same spelling is the row's own identity rather than a dimension.
// `clusters.cluster` is the row; `certificates.cluster` is the node it belongs to. No
// name-based rule can tell those apart, so `role` is declared rather than inferred.
const ROLE_EXEMPT = new Map([
  ['app/inventory/clusters/page.tsx', 'a cluster row IS the node'],
  ['app/inventory/clustering/members/page.tsx', 'server_name is a host inside the selected cluster; node is the reserved selector'],
  ['components/dashboard/projects-section.tsx', 'a project card row IS the project'],
]);

/** Non-pinned facets rendered at rest. Node and project are structural and unbudgeted. */
const MAX_RESTING_FACETS = 2;

// Hand-rolled search inputs outside table toolbars (find-bars, create-form pickers, the tag
// popover): cluster-location-dialog, console/iso-manager-panel, groups/edit-group-dialog and
// tag-manager. Ratchet: may fall, never rise.
const HANDROLLED_SEARCH_BASELINE = 4;

// Every rule below keys off `defineTable`, so a table that renders TableBody directly is
// invisible to the check. Counting those keeps the summary from reading as "every table in the
// app". A table body moved into its own component must take its defineTable with it.
// Ratchet: may fall, never rise.
const UNMANAGED_TABLE_BASELINE = 4;

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
    // Splitting on `field:` puts everything before a column's field name into the previous
    // chunk, and a waiver comment sits above `field:`. `lead` is the text from the brace that
    // opens this column up to its field name, so the waiver matches its own column.
    const parts = block.split(/field:\s*"/);
    for (let i = 1; i < parts.length; i += 1) {
      const part = parts[i];
      const prev = parts[i - 1];
      const braceAt = prev.lastIndexOf('{');
      const lead = braceAt === -1 ? '' : prev.slice(braceAt);
      const field = part.slice(0, part.indexOf('"'));
      const body = lead + part.slice(part.indexOf('"') + 1);
      const label = (body.match(/label:\s*"([^"]*)"/) ?? [])[1] ?? '';
      // Word boundaries rather than /\n\s*facet:/, so a one-line column still matches.
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

  // A table outside the contract. A primitive under `components/ui/` that renders TableBody
  // itself is not an unmanaged table; relPosix starts at `app/` or `components/`, hence UI_DIR.
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

      // Rule 0: `fromSort()` derives the filter from the sort accessor, so a column using it
      // must declare one. resolveDeclaration in lib/table-filter.ts throws at module scope when
      // it does not, which takes down every page importing the table. tsc cannot see it: both
      // properties are well-typed on their own. A hard ban, never a ratchet.
      if (column.usesFromSort && !column.hasSort) {
        offenders.push(
          `${rel}: column "${column.field}" uses fromSort() but declares no sort accessor. ` +
            'This throws at module scope and takes every page importing it down.',
        );
      }

      // Rule 1: a rendered column is filterable, searchable, or carries a written waiver.
      //
      // important: the waiver is per column. A file-wide `// filter-coverage:` would silently
      // exempt every column in the file.
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
