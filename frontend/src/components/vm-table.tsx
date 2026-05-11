"use client";

import * as React from "react";
import {
  Play,
  Square,
  RotateCw,
  Trash2,
  Server,
  Container,
  Terminal,
  Loader2,
} from "lucide-react";
import { useTerminal } from "@/hooks/use-terminal";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { VMInfo, VMStatus } from "@/types";
import {
  useStartVM,
  useStopVM,
  useRestartVM,
  useDeleteVM,
} from "@/hooks/use-lxd";
import { toast } from "@/hooks/use-toast";
import { usePermissions } from "@/hooks/use-permissions";
import { cn } from "@/lib/utils";

interface VMTableProps {
  vms: VMInfo[];
  isLoading?: boolean;
}

function getStatusBadgeVariant(
  status: VMStatus
): "success" | "error" | "warning" | "muted" {
  switch (status) {
    case "Running":
      return "success";
    case "Stopped":
      return "muted";
    case "Frozen":
      return "warning";
    case "Error":
      return "error";
    default:
      return "muted";
  }
}

function StatusIndicator({ status }: { status: VMStatus }) {
  const isRunning = status === "Running";
  return (
    <div className="flex items-center gap-2">
      <div
        className={cn(
          "h-2 w-2 rounded-full",
          isRunning ? "bg-emerald-500 animate-pulse" : "bg-zinc-400"
        )}
      />
      <Badge variant={getStatusBadgeVariant(status)}>{status}</Badge>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <TableRow>
      <TableCell colSpan={5}>
        <div className="space-y-3">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="flex items-center gap-4">
              <Skeleton className="h-10 w-10 rounded-lg" />
              <div className="space-y-2 flex-1">
                <Skeleton className="h-4 w-[200px]" />
                <Skeleton className="h-3 w-[150px]" />
              </div>
              <Skeleton className="h-6 w-16 rounded-full" />
              <Skeleton className="h-4 w-24" />
              <div className="flex gap-2">
                <Skeleton className="h-8 w-8 rounded-md" />
                <Skeleton className="h-8 w-8 rounded-md" />
                <Skeleton className="h-8 w-8 rounded-md" />
                <Skeleton className="h-8 w-8 rounded-md" />
              </div>
            </div>
          ))}
        </div>
      </TableCell>
    </TableRow>
  );
}

function VMActionButtons({ vm }: { vm: VMInfo }) {
  const startMutation = useStartVM();
  const stopMutation = useStopVM();
  const restartMutation = useRestartVM();
  const deleteMutation = useDeleteVM();
  const { openTerminal, hasTerminal } = useTerminal();
  const { isPartner } = usePermissions();

  const isRunning = vm.status === "Running";
  const isStopped = vm.status === "Stopped";
  const isFrozen = vm.status === "Frozen";
  const hasActiveTerminal = hasTerminal(vm.name, vm.host);
  const isLoading =
    startMutation.isPending ||
    stopMutation.isPending ||
    restartMutation.isPending ||
    deleteMutation.isPending;

  const handleOpenTerminal = () => {
    if (!isRunning) {
      toast({
        title: "Cannot Open Terminal",
        description: "Instance must be running to open a terminal.",
        variant: "destructive",
      });
      return;
    }
    openTerminal({
      instanceName: vm.name,
      host: vm.host,
      project: vm.project || "default",
    });
  };

  const handleStart = async () => {
    try {
      await startMutation.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" },
      });
      toast({
        title: "VM Started",
        description: `${vm.name} is now starting up.`,
        variant: "success",
      });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Unknown error occurred";
      const isReadOnlyError = errorMessage.toLowerCase().includes("read-only mode") || errorMessage.includes("read-only");
      
      toast({
        title: isReadOnlyError ? "Read-Only Mode Active" : "Failed to start VM",
        description: isReadOnlyError 
          ? "Cannot start instances while the system is in read-only mode."
          : errorMessage,
        variant: "destructive",
      });
    }
  };

  const handleStop = async () => {
    try {
      await stopMutation.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" },
      });
      toast({
        title: "VM Stopped",
        description: `${vm.name} has been stopped.`,
        variant: "default",
      });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Unknown error occurred";
      const isReadOnlyError = errorMessage.toLowerCase().includes("read-only mode") || errorMessage.includes("read-only");
      
      toast({
        title: isReadOnlyError ? "Read-Only Mode Active" : "Failed to stop VM",
        description: isReadOnlyError 
          ? "Cannot stop instances while the system is in read-only mode."
          : errorMessage,
        variant: "destructive",
      });
    }
  };

  const handleRestart = async () => {
    try {
      await restartMutation.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" },
      });
      toast({
        title: "VM Restarted",
        description: `${vm.name} is restarting.`,
        variant: "success",
      });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Unknown error occurred";
      const isReadOnlyError = errorMessage.toLowerCase().includes("read-only mode") || errorMessage.includes("read-only");
      
      toast({
        title: isReadOnlyError ? "Read-Only Mode Active" : "Failed to restart VM",
        description: isReadOnlyError 
          ? "Cannot restart instances while the system is in read-only mode."
          : errorMessage,
        variant: "destructive",
      });
    }
  };

  const handleDelete = async () => {
    try {
      await deleteMutation.mutateAsync({
        name: vm.name,
        host: vm.host,
        force: true,
        project: vm.project || "default",
      });
      toast({
        title: "VM Deleted",
        description: `${vm.name} has been deleted.`,
        variant: "default",
      });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Unknown error occurred";
      const isReadOnlyError = errorMessage.toLowerCase().includes("read-only mode") || errorMessage.includes("read-only");
      
      toast({
        title: isReadOnlyError ? "Read-Only Mode Active" : "Failed to delete VM",
        description: isReadOnlyError 
          ? "Cannot delete instances while the system is in read-only mode."
          : errorMessage,
        variant: "destructive",
      });
    }
  };

  return (
    <TooltipProvider delayDuration={100}>
      <div className="flex items-center gap-1">
        {/* Terminal Button */}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className={cn(
                "h-8 w-8",
                hasActiveTerminal
                  ? "text-blue-500 bg-blue-50 dark:bg-blue-950"
                  : "text-blue-600 hover:text-blue-700 hover:bg-blue-50 dark:hover:bg-blue-950",
                !isRunning && "opacity-50"
              )}
              onClick={handleOpenTerminal}
              disabled={!isRunning}
            >
              <Terminal className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            {isRunning 
              ? (hasActiveTerminal ? "Focus Terminal" : "Open Terminal") 
              : "Instance must be running"}
          </TooltipContent>
        </Tooltip>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-emerald-600 hover:text-emerald-700 hover:bg-emerald-50 dark:hover:bg-emerald-950"
              onClick={handleStart}
              disabled={isRunning || isLoading || isFrozen}
            >
              {startMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Play className="h-4 w-4" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{isFrozen ? "Frozen by billing policy" : "Start"}</TooltipContent>
        </Tooltip>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-amber-600 hover:text-amber-700 hover:bg-amber-50 dark:hover:bg-amber-950"
              onClick={handleStop}
              disabled={isStopped || isLoading}
            >
              {stopMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Square className="h-4 w-4" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent>Stop</TooltipContent>
        </Tooltip>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 text-blue-600 hover:text-blue-700 hover:bg-blue-50 dark:hover:bg-blue-950"
              onClick={handleRestart}
              disabled={isStopped || isLoading || isFrozen}
            >
              {restartMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RotateCw className="h-4 w-4" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{isFrozen ? "Frozen by billing policy" : "Restart"}</TooltipContent>
        </Tooltip>

        {/* Delete - Partner+ only */}
        {isPartner && (
        <AlertDialog>
          <Tooltip>
            <TooltipTrigger asChild>
              <AlertDialogTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-red-600 hover:text-red-700 hover:bg-red-50 dark:hover:bg-red-950"
                  disabled={isLoading}
                >
                  {deleteMutation.isPending ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Trash2 className="h-4 w-4" />
                  )}
                </Button>
              </AlertDialogTrigger>
            </TooltipTrigger>
            <TooltipContent>Delete</TooltipContent>
          </Tooltip>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete {vm.name}?</AlertDialogTitle>
              <AlertDialogDescription>
                This action cannot be undone. This will permanently delete the
                virtual machine and all its data from {vm.host}.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={handleDelete}
                className="bg-red-600 hover:bg-red-700"
              >
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
        )}
      </div>
    </TooltipProvider>
  );
}

export function VMTable({ vms, isLoading }: VMTableProps) {
  // Group VMs by project
  const groupedVMs = React.useMemo(() => {
    const groups: Record<string, VMInfo[]> = {};
    vms.forEach((vm) => {
      const project = vm.project || vm.config?.project || "default";
      if (!groups[project]) {
        groups[project] = [];
      }
      groups[project].push(vm);
    });
    return groups;
  }, [vms]);

  const projectNames = Object.keys(groupedVMs).sort();

  if (isLoading) {
    return (
      <div className="rounded-xl border bg-card overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow className="bg-muted/50 hover:bg-muted/50">
              <TableHead className="w-[300px]">Instance</TableHead>
              <TableHead className="w-[120px]">Status</TableHead>
              <TableHead className="w-[200px]">IP Addresses</TableHead>
              <TableHead className="w-[100px]">Host</TableHead>
              <TableHead className="w-[160px] text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <LoadingSkeleton />
          </TableBody>
        </Table>
      </div>
    );
  }

  if (vms.length === 0) {
    return (
      <div className="rounded-xl border bg-card overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow className="bg-muted/50 hover:bg-muted/50">
              <TableHead className="w-[300px]">Instance</TableHead>
              <TableHead className="w-[120px]">Status</TableHead>
              <TableHead className="w-[200px]">IP Addresses</TableHead>
              <TableHead className="w-[100px]">Host</TableHead>
              <TableHead className="w-[160px] text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow>
              <TableCell
                colSpan={5}
                className="h-32 text-center text-muted-foreground"
              >
                <div className="flex flex-col items-center gap-2">
                  <Server className="h-8 w-8 opacity-40" />
                  <span>No virtual machines found</span>
                </div>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {projectNames.map((projectName) => {
        const projectVMs = groupedVMs[projectName];
        const runningCount = projectVMs.filter(vm => vm.status === "Running").length;
        const stoppedCount = projectVMs.filter(vm => vm.status === "Stopped").length;
        
        return (
          <div key={projectName} className="space-y-3">
            <div className="flex items-center gap-3">
              <h3 className="text-lg font-semibold">
                Project: <span className="text-violet-600 dark:text-violet-400">{projectName}</span>
              </h3>
              <Badge variant="outline" className="font-mono">
                {projectVMs.length} instance{projectVMs.length !== 1 ? 's' : ''}
              </Badge>
              <Badge variant="success" className="font-mono">
                {runningCount} running
              </Badge>
              <Badge variant="muted" className="font-mono">
                {stoppedCount} stopped
              </Badge>
            </div>
            
            <div className="rounded-xl border bg-card overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow className="bg-muted/50 hover:bg-muted/50">
                    <TableHead className="w-[300px]">Instance</TableHead>
                    <TableHead className="w-[120px]">Status</TableHead>
                    <TableHead className="w-[200px]">IP Addresses</TableHead>
                    <TableHead className="w-[100px]">Host</TableHead>
                    <TableHead className="w-[160px] text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {projectVMs.map((vm, index) => (
                    <TableRow
                      key={`${vm.host}-${vm.project || "default"}-${vm.name}`}
                      className="animate-fade-in"
                      style={{ animationDelay: `${index * 50}ms` }}
                    >
                      <TableCell>
                        <div className="flex items-center gap-3">
                          <div
                            className={cn(
                              "flex h-10 w-10 items-center justify-center rounded-lg",
                              vm.type === "virtual-machine"
                                ? "bg-violet-100 text-violet-600 dark:bg-violet-950 dark:text-violet-400"
                                : "bg-cyan-100 text-cyan-600 dark:bg-cyan-950 dark:text-cyan-400"
                            )}
                          >
                            {vm.type === "virtual-machine" ? (
                              <Server className="h-5 w-5" />
                            ) : (
                              <Container className="h-5 w-5" />
                            )}
                          </div>
                          <div>
                            <div className="font-medium">{vm.name}</div>
                            <div className="text-xs text-muted-foreground">
                              {vm.type === "virtual-machine" ? "VM" : "Container"} •{" "}
                              {vm.architecture || "N/A"}
                            </div>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <StatusIndicator status={vm.status} />
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-col gap-0.5">
                          {vm.ip_addresses.length > 0 ? (
                            vm.ip_addresses.slice(0, 2).map((ip, idx) => (
                              <code
                                key={idx}
                                className="text-xs font-mono bg-muted px-1.5 py-0.5 rounded w-fit"
                              >
                                {ip}
                              </code>
                            ))
                          ) : (
                            <span className="text-xs text-muted-foreground">
                              No IP assigned
                            </span>
                          )}
                          {vm.ip_addresses.length > 2 && (
                            <span className="text-xs text-muted-foreground">
                              +{vm.ip_addresses.length - 2} more
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="font-mono text-xs">
                          {vm.host}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <VMActionButtons vm={vm} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </div>
        );
      })}
    </div>
  );
}
