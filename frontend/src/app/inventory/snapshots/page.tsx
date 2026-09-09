"use client";

import type { SizeMeasurement, SnapshotInfo } from "@/types/api";

import { Suspense, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { AlertTriangle } from "lucide-react";

import { LoadingLottiePage } from "@/components/loading-lottie";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { useFleetSnapshots, useNodeNames } from "@/hooks/use-lxd";
import { useRequireRole } from "@/hooks/use-require-role";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { EMPTY_FACET, parseFacet } from "@/lib/filter-url";
import { TABLE_PAGE_SIZE } from "@/lib/pagination";
import { Role } from "@/types/rbac";

import { ALL_CLUSTERS, ALL_PROJECTS, SnapshotScopePicker } from "./components/snapshot-scope-picker";
import { SNAPSHOTS, SnapshotsTable } from "./components/snapshots-table";

/**
 * A hand-edited URL can carry several values, or the empty-bucket sentinel. Both have to degrade
 * to a fleet read filtered client-side, never reach the request, because the backend reads a
 * single scope and would either 422 or answer about the wrong one.
 */
function onlyValue(values: readonly string[]): string | undefined {
  return values.length === 1 && values[0] !== EMPTY_FACET ? values[0] : undefined;
}

const SIZE_NOTE: Record<SizeMeasurement, string | null> = {
  measured: null,
  partial:
    "Sizes were measured for the first 40 instances holding snapshots. The rest show no size.",
  skipped:
    "Sizes are not measured across the whole fleet. Choose one cluster and one project to measure them.",
};

/** One header for both the ordinary page and the error state, so they cannot drift apart. */
function SnapshotsHeader({
  onRefresh,
  lastUpdatedAt,
}: {
  onRefresh: () => Promise<void> | void;
  lastUpdatedAt?: number;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <h1 className="text-3xl font-bold">Snapshots</h1>
        <p className="text-muted-foreground mt-2">
          Restore points for maintenance. Snapshots this console creates are named{" "}
          <span className="font-mono">ops-</span> and expire on their own.
        </p>
      </div>
      <RefreshButton
        label="Refresh snapshots"
        lastUpdatedAt={lastUpdatedAt}
        onRefresh={onRefresh}
      />
    </div>
  );
}

function SnapshotsContent() {
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = () => setCurrentPage(1);

  // Scope is two of this table's own facets, and the URL is the single place it lives. The query
  // reads it straight from there rather than from useTableFilters, because the filters hook needs
  // the rows and the rows need the scope: reading the same params both ways breaks that cycle
  // without introducing a second source of truth. useTableFilters remains the only writer.
  const searchParams = useSearchParams();
  const scopeHost = onlyValue(parseFacet(searchParams.get("host")));
  const scopeProject = onlyValue(parseFacet(searchParams.get("project")));

  // important: the request narrows only when BOTH narrow, because that is the only scope the
  // backend measures sizes for. Picking a cluster alone costs no request at all: the fleet read
  // is already in hand and the Cluster facet filters it.
  const narrowed = Boolean(scopeHost && scopeProject);
  const { data, isLoading, isFetching, isError, error, refreshNow, dataUpdatedAt } =
    useFleetSnapshots(narrowed ? scopeHost : undefined, narrowed ? scopeProject : undefined);

  const items: SnapshotInfo[] = useMemo(() => data?.items ?? [], [data]);

  // important: the page subscribes to the cluster names itself rather than lifting the picker's
  // hook. check-node-picker-status registers snapshot-scope-picker.tsx and fails the build if it
  // stops calling useNodePickerOptions, and its own comment records that splitting a picker
  // across two files is how this surface slipped that guard once already. Same query key, so
  // React Query serves both subscriptions from one request.
  //
  // Gated on the cluster names and NOT on the project list, deliberately. useNodeNames is a
  // cheap list with a three minute staleTime, usually already warm, and without it the Cluster
  // select renders its single "All Clusters" row and reads as a one-cluster fleet. useAllProjects
  // is itself a fleet-wide fan-out; gating on it would lengthen the blank window and make the
  // picker's own "Reading projects" line unreachable, which is the exact defect that file
  // records having already fixed once.
  const nodeNames = useNodeNames();

  // The select writes this state and the chip records and undoes it: one piece of state rendered
  // twice, not two controls. The toolbar's own multi-select for these two is suppressed through
  // availableFields, because a single-select cannot faithfully render a two-value state, and
  // roles are pinned and exempt from the two-value threshold so this is the only lever.
  const filters = useTableFilters(SNAPSHOTS, items, {
    onFilterChange: resetPage,
    availableFields: SNAPSHOTS.facetFields.filter((f) => f !== "host" && f !== "project"),
  });

  const unreachableNodes: string[] = data?.unreachable_nodes ?? [];
  const queriedNodes: string[] = data?.queried_nodes ?? [];
  // A cluster that answered for only some of its projects is deliberately NOT in
  // unreachable_nodes, because its rows ARE here. Saying nothing would present an incomplete
  // list as a complete one, which is the whole failure this response shape exists to describe.
  const partialNodes: string[] = data?.partial_nodes ?? [];
  const truncated = data?.truncated ?? false;
  const sizeMeasurement: SizeMeasurement = data?.size_measurement ?? "skipped";

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    SNAPSHOTS.sortAccessors,
    SNAPSHOTS.defaultSort.field,
    SNAPSHOTS.defaultSort.direction,
    resetPage,
  );

  const sorted = useMemo(() => sortItems(filters.filteredRows), [filters.filteredRows, sortItems]);
  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // Ordered so the table never contradicts the banner above it. An unreachable cluster answers
  // 200 with no rows, so "take one from an instance's Snapshots tab" used to render directly
  // under an amber notice saying clusters could not be read.
  const empty =
    queriedNodes.length === 0 && unreachableNodes.length > 0
      ? {
          title: "No cluster answered",
          description:
            "Nothing could be read right now, so this is not an empty fleet. The banner above names the clusters that did not answer.",
        }
      : filters.activeFilterCount > 0
        ? {
            title: "No snapshots here",
            description: "No snapshot matches the current filters.",
          }
        : unreachableNodes.length > 0
          ? {
              title: "No snapshots here",
              description:
                "The clusters that answered hold none. The banner above names the ones that did not.",
            }
          : {
              title: "No snapshots here",
              description:
                "Take one from an instance's Snapshots tab before risky maintenance.",
            };

  const sizeNote = SIZE_NOTE[sizeMeasurement];

  // important: above every piece of chrome, which is what the other eighteen console pages do
  // and what this page stopped doing when the read became fleet-wide. The loader used to sit
  // inside the Card, which was harmless while nothing was fetched until a cluster was chosen and
  // became the first paint the moment that precondition went away. What rendered around it was
  // not merely early, it was untrue: the toolbar announced "0 snapshots" into a live region, the
  // size note stated a measurement decision the backend had not made, and an empty banner
  // asserted that every cluster had answered.
  //
  // note: no `hasLoadedOnce` ref. Every refetch it was written for keeps `data` and sets only
  // `isFetching`, so `isLoading` was already false for those; the one case it did change, a scope
  // change to a new query key, it made worse by rendering those same claims with no data. The
  // cost of dropping it is that narrowing to one cluster and one project blanks the page briefly.
  // That is the only interaction that refetches at all, it is the fast read rather than the
  // fan-out, and the URL holds the scope so the selects come back showing it.
  if (isLoading || nodeNames.isLoading) return <LoadingLottiePage />;

  // Only when there is nothing to show. A refetch can fail while the previous answer is still
  // held, and replacing readable rows with an error is worse than showing them. Same rule as
  // the technologies page.
  if (isError && !data) {
    return (
      <div className="p-8 space-y-6">
        <SnapshotsHeader onRefresh={refreshNow} lastUpdatedAt={dataUpdatedAt} />
        <Card>
          <CardContent className="pt-6">
            <EmptyState
              icon={AlertTriangle}
              title="Could not read snapshots"
              description={
                error instanceof Error
                  ? error.message
                  : "The console did not get an answer for this page. This says nothing about whether the snapshots are there."
              }
              action={
                <Button variant="outline" size="sm" onClick={() => void refreshNow()}>
                  Try again
                </Button>
              }
            />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="p-8 space-y-6">
      <SnapshotsHeader onRefresh={refreshNow} lastUpdatedAt={dataUpdatedAt} />

      <UnreachableNodesBanner noun="snapshots" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Snapshots</CardTitle>
          <SnapshotScopePicker
            host={scopeHost ?? ALL_CLUSTERS}
            project={scopeProject ?? ALL_PROJECTS}
            onHostChange={(next) =>
              filters.setFacet("host", next === ALL_CLUSTERS ? [] : [next])
            }
            onProjectChange={(next) =>
              filters.setFacet("project", next === ALL_PROJECTS ? [] : [next])
            }
          />
          <TableToolbar filters={filters} noun="snapshots" />
          {partialNodes.length > 0 && (
            <p className="text-sm text-warning">
              {partialNodes.join(", ")} answered for only some of their projects, so this list is
              incomplete for {partialNodes.length === 1 ? "it" : "them"}.
            </p>
          )}
          {truncated && (
            <p className="text-sm text-warning">
              This is the first {items.length} snapshots. Choose a cluster or a project to see the
              rest.
            </p>
          )}
          {sizeNote && <p className="text-sm text-muted-foreground">{sizeNote}</p>}
        </CardHeader>
        <CardContent>
          <>
              <SnapshotsTable
                rows={paginated}
                sortField={sortField}
                sortDirection={sortDirection}
                onSort={handleSort}
                empty={empty}
                refreshing={isFetching && paginated.length === 0}
                sizeMeasurement={sizeMeasurement}
              />

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
          </>
        </CardContent>
      </Card>
    </div>
  );
}

// important: the role gate sits ABOVE the content component, not inside it. The fleet read is no
// longer gated on a cluster having been chosen, so a tenant deep-linking here would otherwise fan
// out across every cluster before the redirect landed. check-role-gates.mjs reads this file's
// text and does not follow imports, so the literal has to stay here anyway.
export default function SnapshotsPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  // Suspense is required because useTableFilters reads useSearchParams, which opts the component
  // out of prerendering.
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <SnapshotsContent />
    </Suspense>
  );
}
