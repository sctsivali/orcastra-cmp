"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
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
import { Box, Play, Square, RotateCw, Trash2, Search, Filter, ArrowUp, ArrowDown, Terminal, Monitor } from "lucide-react";
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

type SortField = "name" | "host" | "project" | "status";
type SortDirection = "asc" | "desc";

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
  const [sortField, setSortField] = useState<SortField>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [selectedInstance, setSelectedInstance] = useState<VMInfo | null>(null);
  const [isMonitoringModalOpen, setIsMonitoringModalOpen] = useState(false);
  const { toast } = useToast();
  const { isReadOnly } = useReadOnlyMode();

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

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
    setCurrentPage(1);
  };

  // Filter and search instances
  const filteredInstances = useMemo(() => {
    let filtered = instances.filter((instance: any) => {
      const matchesSearch = 
        instance.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        instance.host.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (instance.project && instance.project.toLowerCase().includes(searchQuery.toLowerCase()));
      
      const matchesNode = filterNode === "all" || instance.host === filterNode;
      const matchesProject = filterProject === "all" || (instance.project || "default") === filterProject;
      const matchesStatus = filterStatus === "all" || instance.status.toLowerCase() === filterStatus.toLowerCase();
      
      return matchesSearch && matchesNode && matchesProject && matchesStatus;
    });

    // Apply sorting
    filtered.sort((a: any, b: any) => {
      let aVal, bVal;
      
      switch (sortField) {
        case "name":
          aVal = a.name;
          bVal = b.name;
          break;
        case "host":
          aVal = a.host;
          bVal = b.host;
          break;
        case "project":
          aVal = a.project || "default";
          bVal = b.project || "default";
          break;
        case "status":
          aVal = a.status;
          bVal = b.status;
          break;
        default:
          return 0;
      }

      if (aVal < bVal) return sortDirection === "asc" ? -1 : 1;
      if (aVal > bVal) return sortDirection === "asc" ? 1 : -1;
      return 0;
    });

    return filtered;
  }, [instances, searchQuery, filterNode, filterProject, filterStatus, sortField, sortDirection]);

  // Pagination
  const totalPages = Math.ceil(filteredInstances.length / pageSize);
  const paginatedInstances = useMemo(() => {
    const startIndex = (currentPage - 1) * pageSize;
    return filteredInstances.slice(startIndex, startIndex + pageSize);
  }, [filteredInstances, currentPage, pageSize]);

  // Full-page loading state
  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <Box className="h-8 w-8" />
            Instances
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor instances (containers and virtual machines)
          </p>
        </div>
        <Button onClick={() => refetch()} disabled={isRefetching} size="sm" className="gap-2">
          <RotateCw className={`h-4 w-4 ${isRefetching ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">Refresh</span>
        </Button>
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
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("name")}
                >
                  Name {sortField === "name" && (sortDirection === "asc" ? <ArrowUp className="h-3 w-3 inline ml-1" /> : <ArrowDown className="h-3 w-3 inline ml-1" />)}
                </TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("status")}
                >
                  Status {sortField === "status" && (sortDirection === "asc" ? <ArrowUp className="h-3 w-3 inline ml-1" /> : <ArrowDown className="h-3 w-3 inline ml-1" />)}
                </TableHead>
                <TableHead>Type</TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("host")}
                >
                  Node {sortField === "host" && (sortDirection === "asc" ? <ArrowUp className="h-3 w-3 inline ml-1" /> : <ArrowDown className="h-3 w-3 inline ml-1" />)}
                </TableHead>
                <TableHead 
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("project")}
                >
                  Project {sortField === "project" && (sortDirection === "asc" ? <ArrowUp className="h-3 w-3 inline ml-1" /> : <ArrowDown className="h-3 w-3 inline ml-1" />)}
                </TableHead>
                <TableHead>IP Addresses</TableHead>
                <TableHead>Profiles</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedInstances.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={8} className="text-center text-muted-foreground py-8">
                    No instances found
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
                    <div className="flex gap-2">
                      {instance.status !== "Running" && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            handleAction(instance.name, instance.host, "start")
                          }
                          disabled={actionLoading === `${instance.name}-start` || isReadOnly}
                          title={isReadOnly ? "Action disabled in read-only mode" : "Start instance"}
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
                            disabled={actionLoading === `${instance.name}-stop` || isReadOnly}
                            title={isReadOnly ? "Action disabled in read-only mode" : "Stop instance"}
                          >
                            <Square className="h-4 w-4" />
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              handleAction(instance.name, instance.host, "restart")
                            }
                            disabled={actionLoading === `${instance.name}-restart` || isReadOnly}
                            title={isReadOnly ? "Action disabled in read-only mode" : "Restart instance"}
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
                            title="Open Terminal"
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
                            title="Open Console"
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
