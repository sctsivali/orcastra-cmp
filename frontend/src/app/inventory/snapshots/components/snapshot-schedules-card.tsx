"use client";

import { Suspense, useMemo, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { useSnapshotSchedules } from "@/hooks/use-lxd";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";

import { SNAPSHOT_SCHEDULES, SnapshotSchedulesTable } from "./snapshot-schedules-table";

/**
 * The schedules half of the snapshots page.
 *
 * Its own component, its own filter instance and its own page state. Three filter instances
 * already coexist on the warnings page for the same reason: one selection must not leak into
 * another, which the table id namespaces in the URL.
 *
 * important: this reads Postgres, not a cluster. It is rendered even when the fleet snapshot read
 * has failed, because a cluster outage is exactly the moment an operator wants to see what is
 * still scheduled against it and pause it. Coupling it to that read would hide the control at the
 * only time it matters.
 */
/**
 * The boundary is here rather than only on the page.
 *
 * `useTableFilters` reads the query string, which opts a client component out of prerendering, and
 * Next then fails `next build` on the route unless a Suspense sits above it. The page already has
 * one, so this is redundant at runtime today. It is not redundant as a contract: this card is
 * self-contained and a second mount elsewhere would otherwise break a build minutes after a green
 * local run, which is the exact failure check-searchparams-suspense.mjs exists to move earlier.
 */
export function SnapshotSchedulesCard() {
  return (
    <Suspense fallback={<SchedulesCardShell><p className="text-sm text-muted-foreground">Reading schedules...</p></SchedulesCardShell>}>
      <SchedulesCardContent />
    </Suspense>
  );
}

function SchedulesCardShell({ children }: { children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader className="space-y-4">
        <CardTitle>Snapshot Schedules</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function SchedulesCardContent() {
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);
  const resetPage = () => setCurrentPage(1);

  const { data, isFetching } = useSnapshotSchedules();
  const rows = useMemo(() => data?.items ?? [], [data]);

  const filters = useTableFilters(SNAPSHOT_SCHEDULES, rows, { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    SNAPSHOT_SCHEDULES.sortAccessors,
    SNAPSHOT_SCHEDULES.defaultSort.field,
    SNAPSHOT_SCHEDULES.defaultSort.direction,
    resetPage,
  );

  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <Card>
      <CardHeader className="space-y-4">
        <CardTitle>Snapshot Schedules</CardTitle>
        {rows.length > 0 && <TableToolbar filters={filters} noun="schedules" />}
      </CardHeader>
      <CardContent>
        {/*
          One line rather than a table when there is nothing to show.

          A full empty state here costs about 250px above the primary content of a page called
          Snapshots, on every visit, for a feature most instances will not use. It also renders a
          <tbody> whose only row spans every column, which is a different shape from a data row and
          which anything reading the page positionally would meet first.
        */}
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {isFetching
              ? "Reading schedules..."
              : "No schedules yet. Open an instance, go to its Snapshots tab, and schedule one there."}
          </p>
        ) : (
          <SnapshotSchedulesTable
            rows={paginated}
            sortField={sortField}
            sortDirection={sortDirection}
            onSort={handleSort}
            refreshing={isFetching && paginated.length === 0}
            empty={{
              title: "No schedules match",
              // Not checked by check-heading-case, which reads `title` only, so this is cased by hand.
              description: "Every schedule is filtered out by the toolbar above.",
            }}
          />
        )}

        {sorted.length > 0 && (
          <div className="mt-4">
            <DataTablePagination
              currentPage={currentPage}
              totalPages={totalPages}
              pageSize={pageSize}
              totalItems={sorted.length}
              onPageChange={setCurrentPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                resetPage();
              }}
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
