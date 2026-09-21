"use client";

import {
  Suspense,
  useCallback,
  useState,
  useMemo,
  useEffect,
} from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import { Button } from "@/components/ui/button";

import { RefreshButton } from "@/components/ui/refresh-button";

import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";

import { Loader2 } from "lucide-react";
import { useInstanceStream } from "@/hooks/use-instance-stream";

import { isStalePending, keyFor, resolvePendingCreate } from "@/lib/pending-creates";

import { VMInfo } from "@/types/api";

import { LoadingLottiePage } from "@/components/loading-lottie";

import { useInstances } from "@/hooks/use-lxd";
import { InstanceActionDialogs } from "./components/instance-action-dialogs";

import { DataTablePagination } from "@/components/ui/data-table-pagination";

import { InstanceDetailDialog } from "@/components/instance-detail/instance-detail-dialog";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { useNavigationProgress } from "@/components/navigation/navigation-progress";

import { useTenantAccess } from "@/hooks/use-tenant-access";

import { usePermissions } from "@/hooks/use-permissions";

import { Role } from "@/types/rbac";

import { useTableSort } from "@/hooks/use-table-sort";

import { TableToolbar } from "@/components/ui/table-toolbar";

import { useTableFilters } from "@/hooks/use-table-filters";

import { Plus } from "lucide-react";

import { RebuildDialog } from "@/components/rebuild-dialog";

import { MoveInstanceDialog } from "@/components/instance-detail/move-instance-dialog";

import { PageReadError } from "@/components/ui/read-error-state";

import { useRowFocus } from "@/hooks/use-row-focus";

import { RowFocusNotice } from "@/components/ui/row-focus-notice";

import { INSTANCES, InstancesTable } from "./components/instances-table";

import { useInstanceActions } from "./components/use-instance-actions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { InstanceSpecificationDialog } from "@/components/instance-specification/instance-specification-dialog";

function InstancesContent() {
  const { scopeKey } = useActorQueryScope();
  // Realtime mode: fetch the snapshot once, then keep the list current from the
  // LXD events stream (no polling). useInstanceStream patches this query's cache.
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt, isError, error } = useInstances(
    undefined,
    { realtime: true },
  );
  const instances = (data as any)?.vms || [];
  // Clusters that never answered: their instances are absent from the list, so warn
  // rather than showing a shorter table with nothing to explain it.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];

  // Hosts to stream from: those that currently have instances (pending-create
  // hosts are added inside the hook). One LXD events WS per host keeps the list
  // live. Creates show a "Creating… <progress>" placeholder, and lifecycle
  // events upsert/remove rows directly so status changes appear without a poll.
  const instanceHosts = Array.from(
    new Set(instances.map((i: any) => i.host as string)),
  ) as string[];
  const pendingCreates = useInstanceStream(instanceHosts);

  // A placeholder is "resolved" once a matching instance appears in list data that
  // landed AFTER the create was issued (the stream normally removes it first; this
  // is the fallback). Freshness is the real guard against the stale-cache race, and
  // it is exact where a fixed grace window was only a guess: a create invalidates
  // the instance-list cache server-side, so any data newer than the placeholder
  // cannot be the pre-create list. A blanket 12s wait used to sit here, which left
  // the same instance rendered twice, once "Creating…" and once with its real
  // status, for as long as the real row beat the stream event.
  const isResolved = (p: {
    host: string;
    name: string;
    project?: string;
    createdAt: number;
    jobId?: string;
    jobStatus?: string;
  }) =>
    (!p.jobId || p.jobStatus === "success") &&
    dataUpdatedAt > p.createdAt &&
    instances.some(
      (i: any) =>
        i.host === p.host &&
        i.name === p.name &&
        // important: with the project. Two projects can each hold an instance called "test",
        // so matching on host and name alone resolves a placeholder against someone else's row.
        (i.project || "default") === (p.project || "default"),
    );
  useEffect(() => {
    for (const p of pendingCreates) {
      if (isResolved(p)) resolvePendingCreate(scopeKey, p.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instances, pendingCreates, dataUpdatedAt, scopeKey]);
  // Hide placeholders that already resolved to a real row (avoids a duplicate).
  const visiblePendingCreates = pendingCreates.filter((p) => !isResolved(p));

  // important: isStalePending compares against Date.now() and is only read during a render,
  // and nothing here renders on a clock. useInstances runs with realtime, which sets
  // refetchInterval false, refetchOnWindowFocus is off by house config, and the only other
  // render sources are a stream frame or the operator clicking something. In the very case the
  // expiry exists for, a create LXD refused in the background, none of those arrive by
  // definition: the missing signal IS the premise. So the row kept animating "Creating..."
  // past the ten-minute mark until something unrelated happened to re-render the page.
  // One tick while a live placeholder is on screen, and nothing when the list is quiet.
  const [, forceStaleCheck] = useState(0);
  const hasLivePlaceholder = visiblePendingCreates.some(
    (p) =>
      p.status !== "Error" &&
      p.jobStatus !== "failure" &&
      p.jobStatus !== "partial" &&
      p.jobStatus !== "unknown" &&
      !isStalePending(p),
  );
  useEffect(() => {
    if (!hasLivePlaceholder) return;
    // The tick also refetches, so the list-freshness fallback above can actually fire: a create
    // whose frames never arrive (a stream drop, a daemon that names no project) used to wait for
    // an unrelated render, on a page that never polls.
    const id = setInterval(() => {
      forceStaleCheck((n) => n + 1);
      void refetch();
    }, 30_000);
    return () => clearInterval(id);
  }, [hasLivePlaceholder]);

  const actions = useInstanceActions({ refetch });
  const {
    isReadOnly,
    writeDisabledReason,
    deleteTarget,
    setDeleteTarget,
    rebuildTarget,
    setRebuildTarget,
    copyTarget,
    setCopyTarget,
  } = actions;

  // Deep link from OrcaHub: /inventory/instances?instance=<name>&project=<lxd-project>&host=<cluster>
  // seeds the initial filters. They are only the initial state; the user stays free to change them.
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [selectedInstance, setSelectedInstance] = useState<VMInfo | null>(null);
  const [specificationTarget, setSpecificationTarget] = useState<VMInfo | null>(null);
  const [isMonitoringModalOpen, setIsMonitoringModalOpen] = useState(false);
  const [selectionActorScope, setSelectionActorScope] = useState(scopeKey);
  const selectionIsCurrent = selectionActorScope === scopeKey;
  const router = useRouter();
  const { navigate } = useNavigationProgress();
  // permsLoading matters for the reasons below: until the access rules arrive, hasPermission
  // answers false for everyone, so an operator briefly sees every action withheld and told
  // "No permission" when the real answer is "not known yet". The items stay withheld, which is
  // the safe direction, but the reason must not claim a denial that has not been established.
  const { hasPermission, isLoading: permsLoading } = useTenantAccess();

  // Dialog targets are local state, so a sign-out/sign-in in the same document must clear
  // them explicitly. Query keys isolate the data, while this prevents an already-open detail
  // or destructive confirmation from carrying the previous actor's instance identity.
  useEffect(() => {
    setSelectionActorScope(scopeKey);
    setSelectedInstance(null);
    setSpecificationTarget(null);
    setIsMonitoringModalOpen(false);
  }, [scopeKey]);

  const permissionReason = (
    host: string,
    project: string,
    name: string,
    permission: Parameters<typeof hasPermission>[3],
  ): string | undefined =>
    permsLoading
      ? "Checking permissions"
      : hasPermission(host, project, name, permission)
        ? undefined
        : permission === "console"
          ? "No console permission"
          : "No operate permission";
  const { hasRole } = usePermissions();
  const canCreateInstance = hasRole(Role.PARTNER);
  // Deletion is Partner+ on the backend too (DELETE /vms requires Partner).
  const canDeleteInstance = hasRole(Role.PARTNER);

  const resetPage = () => setCurrentPage(1);
  const filters = useTableFilters(INSTANCES, instances as any[], { onFilterChange: resetPage });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    INSTANCES.sortAccessors,
    INSTANCES.defaultSort.field,
    INSTANCES.defaultSort.direction,
    resetPage,
  );

  // OrcaHub links in the wild carry ?instance=<name>. It is an external contract, so it is
  // read forever and translated once into the canonical search key, then removed. Removing it
  // is what keeps the seed from reasserting itself the moment the operator clears the field.
  // ?host= and ?project= need no translation: they already are the facet keys.
  useEffect(() => {
    const legacy = searchParams.get("instance");
    if (legacy === null) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("instance");
    if (legacy) params.set("q", legacy);
    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }, [searchParams, router, pathname]);

  const handleInstanceClick = (instance: VMInfo) => {
    setSelectionActorScope(scopeKey);
    setSelectedInstance(instance);
    setIsMonitoringModalOpen(true);
  };

  const currentSelectedInstance = selectionIsCurrent ? selectedInstance : null;

  const sortedInstances = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );
  // The row key is the placeholder's id, so a resolved placeholder names the row it became.
  const instanceKeyOf = useCallback(
    (i: VMInfo) => keyFor(i.host, i.project || "default", i.name),
    [],
  );
  const focus = useRowFocus({
    list: "instances",
    rows: instances,
    orderedRows: sortedInstances,
    keyOf: instanceKeyOf,
    pageSize,
    setPage: setCurrentPage,
    dataUpdatedAt,
    clearFilters: filters.clearAll,
  });

  // Pagination
  const totalPages = Math.ceil(sortedInstances.length / pageSize);
  const paginatedInstances = useMemo(() => {
    const startIndex = (currentPage - 1) * pageSize;
    return sortedInstances.slice(startIndex, startIndex + pageSize);
  }, [sortedInstances, currentPage, pageSize]);

  // Full-page loading state
  if (isLoading) {
    return <LoadingLottiePage />;
  }

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (isError && !data) {
    return (
      <PageReadError
        title="Instances"
        description="Manage hypervisor instances (containers and virtual machines)"
        noun="instances"
        error={error}
        onRetry={() => void refetch()}
      />
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Instances
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor instances (containers and virtual machines)
          </p>
        </div>
        <RefreshButton
          label="Refresh instances"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="instances" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <div className="flex items-center justify-between">
            <CardTitle>All Instances</CardTitle>
            {/* Only Partner+ can create; the create page enforces this and would
                bounce a Tenant straight back, so don't show a dead-end button. */}
            {canCreateInstance && (
              <Button
                onClick={() => navigate("/inventory/instances/create")}
                disabled={!!writeDisabledReason}
                title={writeDisabledReason ?? "Create a new instance"}
              >
                <Plus className="h-4 w-4 mr-2" />
                New Instance
              </Button>
            )}
          </div>
          <TableToolbar filters={filters} noun="instances" />
        </CardHeader>
        <CardContent>
          {focus.hidden && (
            <RowFocusNotice label={focus.hidden.label} onShow={focus.showHidden} className="mb-4" />
          )}
          <InstancesTable
            rows={paginatedInstances}
            pending={visiblePendingCreates}
            sortField={sortField}
            sortDirection={sortDirection}
            onSort={handleSort}
            activeFilterCount={filters.activeFilterCount}
            canCreateInstance={canCreateInstance}
            onCreate={() => navigate("/inventory/instances/create")}
            rowProps={focus.rowProps}
            onOpen={handleInstanceClick}
            onAction={actions.handleAction}
            pendingActions={actions.pendingActions}
            isReadOnly={isReadOnly}
            writeDisabledReason={writeDisabledReason}
            permissionReason={permissionReason}
            canDeleteInstance={canDeleteInstance}
            deletePending={actions.deletePending}
            onEditSpecification={(instance) => {
              setIsMonitoringModalOpen(false);
              setSelectedInstance(null);
              setSpecificationTarget(instance);
            }}
            onCopy={setCopyTarget}
            onRebuild={setRebuildTarget}
            onDelete={setDeleteTarget}
          />
          <DataTablePagination
            currentPage={currentPage}
            totalPages={totalPages}
            totalItems={sortedInstances.length}
            pageSize={pageSize}
            onPageChange={setCurrentPage}
            onPageSizeChange={setPageSize}
          />
        </CardContent>
      </Card>

      <InstanceDetailDialog
        instance={currentSelectedInstance}
        isOpen={selectionIsCurrent && isMonitoringModalOpen}
        onClose={() => {
          setIsMonitoringModalOpen(false);
          setSelectedInstance(null);
        }}
        project={currentSelectedInstance?.project || "default"}
        canEditSpecification={canDeleteInstance}
        editSpecificationDisabledReason={
          currentSelectedInstance
            ? writeDisabledReason
              ? writeDisabledReason
              : permissionReason(
                  currentSelectedInstance.host,
                  currentSelectedInstance.project || "default",
                  currentSelectedInstance.name,
                  "operate",
                )
            : undefined
        }
        onEditSpecification={() => {
          if (!currentSelectedInstance) return;
          setSpecificationTarget(currentSelectedInstance);
        }}
      />

      {specificationTarget && (
        <InstanceSpecificationDialog
          instance={specificationTarget}
          open={!!specificationTarget}
          onOpenChange={(open) => {
            if (!open) setSpecificationTarget(null);
          }}
          onDone={() => void refetch()}
          writeDisabledReason={writeDisabledReason}
        />
      )}

      {/* Copy elsewhere (shared across rows, controlled by copyTarget) */}
      {copyTarget && (
        <MoveInstanceDialog
          open={!!copyTarget}
          onOpenChange={(open) => {
            if (!open) setCopyTarget(null);
          }}
          instance={copyTarget}
        />
      )}

      {/* Rebuild (shared across rows, controlled by rebuildTarget) */}
      {rebuildTarget && (
        <RebuildDialog
          open={!!rebuildTarget}
          onOpenChange={(open) => {
            if (!open) setRebuildTarget(null);
          }}
          instance={{
            name: rebuildTarget.name,
            host: rebuildTarget.host,
            project: rebuildTarget.project || "default",
          }}
          onDone={() => refetch()}
        />
      )}

      {/* Confirm-and-mutate dialogs, shared across rows */}
      <InstanceActionDialogs
        deleteTarget={deleteTarget}
        deleteDisplay={actions.deleteDisplay}
        deletePending={actions.deleteInFlight}
        onDeleteOpenChange={(open) => {
          // Don't let Esc / outside-click dismiss it mid-delete; the delete keeps running
          // server-side and closing would strand the UI.
          if (!open && !actions.deleteInFlight) setDeleteTarget(null);
        }}
        onDeleteConfirm={actions.handleDeleteConfirm}
        confirmAction={actions.confirmAction}
        onConfirmActionChange={actions.setConfirmAction}
        onConfirmActionRun={actions.confirmActionRun}
      />
    </div>
  );
}

// Loading fallback while useSearchParams suspends (Next.js requires the Suspense boundary below).
function InstancesLoading() {
  return (
    <div className="flex items-center justify-center py-24">
      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
    </div>
  );
}

// Export with a Suspense wrapper - required because InstancesContent calls useSearchParams().
export default function InstancesPage() {
  return (
    <Suspense fallback={<InstancesLoading />}>
      <InstancesContent />
    </Suspense>
  );
}
