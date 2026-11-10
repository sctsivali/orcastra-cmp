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
import { cn, plural } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { UsageRow } from "./usage-row";
import type { UsageView } from "./types";

// The references treated as a dataset rather than a list of chips. defineTable rather than a
// hand-rolled table: check-table-filters.mjs ratchets a TableBody outside components/ui that
// bypasses defineTable, and hand-rolled search inputs too.
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
      // check-table-filters.mjs flags any field named `project` with no declared role.
      facetSpec: { role: "project", primary: true },
    },
    {
      field: "attribution",
      label: "Attribution",
      // No sort accessor: this is a two-value flag, and the facet isolates every inferred
      // attribution in one click.
      facet: (r) => (r.projectExplicit ? "Explicit" : "Inferred"),
      facetSpec: {},
    },
    {
      // important: fromSort() derives the filter from the sort accessor, so a column using it
      // must declare one. Omitting it throws at module scope, which takes down every page that
      // imports this panel, and neither the typechecker nor the structural guards catch it.
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

  // syncUrl false: a dialog's filter keys must not stay in the address bar after it closes,
  // where they would filter a table the operator cannot see.
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

  // Group sizes come from the whole filtered set, not the page, so a group header shows its
  // full count on every page.
  const groupTotals = React.useMemo(() => {
    const totals = new Map<string, number>();
    for (const reference of ordered) {
      totals.set(reference.project, (totals.get(reference.project) ?? 0) + 1);
    }
    return totals;
  }, [ordered]);

  const kindCounts = React.useMemo(() => countsByKind(references), [references]);

  // Whether the first group on this page is a continuation is decided by the last row of the
  // previous page, not by the page number, so a group that starts on this page is not labelled
  // as continued.
  const previousRow = page > 1 ? ordered[(page - 1) * pageSize - 1] : undefined;

  const rows: React.ReactNode[] = [];
  let lastProject: string | null = null;
  for (const [index, reference] of pageRows.entries()) {
    if (view === "grouped" && reference.project !== lastProject) {
      const total = groupTotals.get(reference.project) ?? 0;
      const continued = lastProject === null && previousRow?.project === reference.project;
      rows.push(
        // The tint is on the row because check-table-typography.mjs whitelists cell classes and
        // bg-muted/50 is not among them.
        <TableRow key={`group-${reference.project}`} className="bg-muted/50 hover:bg-muted/50">
          <TableCell colSpan={4} className="font-medium">
            {reference.project}
            <TableSubText>
              {`${plural(total, "reference")}${continued ? ", continued" : ""}`}
            </TableSubText>
          </TableCell>
        </TableRow>,
      );
      lastProject = reference.project;
    }
    rows.push(
      // Index in the key, not the raw reference alone: a repeated reference must still render,
      // and two rows sharing a React key drop one of them silently.
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
              description={`${plural(filters.activeFilterCount, "filter")} active`}
            />
          ) : (
            rows
          )}
        </TableBody>
      </Table>

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
 * "412 references. 380 instances, 24 profiles, 5 networks, 3 more."
 *
 * Only the top three kinds, with the remainder counted rather than dropped. The full per-kind
 * breakdown is in the Type facet.
 */
function summarise(total: number, kinds: readonly { label: string; count: number }[]): string {
  if (total === 0) return "No references";
  const shown = kinds.slice(0, 3).map((k) => plural(k.count, k.label.toLowerCase()));
  const remainder = kinds.slice(3).reduce((sum, k) => sum + k.count, 0);
  if (remainder > 0) shown.push(`${remainder} more`);
  return `${plural(total, "reference")}. ${shown.join(", ")}.`;
}

// important: the panel carries its own Suspense boundary. useTableFilters reads the query string
// even with syncUrl false, which opts the subtree out of prerendering, and `next build` fails for
// any route that renders it without a Suspense above. A panel can land in any dialog on any page.
export function UsagePanel(props: React.ComponentProps<typeof UsagePanelContent>) {
  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <UsagePanelContent {...props} />
    </Suspense>
  );
}
