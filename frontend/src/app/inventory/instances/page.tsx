"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { formatInstanceType } from "@/lib/utils";
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
import { Play, Square, RotateCw, Trash2, Search, Filter, Terminal, Monitor } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { VMInfo } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { 
  useInstances, 
  useStartInstance, 
  useStopInstance, 
  useRestartInstance 
} from "@/hooks/use-lxd";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { InstanceMonitoringModal } from "@/components/instance-monitoring-modal";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

export default function InstancesPage() {
  const { data, isLoading, refetch, isRefetching } = useInstances();
  const instances = (data as any)?.vms || [];
  
  const startInstance = useStartInstance();
  const stopInstance = useStopInstance();
  const restartInstance = useRestartInstance();
  
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterProject, setFilterProject] = useState<string>("all");
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [selectedInstance, setSelectedInstance] = useState<VMInfo | null>(null);
  const [isMonitoringModalOpen, setIsMonitoringModalOpen] = useState(false);
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();
  const { hasPermission } = useTenantAccess();

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

  const handleAction = async (
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

    try {
      setActionLoading(`${instanceName}-${action}`);
      
      const mutation = action === "start" 
        ? startInstance 
        : action === "stop" 
        ? stopInstance 
        : restartInstance;
      
      await mutation.mutateAsync({ name: instanceName, host });
      
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
          disabled={isRefetching}
          isRefreshing={isRefetching}
          label="Refresh instances"
          onClick={() => refetch()}
        />
      </div>

      {/* Filters */}
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <CardTitle>All Instances ({filteredInstances.length} of {instances.length})</CardTitle>
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
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedInstances.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={8}>
                    <EmptyState icon={Monitor} title="No instances found" />
                  </TableCell>
                </TableRow>
              ) : (
                paginatedInstances.map((instance: any) => (
                <TableRow key={`${instance.host}-${instance.name}`}>
                  <TableCell 
                    className="font-medium cursor-pointer hover:text-primary hover:underline"
                    onClick={() => handleInstanceClick(instance)}
                  >
                    {instance.name}
                  </TableCell>
                  <TableCell>{instance.status}</TableCell>
                  <TableCell>{formatInstanceType(instance.type)}</TableCell>
                  <TableCell>{instance.host}</TableCell>
                  <TableCell>{instance.project || "default"}</TableCell>
                  <TableCell>
                    {instance.ip_addresses.length > 0
                      ? instance.ip_addresses.slice(0, 2).join(", ")
                      : "N/A"}
                  </TableCell>
                  <TableCell>
                    {instance.profiles.length > 0 ? instance.profiles.join(", ") : "N/A"}
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
                          title={!hasPermission(instance.host, instance.project || "default", instance.name, "operate") ? "No operate permission" : isReadOnly ? "Action disabled in read-only mode" : "Start instance"}
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
                            title={!hasPermission(instance.host, instance.project || "default", instance.name, "operate") ? "No operate permission" : isReadOnly ? "Action disabled in read-only mode" : "Stop instance"}
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
                            title={!hasPermission(instance.host, instance.project || "default", instance.name, "operate") ? "No operate permission" : isReadOnly ? "Action disabled in read-only mode" : "Restart instance"}
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
                            title={!hasPermission(instance.host, instance.project || "default", instance.name, "console") ? "No console permission" : "Open Terminal"}
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
                            title={!hasPermission(instance.host, instance.project || "default", instance.name, "console") ? "No console permission" : "Open Console"}
                          >
                            <Monitor className="h-4 w-4" />
                          </Button>
                        </>
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
    </div>
  );
}
