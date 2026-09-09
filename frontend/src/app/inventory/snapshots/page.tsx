"use client";

import type { SizeMeasurement, SnapshotInfo } from "@/types/api";

import { Suspense, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import { LoadingLottiePage } from "@/components/loading-lottie";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { RefreshButton } from "@/components/ui/refresh-button";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { useFleetSnapshots } from "@/hooks/use-lxd";
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

  // A first load blanks the page; a refetch over rows already on screen must not. Deliberately a
  // ref rather than placeholderData: scope is both the query key and a client-side facet here, so
  // keeping the previous scope's rows would render them filtered to zero, which looks like an
  // empty fleet rather than a load in progress.
  const hasLoadedOnce = useRef(false);
  if (data) hasLoadedOnce.current = true;

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

  return (
    <div className="p-8 space-y-6">
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
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

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
          {sizeNote && <p className="text-sm text-muted-foreground">{sizeNote}</p>}
        </CardHeader>
        <CardContent>
          {isLoading && !hasLoadedOnce.current ? (
            <LoadingLottiePage />
          ) : isError ? (
            <div className="py-8 text-center space-y-3">
              <p className="text-sm text-destructive">
                {error instanceof Error ? error.message : "Could not read snapshots."}
              </p>
              <Button variant="outline" size="sm" onClick={() => void refreshNow()}>
                Try again
              </Button>
            </div>
          ) : (
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
          )}
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
