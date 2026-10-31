"use client";

import * as React from "react";
import { Suspense } from "react";
import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { defineTable, fromSort } from "@/lib/table-filter";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { formatLabel, plural } from "@/lib/utils";
import { Shield } from "lucide-react";
import type { AclRule } from "../types";

// The richest payload in the app that nothing rendered. The ACLs table showed "N rules" as
// plain text, so every field below arrived on the wire and was dropped: action, source,
// destination, protocol, ports, state and the rule's own description.
type DirectedRule = AclRule & { direction: "Ingress" | "Egress"; index: number };

const RULES = defineTable<DirectedRule>()({
  searchPlaceholder: "Search rules",
  defaultSort: { field: "direction", direction: "asc" },
  columns: [
    {
      field: "direction",
      label: "Direction",
      sort: (r) => r.direction,
      facet: (r) => r.direction,
      facetSpec: { primary: true },
    },
    {
      field: "action",
      label: "Action",
      sort: (r) => r.action ?? "",
      facet: (r) => formatLabel(r.action ?? ""),
      facetSpec: { primary: true },
    },
    {
      field: "protocol",
      label: "Protocol",
      sort: (r) => r.protocol ?? "",
      facet: (r) => r.protocol || "(any)",
      facetSpec: {},
    },
    {
      field: "source",
      label: "Source",
      sort: (r) => r.source ?? "",
      search: (r) => r.source ?? "",
      filter: fromSort("text"),
    },
    {
      field: "destination",
      label: "Destination",
      sort: (r) => r.destination ?? "",
      search: (r) => r.destination ?? "",
      filter: fromSort("text"),
    },
    {
      field: "description",
      label: "Description",
      sort: (r) => r.description ?? "",
      search: (r) => r.description ?? "",
      filter: fromSort("text"),
    },
  ],
});

function ports(rule: DirectedRule): string {
  const parts = [rule.source_port, rule.destination_port].filter(Boolean);
  return parts.length ? parts.join(" → ") : "";
}

function AclRulesPanelContent({ ingress, egress }: { ingress?: AclRule[]; egress?: AclRule[] }) {
  const rules = React.useMemo<DirectedRule[]>(
    () => [
      ...(ingress ?? []).map((r, index) => ({ ...r, direction: "Ingress" as const, index })),
      ...(egress ?? []).map((r, index) => ({ ...r, direction: "Egress" as const, index })),
    ],
    [ingress, egress],
  );

  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(PANEL_PAGE_SIZE);
  const resetPage = React.useCallback(() => setPage(1), []);

  const filters = useTableFilters(RULES, rules, { syncUrl: false, onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    RULES.sortAccessors,
    RULES.defaultSort.field,
    RULES.defaultSort.direction,
    resetPage,
  );
  // Filter, then sort, then slice, so an ordering covers every rule the filters kept and not
  // only the page on screen.
  const sortedRules = React.useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  // note: clamped on read rather than corrected in an effect. An effect paints one wrong frame
  // first and then moves the panel while it is being read.
  const totalPages = Math.max(1, Math.ceil(sortedRules.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedRules = sortedRules.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (rules.length === 0) {
    return <EmptyState icon={Shield} title="No rules" description="This ACL defines no ingress or egress rules." />;
  }

  return (
    <div className="space-y-3">
      <TableToolbar filters={filters} noun="rules" />
      <Table>
        <TableHeader>
          <TableRow>
            <SortableTableHead label="Direction" field="direction" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Action" field="action" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Protocol" field="protocol" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Source" field="source" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Destination" field="destination" currentField={sortField} direction={sortDirection} onSort={handleSort} />
            <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
          </TableRow>
        </TableHeader>
        <TableBody>
          {pagedRules.length === 0 ? (
            <TableEmptyRow
              colSpan={6}
              icon={Shield}
              title="No rules match the current filters"
              description={`${plural(filters.activeFilterCount, "filter")} active on ${plural(rules.length, "rule")}.`}
              action={
                <Button variant="outline" size="sm" onClick={() => filters.clearAll()}>
                  Clear filters
                </Button>
              }
            />
          ) : (
            pagedRules.map((rule) => (
              <TableRow key={`${rule.direction}-${rule.index}`}>
                <TableCell>{rule.direction}</TableCell>
                <TableCell>{rule.action ? formatLabel(rule.action) : <TableEmptyValue label="No action" />}</TableCell>
                <TableCell>
                  {rule.protocol || <TableEmptyValue label="Any protocol" />}
                  {ports(rule) ? <span className="block text-xs text-muted-foreground">{ports(rule)}</span> : null}
                </TableCell>
                <TableCell className="font-mono">{rule.source || <TableEmptyValue label="Any source" />}</TableCell>
                <TableCell className="font-mono">{rule.destination || <TableEmptyValue label="Any destination" />}</TableCell>
                <TableCell>{rule.description || <TableEmptyValue label="No description" />}</TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
      {/* Absent rather than disabled when everything fits on one page. No mt-4 wrapper: the
          panel is already a space-y-3 stack, whose gap wins over a margin on the child. */}
      {totalPages > 1 && (
        <DataTablePagination
          currentPage={currentPage}
          totalPages={totalPages}
          pageSize={pageSize}
          pageSizeOptions={[...PANEL_PAGE_SIZES]}
          totalItems={sortedRules.length}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
        />
      )}
    </div>
  );
}

// important: its own boundary, not the host page's. useTableFilters reads the query string
// even with syncUrl false, which opts the subtree out of prerendering, and Next then fails
// `next build` for any route that renders it without a Suspense above. A panel is a leaf that
// can be dropped into any dialog on any page, so it carries the boundary itself rather than
// relying on every future host to remember.
export function AclRulesPanel(props: React.ComponentProps<typeof AclRulesPanelContent>) {
  return (
    <Suspense fallback={<Skeleton className="h-64 w-full" />}>
      <AclRulesPanelContent {...props} />
    </Suspense>
  );
}
