"use client";

import { Suspense, useCallback, useState, useMemo, useEffect, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow } from "@/components/ui/table-text";
import { formatInstanceType } from "@/lib/utils";
import { Loader2, Box } from "lucide-react";
import { useInstanceStream } from "@/hooks/use-instance-stream";
import { isStalePending, keyFor, resolvePendingCreate } from "@/lib/pending-creates";
import { VMInfo } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { LoadingLottiePage } from "@/components/loading-lottie";
import {
  useInstances,
  useStartInstance,
  useStopInstance,
  useRestartInstance,
  useDeleteInstance,
} from "@/hooks/use-lxd";
import { InstanceActionDialogs } from "./components/instance-action-dialogs";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { InstanceDetailDialog } from "@/components/instance-detail/instance-detail-dialog";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { Plus } from "lucide-react";
import { RebuildDialog } from "@/components/rebuild-dialog";
import { MoveInstanceDialog } from "@/components/instance-detail/move-instance-dialog";
import { PageReadError } from "@/components/ui/read-error-state";
import { useRowFocus } from "@/hooks/use-row-focus";
import { RowFocusNotice } from "@/components/ui/row-focus-notice";
import { PendingCreateRows } from "./components/pending-create-rows";
import { InstanceTableRow } from "./components/instance-table-row";

// The root disk an instance actually runs on. LXD keeps a size chosen at create time on the
// instance's own device; an instance that overrode nothing still has one through its profile,
// and in that case LXD reports no size because the pool default applies.


// note: the facet fields are named `host` and `project` deliberately, matching both the
// existing sort fields and the OrcaHub deep-link keys. That contract keeps working with no
// translation layer. Only `?instance=` needs mapping, and it is handled in the component.
const INSTANCES = defineTable<any>()({
  searchPlaceholder: "Search instances",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (i) => i.name, search: (i) => i.name, filter: fromSort("text") },
    {
      field: "status",
      label: "Status",
      sort: (i) => i.status,
      facet: (i) => i.status,
      facetSpec: { primary: true },
    },
    {
      field: "type",
      label: "Type",
      sort: (i) => i.type,
      facet: (i) => i.type,
      // formatInstanceType, not formatLabel: formatLabel("vm") returns "VM" through the
      // acronym map, which would be a fifth vocabulary for this one enum.
      facetSpec: { format: formatInstanceType },
    },
    {
      field: "host",
      label: "Node",
      sort: (i) => i.host,
      search: (i) => i.host,
      facet: (i) => i.host,
      facetSpec: { role: "node", primary: true },
    },
    {
      field: "project",
      label: "Project",
      sort: (i) => i.project || "default",
      search: (i) => i.project,
      facet: (i) => i.project || "default",
      facetSpec: { role: "project", primary: true },
    },
    { field: "ip_addresses", label: "IP Addresses", sort: (i) => i.ip_addresses?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    { field: "profiles", label: "Profiles", sort: (i) => i.profiles?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    {
      field: "tags",
      label: "Tags",
      facet: (i) => (i.tags ?? []).map((t: any) => (typeof t === "string" ? t : t?.name)).filter(Boolean),
      facetSpec: { emptyLabel: "(untagged)" },
    },
  ],
});

function InstancesContent() {
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
  }) =>
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
      if (isResolved(p)) resolvePendingCreate(p.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instances, pendingCreates, dataUpdatedAt]);
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
    (p) => p.status !== "Error" && !isStalePending(p),
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


  const startInstance = useStartInstance();
  const stopInstance = useStopInstance();
  const restartInstance = useRestartInstance();
  const deleteInstance = useDeleteInstance();

  // Deep link from OrcaHub: /inventory/instances?instance=<name>&project=<lxd-project>&host=<cluster>
  // seeds the initial filters. They are only the initial state; the user stays free to change them.
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  // Instance pending delete confirmation (controls the shared AlertDialog).
  const [deleteTarget, setDeleteTarget] = useState<VMInfo | null>(null);
  // Instance pending rebuild (controls the shared RebuildDialog).
  const [rebuildTarget, setRebuildTarget] = useState<VMInfo | null>(null);
  // Instance pending a copy to another project, member or cluster.
  const [copyTarget, setCopyTarget] = useState<VMInfo | null>(null);
  // Radix keeps AlertDialogContent mounted through its ~200ms exit fade, so
  // reading a just-nulled deleteTarget flashes blank name/host. Keep the last
  // target around and render the dialog body from it so it stays populated
  // during that fade (on both the Delete and Cancel paths).
  const deleteDisplayRef = useRef<VMInfo | null>(null);
  if (deleteTarget) deleteDisplayRef.current = deleteTarget;
  const deleteDisplay = deleteTarget ?? deleteDisplayRef.current;
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [selectedInstance, setSelectedInstance] = useState<VMInfo | null>(null);
  const [isMonitoringModalOpen, setIsMonitoringModalOpen] = useState(false);
  // Confirmation for stop/restart, with a Force option (power-cycle) for guests
  // that can't answer a graceful ACPI shutdown, e.g. a VM with no OS installed.
  const [confirmAction, setConfirmAction] = useState<{
    open: boolean;
    action: "stop" | "restart" | null;
    name: string;
    host: string;
    project: string;
    force: boolean;
  }>({ open: false, action: null, name: "", host: "", project: "", force: false });
  const router = useRouter();
  const { navigate } = useNavigationProgress();
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();
  // permsLoading matters for the labels below: until the access rules arrive, hasPermission
  // answers false for everyone, so an operator briefly sees every action disabled and tooltipped
  // "No permission" when the real answer is "not known yet". The buttons stay disabled, which is
  // the safe direction, but the label must not claim a denial that has not been established.
  const { hasPermission, isLoading: permsLoading } = useTenantAccess();

  const permissionTitle = (
    host: string,
    project: string,
    name: string,
    permission: Parameters<typeof hasPermission>[3],
    allowed: string,
    denied: string,
  ) =>
    permsLoading
      ? "Checking permissions..."
      : hasPermission(host, project, name, permission)
        ? allowed
        : denied;
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
    setSelectedInstance(instance);
    setIsMonitoringModalOpen(true);
  };

  // Entry point for the row action buttons. Start fires immediately; stop and
  // restart open a confirm dialog so the user can choose a graceful or forced
  // (power-cycle) operation.
  const handleAction = (
    instanceName: string,
    host: string,
    project: string,
    action: "start" | "stop" | "restart"
  ) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance actions are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    if (action === "start") {
      runAction(instanceName, host, project, action);
      return;
    }
    setConfirmAction({ open: true, action, name: instanceName, host, project, force: false });
  };

  const runAction = async (
    instanceName: string,
    host: string,
    project: string,
    action: "start" | "stop" | "restart",
    force = false
  ) => {
    try {
      setActionLoading(`${instanceName}-${action}`);

      if (action === "start") {
        await startInstance.mutateAsync({ name: instanceName, host, project });
      } else if (action === "stop") {
        await stopInstance.mutateAsync({ name: instanceName, host, project, force });
      } else {
        await restartInstance.mutateAsync({ name: instanceName, host, project, force });
      }

      toast({
        title: "Success",
        description: `Instance ${instanceName} ${action} initiated`,
      });

      // Refresh instances after action
      setTimeout(() => refetch(), 2000);
    } catch (error) {
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : `Failed to ${action} instance`,
        variant: "destructive",
      });
    } finally {
      setActionLoading(null);
    }
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    try {
      await deleteInstance.mutateAsync({
        name: target.name,
        host: target.host,
        force: true, // stops a running instance before deleting
        project: target.project || "default",
      });
      toast({
        title: "Instance deleted",
        description: `"${target.name}" has been deleted.`,
      });
    } catch (err) {
      toast({
        title: "Failed to delete instance",
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
    } finally {
      setDeleteTarget(null);
    }
  };

  const confirmActionRun = () => {
    if (confirmAction.action) {
      runAction(
        confirmAction.name,
        confirmAction.host,
        confirmAction.project,
        confirmAction.action,
        confirmAction.force,
      );
    }
    setConfirmAction((prev) => ({ ...prev, open: false }));
  };


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
                disabled={isReadOnly}
                title={isReadOnly ? "Action disabled in read-only mode" : "Create a new instance"}
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
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Node" field="host" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="IP Addresses" field="ip_addresses" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Profiles" field="profiles" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <TableHead>Root Disk</TableHead>
                <TableHead>Tags</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <PendingCreateRows pending={visiblePendingCreates} />
              {paginatedInstances.length === 0 ? (
                visiblePendingCreates.length === 0 ? (
                  <TableEmptyRow
                    colSpan={10}
                    icon={Box}
                    title={
                      filters.activeFilterCount > 0
                        ? "No instances found"
                        : "No instances yet"
                    }
                    // The description was unconditional, so a deployment with no clusters at all
                    // was told its filters were wrong. Eight sibling tables already guard this;
                    // this is the most-visited one and was the outlier.
                    description={
                      filters.activeFilterCount > 0
                        ? "No instance matches the current filters."
                        : "Instances appear here once a cluster reports one."
                    }
                    action={
                      filters.activeFilterCount === 0 && canCreateInstance ? (
                        <Button
                          size="sm"
                          onClick={() => navigate("/inventory/instances/create")}
                        >
                          Create Instance
                        </Button>
                      ) : undefined
                    }
                  />
                ) : null
              ) : (
                paginatedInstances.map((instance: any) => (
                <InstanceTableRow
                  key={keyFor(instance.host, instance.project || "default", instance.name)}
                  instance={instance}
                  rowProps={focus.rowProps(keyFor(instance.host, instance.project || "default", instance.name))}
                  onOpen={handleInstanceClick}
                  onAction={handleAction}
                  actionLoading={actionLoading}
                  isReadOnly={isReadOnly}
                  hasPermission={hasPermission}
                  permissionTitle={permissionTitle}
                  canDeleteInstance={canDeleteInstance}
                  deletePending={
                    deleteInstance.isPending &&
                    deleteTarget?.name === instance.name &&
                    deleteTarget?.host === instance.host
                  }
                  onCopy={setCopyTarget}
                  onRebuild={setRebuildTarget}
                  onDelete={setDeleteTarget}
                />
              ))
            )}
            </TableBody>
          </Table>
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
        instance={selectedInstance}
        isOpen={isMonitoringModalOpen}
        onClose={() => {
          setIsMonitoringModalOpen(false);
          setSelectedInstance(null);
        }}
        project={selectedInstance?.project || "default"}
      />

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
        deleteDisplay={deleteDisplay}
        deletePending={deleteInstance.isPending}
        onDeleteOpenChange={(open) => {
          // Don't let Esc / outside-click dismiss it mid-delete; the delete keeps running
          // server-side and closing would strand the UI.
          if (!open && !deleteInstance.isPending) setDeleteTarget(null);
        }}
        onDeleteConfirm={handleDeleteConfirm}
        confirmAction={confirmAction}
        onConfirmActionChange={setConfirmAction}
        onConfirmActionRun={confirmActionRun}
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
