"use client";

import { Suspense, useMemo, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { useSnapshotSchedules } from "@/hooks/use-lxd";
import { useSnapshotSchedulesEnabled } from "@/hooks/use-read-only-mode";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import { APIError } from "@/types/api";

import { SNAPSHOT_SCHEDULES, SnapshotSchedulesTable } from "./snapshot-schedules-table";

/**
 * The schedules half of the snapshots page, with its own filter instance and page state. It reads
 * Postgres rather than a cluster, so it renders even when the fleet snapshot read has failed,
 * which is when an operator most needs to see and pause what is scheduled.
 *
 * The Suspense boundary sits here as well as on the page: `useTableFilters` reads the query
 * string, and `next build` fails a route with no boundary above it
 * (check-searchparams-suspense.mjs).
 */
export function SnapshotSchedulesCard() {
  return (
    <Suspense fallback={<SchedulesCardShell><p className="text-sm text-muted-foreground">Reading schedules…</p></SchedulesCardShell>}>
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

  // Two sources for one fact. The settings poll answers before a request is made, so a
  // deployment with schedules off is never asked; a 404 covers a backend whose settings lack the
  // field, and it is the authoritative one when they disagree.
  const schedulesEnabled = useSnapshotSchedulesEnabled();
  const { data, isFetching, isError, error } = useSnapshotSchedules({}, schedulesEnabled !== false);
  const rows = useMemo(() => data?.items ?? [], [data]);
  // SNAPSHOT_SCHEDULES_ENABLED is off by default and gates both the loop and the router
  // (app/main.py), so a 404 here means the routes are not mounted rather than a failed read.
  const switchedOff =
    schedulesEnabled === false || (isError && error instanceof APIError && error.status === 404);

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
        {!switchedOff && rows.length > 0 && <TableToolbar filters={filters} noun="schedules" />}
      </CardHeader>
      <CardContent>
        {/*
          Four answers, and one line rather than a table for the three that have no rows: a full
          empty state would cost about 250px above the page's primary content on every visit.

          Switched off, failed and empty each get their own sentence: a deployment without the
          routes must not read as unused, and an outage an operator can act on is not a
          deployment setting. The error branch waits for `rows.length === 0` because a refetch can
          fail while the last good rows are still in hand, and those stay on screen.
        */}
        {switchedOff ? (
          <p className="text-sm text-muted-foreground">
            Snapshot schedules are switched off on this deployment. Nothing is scheduled and nothing
            will run until the feature is turned on. You can still take snapshots by hand.
          </p>
        ) : isError && rows.length === 0 ? (
          <p className="text-sm text-destructive">
            {error instanceof Error ? error.message : "Could not read schedules."}
          </p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {isFetching
              ? "Reading schedules…"
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

        {!switchedOff && sorted.length > 0 && (
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
