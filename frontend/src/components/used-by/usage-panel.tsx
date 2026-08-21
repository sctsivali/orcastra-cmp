"use client";

import * as React from "react";
import { Suspense } from "react";
import {
  Table,
  TableBody,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { TableCell } from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { Button } from "@/components/ui/button";
import { defineTable, fromSort } from "@/lib/table-filter";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { orderByProject, referencesFromUsedBy, type LxdReference } from "@/lib/used-by";
import { countsByKind, referenceKindLabel } from "@/lib/used-by-kinds";
import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { UsageRow } from "./usage-row";
import type { UsageView } from "./types";

// The references treated as a dataset rather than a list of chips.
//
// important: it uses defineTable rather than a hand-rolled table for two reasons beyond taste.
// check-table-filters.mjs counts any file outside components/ui that renders a TableBody
// without defineTable against a ratchet, and it flags hand-rolled search inputs against another.
// The groups page already ships this exact combination inside a DialogContent, so the pattern
// is proven here, not novel.
const USED_BY = defineTable<LxdReference>()({
  searchPlaceholder: "Search references",
  defaultSort: { field: "resource", direction: "asc" },
  searchExtra: (r) => [r.raw],
  columns: [
    {
      field: "resource",
      label: "Resource",
      sort: (r) => r.name ?? r.raw,
      search: (r) => r.name ?? r.raw,
      filter: fromSort("text"),
    },
    {
      field: "kind",
      label: "Type",
      sort: (r) => referenceKindLabel(r),
      facet: (r) => referenceKindLabel(r),
      facetSpec: { primary: true },
    },
    {
      field: "project",
      label: "Project",
      sort: (r) => r.project,
      facet: (r) => r.project,
      // required: check-table-filters.mjs flags any field named `project` with no declared role.
      facetSpec: { role: "project", primary: true },
    },
    {
      field: "attribution",
      label: "Attribution",
      // No sort accessor on purpose: this is a two-value flag, and the facet is the useful
      // control. It lets an operator isolate every inferred attribution in one click.
      facet: (r) => (r.projectExplicit ? "Explicit" : "Inferred"),
      facetSpec: {},
    },
    {
      // important: fromSort() derives the filter FROM the sort accessor, so a column using it
      // must declare one. Omitting it throws at module scope, which takes down every page that
      // imports this panel, and neither the typechecker nor the structural guards can see it.
      field: "reference",
      label: "Reference",
      sort: (r) => r.raw,
      search: (r) => r.raw,
      filter: fromSort("text"),
    },
  ],
});

const PAGE_SIZES = [25, 50, 100, 250];

interface UsagePanelProps {
  usedBy: readonly string[];
  node?: string;
  view: UsageView;
  onViewChange: (view: UsageView) => void;
  onCopied: (message: string) => void;
}

function UsagePanelContent({ usedBy, node, view, onViewChange, onCopied }: UsagePanelProps) {
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(50);
  const resetPage = React.useCallback(() => setPage(1), []);

  const references = React.useMemo(() => referencesFromUsedBy(usedBy), [usedBy]);

  // note: syncUrl false. A dialog's filter keys must not outlive it in the address bar, where
  // they would filter a table the operator can no longer see.
  const filters = useTableFilters(USED_BY, references, {
    onFilterChange: resetPage,
    syncUrl: false,
  });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    USED_BY.sortAccessors,
    USED_BY.defaultSort.field,
    USED_BY.defaultSort.direction,
    resetPage,
  );

  const ordered = React.useMemo(() => {
    const sorted = sortItems(filters.filteredRows);
    // Stable sort, so whichever column the operator chose still holds inside each project.
    return view === "grouped" ? orderByProject(sorted) : sorted;
  }, [filters.filteredRows, sortItems, view]);

  const totalPages = Math.max(1, Math.ceil(ordered.length / pageSize));
  const pageRows = ordered.slice((page - 1) * pageSize, page * pageSize);

  // Group sizes come from the whole FILTERED set, not the page. A header reading "4213
  // references, continued" on page twelve tells the truth; one reading "50" would not.
  const groupTotals = React.useMemo(() => {
    const totals = new Map<string, number>();
    for (const reference of ordered) {
      totals.set(reference.project, (totals.get(reference.project) ?? 0) + 1);
    }
    return totals;
  }, [ordered]);

  const kindCounts = React.useMemo(() => countsByKind(references), [references]);

  // important: whether the first group on this page is a continuation is decided by the LAST
  // ROW OF THE PREVIOUS PAGE, not by "is this page > 1". Keying it on the page number labels
  // every page-2-and-later opening group as continued, including one that genuinely starts
  // there, which is a small lie told confidently on every single page.
  const previousRow = page > 1 ? ordered[(page - 1) * pageSize - 1] : undefined;

  const rows: React.ReactNode[] = [];
  let lastProject: string | null = null;
  for (const [index, reference] of pageRows.entries()) {
    if (view === "grouped" && reference.project !== lastProject) {
      const total = groupTotals.get(reference.project) ?? 0;
      const continued = lastProject === null && previousRow?.project === reference.project;
      rows.push(
        // The tint lives on the row, not the cell: check-table-typography.mjs whitelists cell
        // classes and bg-muted/50 is not among them.
        <TableRow key={`group-${reference.project}`} className="bg-muted/50 hover:bg-muted/50">
          <TableCell colSpan={4} className="font-medium">
            {reference.project}
            <TableSubText>
              {`${total} reference${total === 1 ? "" : "s"}${continued ? ", continued" : ""}`}
            </TableSubText>
          </TableCell>
        </TableRow>,
      );
      lastProject = reference.project;
    }
    rows.push(
      // note: index in the key, not the raw reference alone. The grouping helper this replaces
      // deduplicated through a Set, which is only worth doing if a repeated reference has been
      // seen, and two rows sharing a React key drop one of them silently.
      <UsageRow
        key={`${reference.raw}-${index}`}
        reference={reference}
        node={node}
        onCopied={onCopied}
      />,
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">{summarise(references.length, kindCounts)}</p>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <TableToolbar filters={filters} noun="references" />
        <div className="inline-flex items-center gap-1" role="group" aria-label="Grouping">
          {(["grouped", "flat"] as const).map((option) => (
            <Button
              key={option}
              type="button"
              size="sm"
              variant={view === option ? "secondary" : "ghost"}
              aria-pressed={view === option}
              onClick={() => {
                onViewChange(option);
                resetPage();
              }}
              className={cn("h-8")}
            >
              {option === "grouped" ? "By project" : "Flat"}
            </Button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <SortableTableHead label="Resource" field="resource" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              <SortableTableHead label="Type" field="kind" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              <SortableTableHead label="Reference" field="reference" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            </TableRow>
          </TableHeader>
          <TableBody>
            {pageRows.length === 0 ? (
              <TableEmptyRow
                colSpan={4}
                title="No references match the current filters"
                description={`${filters.activeFilterCount} filter${filters.activeFilterCount === 1 ? "" : "s"} active`}
              />
            ) : (
              rows
            )}
          </TableBody>
        </Table>
      </div>

      <DataTablePagination
        currentPage={page}
        totalPages={totalPages}
        pageSize={pageSize}
        totalItems={ordered.length}
        onPageChange={setPage}
        onPageSizeChange={(size) => {
          setPageSize(size);
          resetPage();
        }}
        pageSizeOptions={PAGE_SIZES}
      />
    </div>
  );
}

/**
 * "412 references across 7 projects. 380 instances, 24 profiles and 8 more."
 *
 * note: only the top three kinds, and the remainder is NAMED rather than dropped. The full
 * breakdown with per-kind counts is already in the Type facet, so restating it here would be a
 * second control two inches from the first.
 */
function summarise(total: number, kinds: readonly { label: string; count: number }[]): string {
  if (total === 0) return "No references";
  const shown = kinds.slice(0, 3).map((k) => `${k.count} ${k.label.toLowerCase()}${k.count === 1 ? "" : "s"}`);
  const remainder = kinds.slice(3).reduce((sum, k) => sum + k.count, 0);
  if (remainder > 0) shown.push(`${remainder} more`);
  return `${total} reference${total === 1 ? "" : "s"}. ${shown.join(", ")}.`;
}

// important: its own boundary, not the host page's. useTableFilters reads the query string
// even with syncUrl false, which opts the subtree out of prerendering, and Next then fails
// `next build` for any route that renders it without a Suspense above. A panel is a leaf that
// can be dropped into any dialog on any page, so it carries the boundary itself rather than
// relying on every future host to remember.
export function UsagePanel(props: React.ComponentProps<typeof UsagePanelContent>) {
  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <UsagePanelContent {...props} />
    </Suspense>
  );
}
