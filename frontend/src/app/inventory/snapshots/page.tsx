"use client";

import type {
  FleetSnapshotListResponse,
  SnapshotCreateResponse,
  SnapshotInfo,
} from "@/types/api";

import { Suspense, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";

import { AlertTriangle, Camera, Loader2 } from "lucide-react";

import { LoadingLottiePage } from "@/components/loading-lottie";
import {
  PendingSnapshotAlert,
  type PendingSnapshotNotice,
} from "@/components/snapshots/pending-snapshot-alert";
import type { SnapshotCreateTarget } from "@/components/snapshots/snapshot-create-form";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { useFleetSnapshots, useNodeNames } from "@/hooks/use-lxd";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useRequireRole } from "@/hooks/use-require-role";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { useToast } from "@/hooks/use-toast";
import { EMPTY_FACET, parseFacet } from "@/lib/filter-url";
import { api } from "@/lib/api";
import { TABLE_PAGE_SIZE } from "@/lib/pagination";
import { readSnapshotList } from "@/lib/snapshot-reconciliation";
import { Role } from "@/types/rbac";

import { ALL_CLUSTERS, ALL_PROJECTS, SnapshotScopePicker } from "./components/snapshot-scope-picker";
import { SnapshotSchedulesCard } from "./components/snapshot-schedules-card";
import { SNAPSHOTS, SnapshotsTable } from "./components/snapshots-table";
import { TakeSnapshotDialog } from "./components/take-snapshot-dialog";

/**
 * A hand-edited URL can carry several values, or the empty-bucket sentinel. Both have to degrade
 * to a fleet read filtered client-side, never reach the request, because the backend reads a
 * single scope and would either 422 or answer about the wrong one.
 */
function onlyValue(values: readonly string[]): string | undefined {
  return values.length === 1 && values[0] !== EMPTY_FACET ? values[0] : undefined;
}

/**
 * What the size column could not say for itself. Nothing when every holder was measured; the
 * counters when the backend sent them, and a generic sentence when it did not (an older backend,
 * or a mocked body) rather than a number invented from the rows.
 */
function sizeNoteFor(data: FleetSnapshotListResponse): string | null {
  const measured = data.measured_instances;
  const unmeasured = data.unmeasured_instances;
  switch (data.size_measurement) {
    case "partial":
      return typeof measured === "number" && typeof unmeasured === "number"
        ? `Sizes measured for ${measured} of ${measured + unmeasured} instances holding snapshots. The rest show Not measured.`
        : "Sizes were measured for some of the instances holding snapshots. The rest show Not measured.";
    case "skipped":
      return "Sizes could not be measured on this read. Refresh to try again.";
    default:
      return null;
  }
}

/** One header for both the ordinary page and the error state, so they cannot drift apart. */
function SnapshotsHeader({
  onRefresh,
  lastUpdatedAt,
  onTakeSnapshot,
  takeSnapshotDisabled,
  takeSnapshotLoading,
}: {
  onRefresh: () => Promise<void> | void;
  lastUpdatedAt?: number;
  onTakeSnapshot: () => void;
  takeSnapshotDisabled: boolean;
  takeSnapshotLoading: boolean;
}) {
  const disabledReasonId = "fleet-take-snapshot-disabled-reason";
  const disabledReason = takeSnapshotLoading
    ? "Checking whether snapshot creation is available."
    : takeSnapshotDisabled
      ? "Snapshot creation is disabled in read-only mode."
      : null;

  return (
    <div className="flex flex-col items-start justify-between gap-4 sm:flex-row">
      <div className="min-w-0 sm:flex-1">
        <h1 className="text-3xl font-bold">Snapshots</h1>
        <p className="text-muted-foreground mt-2">
          Restore points for maintenance. Snapshots this console creates are named{" "}
          <span className="font-mono">ops-</span> and expire on their own.
        </p>
      </div>
      <div className="flex w-full flex-wrap items-start gap-2 sm:w-auto sm:shrink-0 sm:flex-nowrap sm:justify-end">
        <RefreshButton
          label="Refresh snapshots"
          lastUpdatedAt={lastUpdatedAt}
          onRefresh={onRefresh}
        />
        <div className="flex flex-col items-start gap-1 sm:items-end">
          <Button
            type="button"
            size="sm"
            className="min-h-11 sm:min-h-9"
            onClick={onTakeSnapshot}
            disabled={takeSnapshotDisabled || takeSnapshotLoading}
            aria-busy={takeSnapshotLoading}
            aria-describedby={disabledReason ? disabledReasonId : undefined}
            title={
              takeSnapshotLoading
                ? "Checking read-only mode"
                : takeSnapshotDisabled
                  ? "Action disabled in read-only mode"
                  : "Take an operator snapshot of one instance"
            }
          >
            {takeSnapshotLoading ? (
              <Loader2
                className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none"
                aria-hidden
              />
            ) : (
              <Camera className="mr-2 h-4 w-4" aria-hidden />
            )}
            Take Snapshot
          </Button>
          {disabledReason && (
            <p
              id={disabledReasonId}
              role="status"
              className="max-w-64 text-xs text-muted-foreground sm:text-right"
            >
              {disabledReason}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function SnapshotsContent() {
  const queryClient = useQueryClient();
  const { scopeKey: actorScope } = useActorQueryScope();
  const { toast } = useToast();
  const readOnlyMode = useReadOnlyMode();
  const [takeSnapshotOpen, setTakeSnapshotOpen] = useState(false);
  const [pendingSnapshot, setPendingSnapshot] = useState<PendingSnapshotNotice | null>(null);
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

  // important: the request narrows only when BOTH narrow. That read is the cheaper one and the
  // only path the backend runs its per-scope snapshot access check on; sizes do not depend on
  // it, every read measures them. Picking a cluster alone costs no request at all: the fleet
  // read is already in hand and the Cluster facet filters it.
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
  // 200 with no rows, so the ordinary create guidance used to render directly
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
                "Use Take Snapshot above, or open an instance's Snapshots tab, before risky maintenance.",
            };

  // Never claimed before data exists: a note about sizes while loading states a measurement
  // decision the backend has not made.
  const sizeNote = data ? sizeNoteFor(data) : null;

  const handleSnapshotCreated = (
    result: SnapshotCreateResponse,
    target: SnapshotCreateTarget,
  ) => {
    if (result.status === "succeeded") {
      toast({ title: "Snapshot taken", description: result.message });
      return;
    }
    setPendingSnapshot({
      host: target.instance.host,
      project: target.project,
      instanceName: target.instance.name,
      snapshotName: result.snapshot,
      message: result.message,
    });
    toast({ title: "Snapshot creation is in progress", description: result.message });
  };

  const viewTargetSnapshots = (target: SnapshotCreateTarget) => {
    // The dialog has already verified the exact instance endpoint. Apply that scope before the
    // list reads again, so an unrelated fleet-wide outage cannot turn a successful target check
    // into a failure or leave the operator looking at an unscoped stale table.
    void queryClient.invalidateQueries({
      queryKey: ["fleetSnapshots", target.instance.host, target.project],
      exact: true,
    });
    filters.setFacet("host", [target.instance.host]);
    filters.setFacet("project", [target.project]);
    filters.setSearchInput(target.instance.name);
  };

  const refreshPendingSnapshot = async (notice: PendingSnapshotNotice) => {
    // The page filters can differ from the target chosen in the dialog. Read the immutable
    // notice tuple explicitly so Refresh can never verify an unrelated scope, then warm the
    // exact fleet query before moving the table to that target. staleTime 0: the create's own
    // slice read fills this key on a success verdict, and a click must still reach the cluster.
    await api.snapshots.list(notice.instanceName, notice.host, notice.project);
    await queryClient.fetchQuery({
      queryKey: ["fleetSnapshots", notice.host, notice.project],
      queryFn: () =>
        readSnapshotList(queryClient, actorScope, () =>
          api.snapshots.listFleet(notice.host, notice.project, { fresh: true }),
        ),
      staleTime: 0,
    });
    filters.setFacet("host", [notice.host]);
    filters.setFacet("project", [notice.project]);
    filters.setSearchInput(notice.instanceName);
  };

  const takeSnapshotDialog = (
    <TakeSnapshotDialog
      open={takeSnapshotOpen}
      onOpenChange={setTakeSnapshotOpen}
      initialHost={scopeHost}
      initialProject={scopeProject}
      onCreated={handleSnapshotCreated}
      onViewSnapshots={viewTargetSnapshots}
    />
  );

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
        <SnapshotsHeader
          onRefresh={refreshNow}
          lastUpdatedAt={dataUpdatedAt}
          onTakeSnapshot={() => setTakeSnapshotOpen(true)}
          takeSnapshotDisabled={readOnlyMode.isReadOnly}
          takeSnapshotLoading={readOnlyMode.isLoading}
        />
        {pendingSnapshot && (
          <PendingSnapshotAlert
            notice={pendingSnapshot}
            onRefresh={() => refreshPendingSnapshot(pendingSnapshot)}
            onDismiss={() => setPendingSnapshot(null)}
          />
        )}
        {takeSnapshotDialog}
        {/* Still rendered. Schedules are read from Postgres rather than from a cluster, and a
            cluster outage is exactly when an operator needs to see what is scheduled against it
            and stop it. */}
        <SnapshotSchedulesCard />
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
      <SnapshotsHeader
        onRefresh={refreshNow}
        lastUpdatedAt={dataUpdatedAt}
        onTakeSnapshot={() => setTakeSnapshotOpen(true)}
        takeSnapshotDisabled={readOnlyMode.isReadOnly}
        takeSnapshotLoading={readOnlyMode.isLoading}
      />

      {pendingSnapshot && (
        <PendingSnapshotAlert
          notice={pendingSnapshot}
          onRefresh={() => refreshPendingSnapshot(pendingSnapshot)}
          onDismiss={() => setPendingSnapshot(null)}
        />
      )}

      {takeSnapshotDialog}

      <UnreachableNodesBanner noun="snapshots" nodes={unreachableNodes} />

      {/* Schedules first: they are the cause of some of the rows below, and stacking rather than
          tabbing keeps that comparison on one screen. */}
      <SnapshotSchedulesCard />

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
              This is the first {items.length} snapshots. Choose one cluster and one project to
              narrow the read and see the rest.
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
