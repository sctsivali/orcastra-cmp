"use client";

import { Suspense, useState, useMemo, useEffect, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Play, Square, RotateCw, RefreshCw, Trash2, Search, Filter, Terminal, Monitor, Loader2 } from "lucide-react";
import { useInstanceStream } from "@/hooks/use-instance-stream";
import { TagBadge } from "@/components/tag-manager";
import { removePendingCreate } from "@/lib/pending-creates";
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
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { InstanceMonitoringModal } from "@/components/instance-monitoring-modal";
import { useRouter, useSearchParams } from "next/navigation";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Plus } from "lucide-react";
import { RebuildDialog } from "@/components/rebuild-dialog";

// The root disk an instance actually runs on. LXD keeps a size chosen at create time on the
// instance's own device; an instance that overrode nothing still has one through its profile,
// and in that case LXD reports no size because the pool default applies.
function rootDiskLabel(instance: any): { text: string; title: string } {
  const own = instance?.devices?.root;
  const effective = instance?.expanded_devices?.root ?? own;
  if (!effective) return { text: "—", title: "No root disk device reported by LXD" };
  const size = String(effective.size ?? "").trim();
  const pool = String(effective.pool ?? "").trim();
  if (!size) {
    return {
      text: pool ? `pool default (${pool})` : "pool default",
      title: "No explicit size on the root disk, so the storage pool default applies",
    };
  }
  const overridden = Boolean(own?.size);
  return {
    text: size,
    title: overridden
      ? `Set on this instance${pool ? `, pool ${pool}` : ""}`
      : `Inherited from a profile${pool ? `, pool ${pool}` : ""}`,
  };
}

function InstancesContent() {
  // Realtime mode: fetch the snapshot once, then keep the list current from the
  // LXD events stream (no polling). useInstanceStream patches this query's cache.
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useInstances(
    undefined,
    { realtime: true },
  );
  const instances = (data as any)?.vms || [];

  // Hosts to stream from: those that currently have instances (pending-create
  // hosts are added inside the hook). One LXD events WS per host keeps the list
  // live — creates show a "Creating… <progress>" placeholder, and lifecycle
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
  const isResolved = (p: { host: string; name: string; createdAt: number }) =>
    dataUpdatedAt > p.createdAt &&
    instances.some((i: any) => i.host === p.host && i.name === p.name);
  useEffect(() => {
    for (const p of pendingCreates) {
      if (isResolved(p)) removePendingCreate(p.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instances, pendingCreates, dataUpdatedAt]);
  // Hide placeholders that already resolved to a real row (avoids a duplicate).
  const visiblePendingCreates = pendingCreates.filter((p) => !isResolved(p));


  const startInstance = useStartInstance();
  const stopInstance = useStopInstance();
  const restartInstance = useRestartInstance();
  const deleteInstance = useDeleteInstance();

  // Deep link from OrcaHub: /inventory/instances?instance=<name>&project=<lxd-project>&host=<cluster>
  // seeds the initial filters. They are only the initial state; the user stays free to change them.
  const searchParams = useSearchParams();
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  // Instance pending delete confirmation (controls the shared AlertDialog).
  const [deleteTarget, setDeleteTarget] = useState<VMInfo | null>(null);
  // Instance pending rebuild (controls the shared RebuildDialog).
  const [rebuildTarget, setRebuildTarget] = useState<VMInfo | null>(null);
  // Radix keeps AlertDialogContent mounted through its ~200ms exit fade, so
  // reading a just-nulled deleteTarget flashes blank name/host. Keep the last
  // target around and render the dialog body from it so it stays populated
  // during that fade (on both the Delete and Cancel paths).
  const deleteDisplayRef = useRef<VMInfo | null>(null);
  if (deleteTarget) deleteDisplayRef.current = deleteTarget;
  const deleteDisplay = deleteTarget ?? deleteDisplayRef.current;
  const [searchQuery, setSearchQuery] = useState(() => searchParams.get("instance") ?? "");
  const [filterNode, setFilterNode] = useState<string>(() => searchParams.get("host") ?? "all");
  const [filterProject, setFilterProject] = useState<string>(() => searchParams.get("project") ?? "all");
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [selectedInstance, setSelectedInstance] = useState<VMInfo | null>(null);
  const [isMonitoringModalOpen, setIsMonitoringModalOpen] = useState(false);
  // Confirmation for stop/restart, with a Force option (power-cycle) for guests
  // that can't answer a graceful ACPI shutdown — e.g. a VM with no OS installed.
  const [confirmAction, setConfirmAction] = useState<{
    open: boolean;
    action: "stop" | "restart" | null;
    name: string;
    host: string;
    force: boolean;
  }>({ open: false, action: null, name: "", host: "", force: false });
  const router = useRouter();
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

  const sortAccessors = useMemo(
    () => ({
      name: (i: any) => i.name,
      status: (i: any) => i.status,
      type: (i: any) => i.type,
      host: (i: any) => i.host,
      project: (i: any) => i.project || "default",
      ip_addresses: (i: any) => i.ip_addresses?.length ?? 0,
      profiles: (i: any) => i.profiles?.length ?? 0,
    }),
    [],
  );
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    "name",
    "asc",
    () => setCurrentPage(1),
  );

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
      runAction(instanceName, host, action);
      return;
    }
    setConfirmAction({ open: true, action, name: instanceName, host, force: false });
  };

  const runAction = async (
    instanceName: string,
    host: string,
    action: "start" | "stop" | "restart",
    force = false
  ) => {
    try {
      setActionLoading(`${instanceName}-${action}`);

      if (action === "start") {
        await startInstance.mutateAsync({ name: instanceName, host });
      } else if (action === "stop") {
        await stopInstance.mutateAsync({ name: instanceName, host, force });
      } else {
        await restartInstance.mutateAsync({ name: instanceName, host, force });
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
      runAction(confirmAction.name, confirmAction.host, confirmAction.action, confirmAction.force);
    }
    setConfirmAction((prev) => ({ ...prev, open: false }));
  };

  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case "running":
        return "default";
      case "stopped":
        return "secondary";
      case "frozen":
        return "outline";
      default:
        return "destructive";
    }
  };

  // Get unique nodes and projects for filters
  const availableNodes = useMemo(() => {
    const nodes = Array.from(new Set(instances.map((i: any) => i.host as string))) as string[];
    return nodes.sort();
  }, [instances]);

  const availableProjects = useMemo(() => {
    const projects = Array.from(new Set(instances.map((i: any) => (i.project || "default") as string))) as string[];
    return projects.sort();
  }, [instances]);

  // Filter and search instances
  const filteredInstances = useMemo(() => {
    return instances.filter((instance: any) => {
      const matchesSearch =
        instance.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        instance.host.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (instance.project && instance.project.toLowerCase().includes(searchQuery.toLowerCase()));

      const matchesNode = filterNode === "all" || instance.host === filterNode;
      const matchesProject = filterProject === "all" || (instance.project || "default") === filterProject;
      const matchesStatus = filterStatus === "all" || instance.status.toLowerCase() === filterStatus.toLowerCase();

      return matchesSearch && matchesNode && matchesProject && matchesStatus;
    });
  }, [instances, searchQuery, filterNode, filterProject, filterStatus]);

  const sortedInstances = useMemo(() => sortItems(filteredInstances), [filteredInstances, sortItems]);

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

      {/* Filters */}
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <CardTitle>All Instances ({filteredInstances.length} of {instances.length})</CardTitle>
              {/* Only Partner+ can create; the create page enforces this and would
                  bounce a Tenant straight back, so don't show a dead-end button. */}
              {canCreateInstance && (
                <Button
                  onClick={() => router.push("/inventory/instances/create")}
                  disabled={isReadOnly}
                  title={isReadOnly ? "Action disabled in read-only mode" : "Create a new instance"}
                >
                  <Plus className="h-4 w-4 mr-2" />
                  New Instance
                </Button>
              )}
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
              <div className="lg:col-span-2">
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search instances..."
                    value={searchQuery}
                    onChange={(e) => {
                      setSearchQuery(e.target.value);
                      setCurrentPage(1);
                    }}
                    className="pl-9"
                  />
                </div>
              </div>
              <Select value={filterNode} onValueChange={(value) => { setFilterNode(value); setCurrentPage(1); }}>
                <SelectTrigger>
                  <SelectValue placeholder="All Nodes" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Nodes</SelectItem>
                  {availableNodes.map((node: string) => (
                    <SelectItem key={node} value={node}>{node}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={filterProject} onValueChange={(value) => { setFilterProject(value); setCurrentPage(1); }}>
                <SelectTrigger>
                  <SelectValue placeholder="All Projects" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Projects</SelectItem>
                  {availableProjects.map((project: string) => (
                    <SelectItem key={project} value={project}>{project}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={filterStatus} onValueChange={(value) => { setFilterStatus(value); setCurrentPage(1); }}>
                <SelectTrigger>
                  <SelectValue placeholder="All Status" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Status</SelectItem>
                  <SelectItem value="running">Running</SelectItem>
                  <SelectItem value="stopped">Stopped</SelectItem>
                  <SelectItem value="frozen">Frozen</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardHeader>
        <CardContent>
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
                <TableHead>Root disk</TableHead>
                <TableHead>Tags</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visiblePendingCreates.map((p) => (
                <TableRow key={`pending-${p.id}`} className="bg-muted/30">
                  <TableCell className="font-medium">{p.name}</TableCell>
                  <TableCell>
                    <Badge
                      variant={p.status === "Error" ? "destructive" : "outline"}
                      className="gap-1"
                    >
                      {p.status !== "Error" && (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      )}
                      {p.status === "Error"
                        ? "Failed"
                        : p.progress
                          ? `Creating… ${p.progress}`
                          : "Creating…"}
                    </Badge>
                    {p.error && (
                      <span className="ml-2 text-xs text-destructive">{p.error}</span>
                    )}
                  </TableCell>
                  <TableCell>{p.type}</TableCell>
                  <TableCell>
                    <Badge variant="outline">{p.host}</Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{p.project || "default"}</Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">—</TableCell>
                  <TableCell className="text-muted-foreground">—</TableCell>
                  <TableCell className="text-muted-foreground">—</TableCell>
                  <TableCell />
                </TableRow>
              ))}
              {paginatedInstances.length === 0 ? (
                visiblePendingCreates.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={9} className="text-center text-muted-foreground py-8">
                      No instances found
                    </TableCell>
                  </TableRow>
                ) : null
              ) : (
                paginatedInstances.map((instance: any) => (
                <TableRow key={`${instance.host}-${instance.name}`}>
                  <TableCell 
                    className="font-medium cursor-pointer hover:text-primary hover:underline"
                    onClick={() => handleInstanceClick(instance)}
                  >
                    {instance.name}
                  </TableCell>
                  <TableCell>
                    <Badge variant={getStatusColor(instance.status)}>
                      {instance.status}
                    </Badge>
                  </TableCell>
                  <TableCell>{instance.type}</TableCell>
                  <TableCell>
                    <Badge variant="outline">{instance.host}</Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{instance.project || "default"}</Badge>
                  </TableCell>
                  <TableCell>
                    {instance.ip_addresses.length > 0
                      ? instance.ip_addresses.slice(0, 2).join(", ")
                      : "N/A"}
                  </TableCell>
                  <TableCell>
                    {instance.profiles.map((profile: string) => (
                      <Badge key={profile} variant="outline" className="mr-1">
                        {profile}
                      </Badge>
                    ))}
                  </TableCell>
                  <TableCell>
                    {(() => {
                      const disk = rootDiskLabel(instance);
                      return (
                        <span
                          className="whitespace-nowrap text-sm text-muted-foreground"
                          title={disk.title}
                        >
                          {disk.text}
                        </span>
                      );
                    })()}
                  </TableCell>
                  <TableCell>
                    {instance.tags && instance.tags.length > 0 ? (
                      <div className="flex flex-wrap gap-1.5">
                        {instance.tags.slice(0, 3).map((tag: string) => (
                          <TagBadge key={tag} tag={tag} size="sm" />
                        ))}
                        {instance.tags.length > 3 && (
                          <span className="text-xs text-muted-foreground">
                            +{instance.tags.length - 3}
                          </span>
                        )}
                      </div>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-2">
                      {instance.status !== "Running" && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            handleAction(instance.name, instance.host, "start")
                          }
                          disabled={actionLoading === `${instance.name}-start` || isReadOnly || !hasPermission(instance.host, instance.project || "default", instance.name, "operate")}
                          title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Start instance", "No operate permission")}
                        >
                          <Play className="h-4 w-4" />
                        </Button>
                      )}
                      {instance.status === "Running" && (
                        <>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              handleAction(instance.name, instance.host, "stop")
                            }
                            disabled={actionLoading === `${instance.name}-stop` || isReadOnly || !hasPermission(instance.host, instance.project || "default", instance.name, "operate")}
                            title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Stop instance", "No operate permission")}
                          >
                            <Square className="h-4 w-4" />
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              handleAction(instance.name, instance.host, "restart")
                            }
                            disabled={actionLoading === `${instance.name}-restart` || isReadOnly || !hasPermission(instance.host, instance.project || "default", instance.name, "operate")}
                            title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Restart instance", "No operate permission")}
                          >
                            <RotateCw className="h-4 w-4" />
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => {
                              useTerminalStore.getState().openTerminal({
                                instanceName: instance.name,
                                host: instance.host,
                                project: instance.project || "default",
                              });
                            }}
                            disabled={!hasPermission(instance.host, instance.project || "default", instance.name, "console")}
                            title={permissionTitle(instance.host, instance.project || "default", instance.name, "console", "Open Terminal", "No console permission")}
                          >
                            <Terminal className="h-4 w-4" />
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => {
                              useConsoleStore.getState().openConsole({
                                instanceName: instance.name,
                                instanceType: instance.type || "container",
                                host: instance.host,
                                project: instance.project || "default",
                              });
                            }}
                            disabled={!hasPermission(instance.host, instance.project || "default", instance.name, "console")}
                            title={permissionTitle(instance.host, instance.project || "default", instance.name, "console", "Open Console", "No console permission")}
                          >
                            <Monitor className="h-4 w-4" />
                          </Button>
                        </>
                      )}
                      {canDeleteInstance && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => setRebuildTarget(instance)}
                          disabled={
                            isReadOnly ||
                            !hasPermission(instance.host, instance.project || "default", instance.name, "operate")
                          }
                          aria-label={`Rebuild instance ${instance.name}`}
                          title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Rebuild instance (reinstall root disk)", "No operate permission")}
                        >
                          <RefreshCw className="h-4 w-4" />
                        </Button>
                      )}
                      {canDeleteInstance && (
                        <Button
                          size="sm"
                          variant="outline"
                          className="text-destructive hover:text-destructive"
                          onClick={() => setDeleteTarget(instance)}
                          // Only the row actually being deleted greys out; a
                          // page-wide isPending would disable every row's button.
                          disabled={
                            isReadOnly ||
                            (deleteInstance.isPending &&
                              deleteTarget?.name === instance.name &&
                              deleteTarget?.host === instance.host)
                          }
                          aria-label={`Delete instance ${instance.name}`}
                          title={isReadOnly ? "Action disabled in read-only mode" : "Delete instance"}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              ))
            )}
            </TableBody>
          </Table>
          <DataTablePagination
            currentPage={currentPage}
            totalPages={totalPages}
            totalItems={filteredInstances.length}
            pageSize={pageSize}
            onPageChange={setCurrentPage}
            onPageSizeChange={setPageSize}
          />
        </CardContent>
      </Card>

      <InstanceMonitoringModal
        instance={selectedInstance}
        isOpen={isMonitoringModalOpen}
        onClose={() => {
          setIsMonitoringModalOpen(false);
          setSelectedInstance(null);
        }}
        project={selectedInstance?.project || "default"}
      />

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

      {/* Delete confirmation (shared across rows, controlled by deleteTarget) */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(open) => {
          // Don't let Esc / outside-click dismiss it mid-delete; the delete
          // keeps running server-side and closing would strand the UI.
          if (!open && !deleteInstance.isPending) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Delete instance &quot;{deleteDisplay?.name}&quot;?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes the instance and its data from{" "}
              {deleteDisplay?.host}.
              {deleteDisplay?.status === "Running" &&
                " The instance is currently running and will be force-stopped first."}
              {deleteDisplay?.status === "Frozen" &&
                " The instance is frozen by billing policy and will be force-stopped first."}{" "}
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteInstance.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              // preventDefault blocks Radix's built-in close so the dialog stays
              // open (and populated, with the spinner) for the full 10-30s that a
              // force-stop + delete takes. The finally in handleDeleteConfirm
              // closes it once the request resolves.
              onClick={(e) => {
                e.preventDefault();
                handleDeleteConfirm();
              }}
              disabled={deleteInstance.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteInstance.isPending && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {deleteInstance.isPending ? "Deleting…" : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Stop / Restart confirmation, with a Force (power-cycle) option. */}
      <AlertDialog
        open={confirmAction.open}
        onOpenChange={(open) => setConfirmAction((prev) => ({ ...prev, open }))}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmAction.action === "stop" ? "Stop" : "Restart"} “{confirmAction.name}”?
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmAction.action === "stop"
                ? "This will shut down the instance."
                : "This will restart the instance."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {/* A graceful stop asks the guest OS to shut down over ACPI. A VM with
              no OS installed, or one at a boot menu/UEFI shell, has nothing
              listening and will time out — force power-cycles those. */}
          <label className="flex items-start gap-2 rounded-md border p-3 cursor-pointer">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={confirmAction.force}
              onChange={(e) =>
                setConfirmAction((prev) => ({ ...prev, force: e.target.checked }))
              }
            />
            <span className="text-sm">
              <span className="font-medium">
                Force (power-cycle, don&apos;t wait for the guest)
              </span>
              <span className="block text-xs text-muted-foreground mt-0.5">
                {confirmAction.force
                  ? "Cuts power immediately — like pulling the plug. Unsaved data in the guest may be lost."
                  : "Use this if nothing in the guest will answer a graceful shutdown: no OS installed yet, sitting at a boot menu, still booting, or a live/installer image that ignores ACPI."}
              </span>
            </span>
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmActionRun}>
              {confirmAction.force ? "Force " : ""}
              {confirmAction.action === "stop" ? "Stop" : "Restart"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
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
