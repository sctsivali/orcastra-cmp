"use client";

import * as React from "react";
// note: these alert cards all point at /inventory/warnings with distinct query strings,
// which are distinct router cache keys. Prefetching them on sight cost seven RSC requests
// on every dashboard paint, so they warm on intent like the sidebar does.
import { IntentLink as Link } from "@/components/navigation/route-intent";
import { FolderKanban, ChevronDown, ChevronUp, Server, Container, Play, Square, RotateCw, Trash2, MoreVertical, Cpu, HardDrive, MemoryStick, Settings, Terminal, AlertTriangle, Info, Flame, ShieldAlert, Shield, WifiOff, ChevronRight, Monitor, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { LoadingLottieMediumEnhanced, LoadingLottiePageEnhanced } from "@/components/loading-lottie";
import { NodeStatusCards } from "@/components/node-status";
import { useRelativeTime } from "@/hooks/use-relative-time";
import { StatsOverview } from "@/components/stats-overview";
import { InstanceDetailDialog } from "@/components/instance-detail/instance-detail-dialog";
import { TagManager, TagFilter } from "@/components/tag-manager";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger } from "@/components/ui/tooltip";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger } from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue } from "@/components/ui/select";
import { useInstances, useAllProjects, useStartVM, useStopVM, useRestartVM, useDeleteVM, useDeleteProject, useUpdateProject, useUpdateInstanceTags, useUpdateProjectTags, useClusters, useWarnings, useSystemWarnings, queryKeys } from "@/hooks/use-lxd";
import { useQueryClient } from "@tanstack/react-query";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { usePermissions } from "@/hooks/use-permissions";
import { toast } from "@/hooks/use-toast";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";
import { VMInfo, ProjectListResponse, ProjectInfo } from "@/types";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { OverflowText } from "@/components/ui/overflow-text";

interface ProjectsSectionProps {
  projectsData: ProjectListResponse[] | undefined;
  projectsLoading: boolean;
  vmsData: VMInfo[];
  projectInstanceCounts: Map<string, number>;
  refetch: () => void;
  lastUpdatedAt: number | null;
}

interface ProjectCardProps {
  project: ProjectInfo;
  projectKey: string;
  instanceCount: number;
  isExpanded: boolean;
  instances: VMInfo[];
  stats: { running: number; stopped: number; other: number; total: number };
  toggleProject: (key: string) => void;
  refetch: () => void;
  isReadOnly?: boolean;
}

interface InstanceRowProps {
  vm: VMInfo;
  onTagsUpdate?: () => void;
  isReadOnly?: boolean;
}

function InstanceRow({ vm, onTagsUpdate, isReadOnly = false }: InstanceRowProps) {
  const startVM = useStartVM();
  const stopVM = useStopVM();
  const restartVM = useRestartVM();
  const deleteVM = useDeleteVM();
  const updateInstanceTags = useUpdateInstanceTags();
  const [isDeleting, setIsDeleting] = React.useState(false);
  const [showMonitoring, setShowMonitoring] = React.useState(false);
  const { isPartner } = usePermissions();
  const { hasPermission } = useTenantAccess();

  // tenant permission checks for this instance
  const canOperate = hasPermission(vm.host, vm.project || "default", vm.name, "operate");
  const canConsole = hasPermission(vm.host, vm.project || "default", vm.name, "console");

  // important: an instance name is unique only within one project on one host, and the projects
  // grid can hold two expanded cards at once, so `Start instance web-01` was capable of naming
  // two different buttons on one screen. check-icon-button-names.mjs counts buttons with no name,
  // never buttons whose name is ambiguous, so nothing was catching it.
  const rowLabel = `${vm.name} in ${vm.project || "default"} on ${vm.host}`;

  const handleTagsChange = async (newTags: string[]) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Tag updates are disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    try {
      await updateInstanceTags.mutateAsync({
        host: vm.host,
        instanceName: vm.name,
        tags: newTags,
        project: vm.project || "default" });
      toast({
        title: "Tags Updated",
        description: `Tags for ${vm.name} have been updated.`,
        variant: "success" });
      onTagsUpdate?.();
    } catch (error) {
      toast({
        title: "Failed to update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  const handleStart = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance actions are disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    try {
      await startVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" } });
      toast({
        title: "VM Started",
        description: `${vm.name} is now starting.`,
        variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to start VM",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  const handleStop = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance actions are disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    try {
      await stopVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" } });
      toast({
        title: "VM Stopped",
        description: `${vm.name} is now stopping.`,
        variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to stop VM",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  const handleRestart = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance actions are disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    try {
      await restartVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" } });
      toast({
        title: "VM Restarted",
        description: `${vm.name} is now restarting.`,
        variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to restart VM",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  const handleDelete = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance deletion is disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    setIsDeleting(true);
    try {
      await deleteVM.mutateAsync({
        name: vm.name,
        host: vm.host,
        force: true,
        project: vm.project || "default" });
      toast({
        title: "VM Deleted",
        description: `${vm.name} has been deleted.`,
        variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to delete VM",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    } finally {
      setIsDeleting(false);
    }
  };

  const isRunning = vm.status === "Running";
  const isPending = startVM.isPending || stopVM.isPending || restartVM.isPending;

  return (
    <div className="p-2 md:p-3 rounded-lg bg-muted/30 hover:bg-muted/50 transition-colors border">
      <div className="flex flex-col sm:flex-row items-start gap-2 md:gap-3">
        {/* Left: Instance Info - Clickable */}
        <div 
          className="flex-1 w-full sm:min-w-0 space-y-2 cursor-pointer" 
          onClick={() => setShowMonitoring(true)}
        >
          <div className="flex items-center gap-2 flex-wrap">
            {vm.type === "virtual-machine" ? (
              <Server className="h-4 w-4 text-muted-foreground flex-shrink-0" />
            ) : (
              <Container className="h-4 w-4 text-muted-foreground flex-shrink-0" />
            )}
            <span className="text-sm font-medium truncate">{vm.name}</span>
            <Badge variant={isRunning ? "success" : "secondary"} className="text-xs">
              {vm.status}
            </Badge>
          </div>
          
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            {/* Type */}
            <div className="flex items-center gap-1">
              <span className="font-medium">Type:</span>
              <span>{vm.type === "virtual-machine" ? "VM" : "Container"}</span>
            </div>
            
            {/* Architecture */}
            {vm.architecture && (
              <div className="flex items-center gap-1">
                <span className="font-medium">Arch:</span>
                <span>{vm.architecture}</span>
              </div>
            )}
            
            {/* IP Address */}
            {vm.ip_addresses && vm.ip_addresses.length > 0 && (
              <div className="flex items-center gap-1">
                <span className="font-medium">IP:</span>
                <span>{vm.ip_addresses[0]}</span>
              </div>
            )}
          </div>

          {/* Tags */}
          <div className="mt-2" onClick={(e) => e.stopPropagation()}>
            <TagManager
              tags={vm.tags || []}
              onTagsChange={handleTagsChange}
              readonly={isReadOnly}
              maxTags={10}
              placeholder="Add instance tag..."
            />
          </div>
        </div>

        {/* Right: Actions */}
        <TooltipProvider>
          <div className="flex items-center gap-1 shrink-0">
            {/* Start */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={handleStart}
                  aria-label={`Start instance ${rowLabel}`}
                  disabled={isRunning || isPending || isReadOnly || !canOperate}
                >
                  {startVM.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{!canOperate ? "No operate permission" : isReadOnly ? "Disabled in read-only mode" : "Start"}</TooltipContent>
            </Tooltip>

            {/* Stop */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={handleStop}
                  aria-label={`Stop instance ${rowLabel}`}
                  disabled={!isRunning || isPending || isReadOnly || !canOperate}
                >
                  {stopVM.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Square className="h-3.5 w-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{!canOperate ? "No operate permission" : isReadOnly ? "Disabled in read-only mode" : "Stop"}</TooltipContent>
            </Tooltip>

            {/* Restart */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={handleRestart}
                  aria-label={`Restart instance ${rowLabel}`}
                  disabled={!isRunning || isPending || isReadOnly || !canOperate}
                >
                  {restartVM.isPending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RotateCw className="h-3.5 w-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{!canOperate ? "No operate permission" : isReadOnly ? "Disabled in read-only mode" : "Restart"}</TooltipContent>
            </Tooltip>

            {/* Terminal */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => {
                    useTerminalStore.getState().openTerminal({
                      instanceName: vm.name,
                      host: vm.host,
                      project: vm.project || "default" });
                  }}
                  disabled={!isRunning || !canConsole}
                  title={!canConsole ? "No console permission" : !isRunning ? "Instance must be running" : "Open Terminal"}
                  aria-label={`Open terminal for instance ${rowLabel}`}
                >
                  <Terminal className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{!canConsole ? "No console permission" : !isRunning ? "Instance must be running" : "Open Terminal"}</TooltipContent>
            </Tooltip>

            {/* Console */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => {
                    useConsoleStore.getState().openConsole({
                      instanceName: vm.name,
                      instanceType: vm.type,
                      host: vm.host,
                      project: vm.project || "default" });
                  }}
                  disabled={!isRunning || !canConsole}
                  title={!canConsole ? "No console permission" : !isRunning ? "Instance must be running" : "Open Console"}
                  aria-label={`Open console for instance ${rowLabel}`}
                >
                  <Monitor className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{!canConsole ? "No console permission" : !isRunning ? "Instance must be running" : "Open Console"}</TooltipContent>
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
                      className="h-7 w-7 text-destructive hover:text-destructive"
                      disabled={isPending || isDeleting || isReadOnly}
                      aria-label={`Delete instance ${rowLabel}`}
                    >
                      {isDeleting ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Trash2 className="h-3.5 w-3.5" />
                      )}
                    </Button>
                  </AlertDialogTrigger>
                </TooltipTrigger>
                <TooltipContent>{isReadOnly ? "Disabled in read-only mode" : "Delete"}</TooltipContent>
              </Tooltip>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Delete {vm.name}?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This will permanently delete the instance. This action cannot be undone.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={handleDelete}
                    className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                  >
                    Delete
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
            )}
          </div>
        </TooltipProvider>
      </div>
      
      {/* Monitoring Modal */}
      <InstanceDetailDialog
        instance={vm}
        isOpen={showMonitoring}
        onClose={() => setShowMonitoring(false)}
        project="default"
      />
    </div>
  );
}

// A quota chip prints "19.9 of 512.0" over "GiB", so both numbers have to share one unit. The
// unit comes from the limit and stops at TiB, which is what keeps the value line short enough for
// three chips to sit across a card: an 8 TiB pool would otherwise print "0.0 of 8192.0".
function formatQuota(usedBytes: number, limitBytes: number): { value: string; unit: string } {
  const GIB = 1024 * 1024 * 1024;
  const useTiB = limitBytes >= 1024 * GIB;
  const divisor = useTiB ? 1024 * GIB : GIB;

  return {
    value: `${(usedBytes / divisor).toFixed(1)} of ${(limitBytes / divisor).toFixed(1)}`,
    unit: useTiB ? "TiB" : "GiB",
  };
}

function ProjectCard({ project, projectKey, instanceCount, isExpanded, instances, stats, toggleProject, refetch, isReadOnly = false }: ProjectCardProps) {
  const deleteProject = useDeleteProject();
  const updateProject = useUpdateProject();
  const updateProjectTags = useUpdateProjectTags();
  const [deleteDialogOpen, setDeleteDialogOpen] = React.useState(false);
  const [isDeleting, setIsDeleting] = React.useState(false);
  const [editDialogOpen, setEditDialogOpen] = React.useState(false);
  const [isSaving, setIsSaving] = React.useState(false);
  const { isPartner } = usePermissions();

  const handleTagsChange = async (newTags: string[]) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Tag updates are disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    try {
      await updateProjectTags.mutateAsync({
        host: project.host,
        projectName: project.name,
        tags: newTags });
      toast({
        title: "Tags Updated",
        description: `Tags for project '${project.name}' have been updated.`,
        variant: "success" });
      refetch();
    } catch (error) {
      toast({
        title: "Failed to update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };
  
  // form state for editing
  const [editName, setEditName] = React.useState(project.name);
  const [editDescription, setEditDescription] = React.useState(project.description || "");
  const [editMaxCpu, setEditMaxCpu] = React.useState(0);
  const [editMaxRamValue, setEditMaxRamValue] = React.useState(0);
  const [editMaxRamUnit, setEditMaxRamUnit] = React.useState<"GiB" | "MiB">("GiB");
  const [editMaxStorageValue, setEditMaxStorageValue] = React.useState(0);
  const [editMaxStorageUnit, setEditMaxStorageUnit] = React.useState<"GiB" | "MiB">("GiB");

  // calculate resource usage from instances
  const resourceUsage = React.useMemo(() => {
    let cpuUsed = 0;
    let ramUsedBytes = 0;
    let storageUsedBytes = 0;

    instances.forEach((vm) => {
      // count CPU
      const vmCpu = vm.config?.["limits.cpu"];
      if (vmCpu) {
        cpuUsed += parseInt(vmCpu) || 0;
      }

      // count RAM
      if (vm.resources?.memory_usage) {
        ramUsedBytes += vm.resources.memory_usage;
      }

      // count storage from disk usage
      if (vm.resources?.disk_usage) {
        Object.values(vm.resources.disk_usage).forEach((bytes) => {
          storageUsedBytes += bytes;
        });
      }
    });

    return { cpuUsed, ramUsedBytes, storageUsedBytes };
  }, [instances]);

  // parse project limits
  const limits = React.useMemo(() => {
    const cpuLimit = project.config?.["limits.cpu"] ? parseInt(project.config["limits.cpu"]) : null;
    
    const ramLimitStr = project.config?.["limits.memory"];
    let ramLimitBytes = null;
    if (ramLimitStr) {
      const match = ramLimitStr.match(/^(\d+(?:\.\d+)?)(GiB|MiB|GB|MB)$/i);
      if (match) {
        const value = parseFloat(match[1]);
        const unit = match[2].toUpperCase();
        ramLimitBytes = unit.includes('G') ? value * 1024 * 1024 * 1024 : value * 1024 * 1024;
      }
    }

    const storageLimitStr = project.config?.["limits.disk"];
    let storageLimitBytes = null;
    if (storageLimitStr) {
      const match = storageLimitStr.match(/^(\d+(?:\.\d+)?)(GiB|MiB|GB|MB)$/i);
      if (match) {
        const value = parseFloat(match[1]);
        const unit = match[2].toUpperCase();
        storageLimitBytes = unit.includes('G') ? value * 1024 * 1024 * 1024 : value * 1024 * 1024;
      }
    }

    return { cpuLimit, ramLimitBytes, storageLimitBytes };
  }, [project.config]);

  // Parse existing resource limits when dialog opens
  React.useEffect(() => {
    if (editDialogOpen && project.config) {
      // Set name and description
      setEditName(project.name);
      setEditDescription(project.description || "");

      // Parse CPU limit (0 = unlimited/no limit set)
      const cpuLimitStr = project.config["limits.cpu"];
      setEditMaxCpu(cpuLimitStr ? parseInt(cpuLimitStr) || 0 : 0);

      // Parse RAM limit (0 = unlimited/no limit set)
      const ramLimitStr = project.config["limits.memory"];
      if (ramLimitStr) {
        const ramMatch = ramLimitStr.match(/^(\d+(?:\.\d+)?)(GiB|MiB)$/i);
        if (ramMatch) {
          setEditMaxRamValue(parseFloat(ramMatch[1]));
          setEditMaxRamUnit(ramMatch[2] as "GiB" | "MiB");
        } else {
          setEditMaxRamValue(0);
        }
      } else {
        setEditMaxRamValue(0);
      }

      // Parse Storage limit (0 = unlimited/no limit set)
      const storageLimitStr = project.config["limits.disk"];
      if (storageLimitStr) {
        const storageMatch = storageLimitStr.match(/^(\d+(?:\.\d+)?)(GiB|MiB)$/i);
        if (storageMatch) {
          setEditMaxStorageValue(parseFloat(storageMatch[1]));
          setEditMaxStorageUnit(storageMatch[2] as "GiB" | "MiB");
        } else {
          setEditMaxStorageValue(0);
        }
      } else {
        setEditMaxStorageValue(0);
      }
    }
  }, [editDialogOpen, project.config, project.description, project.name]);

  // important: isPending is React state, so the disabled prop only updates on the next render.
  // Two clicks inside one tick both fire the mutation. Claimed synchronously, before any await.
  const submitInFlightRef = React.useRef(false);

  const handleSave = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Project updates are disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setIsSaving(true);
    try {
      const updateData: {
        description?: string;
        max_cpu?: number;
        max_ram?: string;
        max_storage?: string;
      } = {};

      // Always include description
      updateData.description = editDescription;

      // CPU: 0 = unlimited (send 0 to clear limit), >0 = set limit
      // null/undefined = no change (field omitted)
      updateData.max_cpu = editMaxCpu; // always send - 0 clears the limit

      // RAM: 0 = unlimited (send "0" to clear limit), >0 = set limit
      updateData.max_ram = editMaxRamValue > 0
        ? `${editMaxRamValue}${editMaxRamUnit}`
        : "0";

      // Storage: 0 = unlimited (send "0" to clear limit), >0 = set limit
      updateData.max_storage = editMaxStorageValue > 0
        ? `${editMaxStorageValue}${editMaxStorageUnit}`
        : "0";

      await updateProject.mutateAsync({ host: project.host, projectName: project.name, data: updateData });

      toast({
        title: "Project Updated",
        description: `Project '${project.name}' has been updated successfully.`,
        variant: "success" });

      setEditDialogOpen(false);
      refetch(); // Refresh the project list
    } catch (error) {
      toast({
        title: "Failed to update project",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    } finally {
      submitInFlightRef.current = false;
      setIsSaving(false);
    }
  };

  const handleDelete = async (e: React.MouseEvent) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Project deletion is disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    e.stopPropagation();
    setIsDeleting(true);
    try {
      await deleteProject.mutateAsync({
        host: project.host,
        projectName: project.name });
      toast({
        title: "Project Deleted",
        description: `Project '${project.name}' has been deleted from ${project.host}.`,
        variant: "success" });
      setDeleteDialogOpen(false);
    } catch (error) {
      toast({
        title: "Failed to delete project",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <Card className="hover:border-primary/50 transition-colors">
      <CardHeader className="pb-3 px-3 md:px-6">
        <div className="flex flex-col sm:flex-row items-start sm:justify-between gap-2">
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <FolderKanban className="h-4 w-4 text-muted-foreground shrink-0" />
            <CardTitle className="text-base min-w-0 break-words">{project.name}</CardTitle>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Badge variant="outline" className="text-xs shrink-0">
              {project.host}
            </Badge>
            {/* Delete/Edit Button - Partner+ only */}
            {!isReadOnly && isPartner && (
              <>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      aria-label={`Project actions for ${project.name}`}
                    >
                      <MoreVertical className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                  <DropdownMenuItem 
                    className="cursor-pointer"
                    onSelect={() => setEditDialogOpen(true)}
                  >
                    <Settings className="mr-2 h-4 w-4" />
                    Edit Project
                  </DropdownMenuItem>
                  <DropdownMenuItem 
                    className="text-destructive focus:text-destructive cursor-pointer"
                    onSelect={() => setDeleteDialogOpen(true)}
                  >
                    <Trash2 className="mr-2 h-4 w-4" />
                    Delete Project
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
              <AlertDialogContent onClick={(e) => e.stopPropagation()}>
                <AlertDialogHeader>
                  <AlertDialogTitle>Delete Project &quot;{project.name}&quot;?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This will permanently delete the project from {project.host}. 
                    {instanceCount > 0 && (
                      <span className="block mt-2 text-destructive font-medium">
                        Warning: This project has {instanceCount} instance(s). Delete all instances first.
                      </span>
                    )}
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={handleDelete}
                    disabled={isDeleting || instanceCount > 0}
                    className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                  >
                    {isDeleting ? (
                      <>
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        Deleting...
                      </>
                    ) : (
                      "Delete Project"
                    )}
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
            </>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {project.description && (
          // note: the card itself is clickable, so the reveal has to stop its click from also
          // opening the project. Same guard the tags row below already uses.
          <div className="text-xs text-muted-foreground" onClick={(e) => e.stopPropagation()}>
            <OverflowText
              value={project.description}
              label={`description for project ${project.name}`}
              lines={2}
            />
          </div>
        )}

        {/* Tags */}
        <div 
          className="pt-1"
          onClick={(e) => e.stopPropagation()}
        >
          <TagManager
            tags={project.tags || []}
            onTagsChange={handleTagsChange}
            readonly={isReadOnly}
            maxTags={10}
            placeholder="Add project tag..."
          />
        </div>
        
        {/* Resource Usage */}
        {/* important: the chip grid sizes itself from the card, not from a viewport breakpoint.
            The sidebar collapses, so one viewport gives two different card widths and an `lg:`
            rule would lie about the room available. `auto-fit` measures the real container: three
            chips across a 385px card at 1728, two then one as the card narrows, so a chip is
            never handed less room than its text needs. The 104px floor is measured rather than
            guessed. Chip chrome costs 36px (padding, icon, gap, border) and the widest short
            value, "Unlimited", is 59px in Geist 12px/600, so even the narrowest chip clears it.
            note: the value wraps, it does not truncate. A quota is what an operator acts on, and
            a wrapped "19.9 of 512.0" still says 512 where a clipped one says "19.9 of 5". */}
        <div className="grid gap-2 pt-2 grid-cols-[repeat(auto-fit,minmax(104px,1fr))]">
          {[
            {
              key: "cpu",
              Icon: Cpu,
              label: "CPU",
              spec: limits.cpuLimit
                ? { value: `${resourceUsage.cpuUsed} of ${limits.cpuLimit}`, unit: "cores" }
                : { value: "Unlimited", unit: "CPU" },
            },
            {
              key: "ram",
              Icon: MemoryStick,
              label: "RAM",
              spec: limits.ramLimitBytes
                ? formatQuota(resourceUsage.ramUsedBytes, limits.ramLimitBytes)
                : { value: "Unlimited", unit: "RAM" },
            },
            {
              key: "storage",
              Icon: HardDrive,
              label: "Storage",
              spec: limits.storageLimitBytes
                ? formatQuota(resourceUsage.storageUsedBytes, limits.storageLimitBytes)
                : { value: "Unlimited", unit: "Storage" },
            },
          ].map(({ key, Icon, label, spec }) => (
            <div
              key={key}
              className="flex items-center gap-1.5 px-1.5 py-2 rounded-md bg-muted/40 border border-border"
            >
              <Icon className="h-4 w-4 text-foreground/80 shrink-0" />
              <div className="flex flex-col min-w-0 break-words">
                {/* the icon is the only thing naming CPU against RAM against Storage once the
                    unit line reads "GiB" twice, so a screen reader gets the name in words. */}
                {spec.unit !== label && <span className="sr-only">{label}</span>}
                <span className="text-xs font-semibold text-foreground/80">{spec.value}</span>
                <span className="text-[10px] text-muted-foreground">{spec.unit}</span>
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-2 pt-2 border-t">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">Instances</span>
            <div className="flex items-center gap-2">
              <Badge 
                variant={instanceCount > 0 ? "default" : "secondary"}
                className={instanceCount > 0 ? "cursor-pointer hover:bg-primary/80 transition-colors" : ""}
                onClick={() => instanceCount > 0 && toggleProject(projectKey)}
              >
                {instanceCount}
              </Badge>
              {instanceCount > 0 && (
                <div onClick={() => toggleProject(projectKey)} className="cursor-pointer">
                  {isExpanded ? 
                    <ChevronUp className="h-4 w-4 text-muted-foreground" /> : 
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  }
                </div>
              )}
            </div>
          </div>
          {instanceCount > 0 && (
            <div className="flex items-center gap-2 text-xs">
              <div className="flex items-center gap-1">
                <div className="h-2 w-2 rounded-full bg-status-online" />
                <span className="text-muted-foreground">{stats.running} running</span>
              </div>
              <div className="flex items-center gap-1">
                <div className="h-2 w-2 rounded-full bg-status-unknown" />
                <span className="text-muted-foreground">{stats.stopped} stopped</span>
              </div>
              {stats.other > 0 && (
                <div className="flex items-center gap-1">
                  <div className="h-2 w-2 rounded-full bg-status-pending" />
                  <span className="text-muted-foreground">{stats.other} other</span>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Expanded Instance List */}
        {isExpanded && instanceCount > 0 && (
          <div className="mt-3 pt-3 border-t space-y-2">
            {instances.map((vm) => (
              <InstanceRow key={vm.name} vm={vm} onTagsUpdate={refetch} isReadOnly={isReadOnly} />
            ))}
          </div>
        )}
      </CardContent>

      {/* Edit Resources Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <DialogHeader>
            <DialogTitle>Edit Project</DialogTitle>
            <DialogDescription>
              Update the name, description and resource limits for {project.name}
            </DialogDescription>
          </DialogHeader>
          
          <div className="space-y-4 py-4">
            {/* Project Name */}
            <div className="space-y-2">
              <Label htmlFor="edit-name">Project Name</Label>
              <Input
                id="edit-name"
                placeholder="Project name"
                value={editName}
                disabled
                className="bg-muted cursor-not-allowed"
              />
              <p className="text-xs text-muted-foreground">
                Note: Renaming projects is not supported by the current hypervisor API.
              </p>
            </div>

            {/* Description */}
            <div className="space-y-2">
              <Label htmlFor="edit-description">Description</Label>
              <Textarea
                id="edit-description"
                placeholder="Project description"
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                rows={3}
              />
            </div>

            {/* Max CPU */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="edit-max-cpu">Max CPU Cores</Label>
                {editMaxCpu === 0 && <span className="text-xs text-muted-foreground">Unlimited</span>}
              </div>
              <Input
                id="edit-max-cpu"
                type="number"
                min="0"
                value={editMaxCpu}
                onChange={(e) => setEditMaxCpu(Number(e.target.value))}
                placeholder="0 = Unlimited"
              />
            </div>

            {/* Max RAM */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="edit-max-ram">Max RAM</Label>
                {editMaxRamValue === 0 && <span className="text-xs text-muted-foreground">Unlimited</span>}
              </div>
              <div className="flex gap-2">
                <Input
                  id="edit-max-ram"
                  type="number"
                  min="0"
                  value={editMaxRamValue}
                  onChange={(e) => setEditMaxRamValue(Number(e.target.value))}
                  placeholder="0 = Unlimited"
                  className="flex-1"
                />
                <Select value={editMaxRamUnit} onValueChange={(v) => setEditMaxRamUnit(v as "GiB" | "MiB")}>
                  <SelectTrigger className="w-[100px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="MiB">MiB</SelectItem>
                    <SelectItem value="GiB">GiB</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            {/* Max Storage */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="edit-max-storage">Max Storage</Label>
                {editMaxStorageValue === 0 && <span className="text-xs text-muted-foreground">Unlimited</span>}
              </div>
              <div className="flex gap-2">
                <Input
                  id="edit-max-storage"
                  type="number"
                  min="0"
                  value={editMaxStorageValue}
                  onChange={(e) => setEditMaxStorageValue(Number(e.target.value))}
                  placeholder="0 = Unlimited"
                  className="flex-1"
                />
                <Select value={editMaxStorageUnit} onValueChange={(v) => setEditMaxStorageUnit(v as "GiB" | "MiB")}>
                  <SelectTrigger className="w-[100px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="MiB">MiB</SelectItem>
                    <SelectItem value="GiB">GiB</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setEditDialogOpen(false)}
              disabled={isSaving}
            >
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={isSaving}>
              {isSaving ? "Saving..." : "Save Changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

interface ProjectsContext {
  /** Instance names and tags for a project, indexed once instead of re-scanned per call. */
  index: Map<string, { names: string[]; tags: string[]; count: number }>;
}

// A card grid, so it takes the shared toolbar and filter engine but none of the table
// plumbing. Tags stay behind TagFilter rather than the generic facet popover: getTagColor
// derives a tag's colour from its prefix, so the colour is data here, not decoration.
const PROJECTS = defineTable<ProjectInfo, ProjectsContext>()({
  searchPlaceholder: "Search projects by name, host, description, or instance",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Project", sort: (p) => p.name, search: (p) => p.name, filter: fromSort("text") },
    {
      field: "host",
      label: "Node",
      sort: (p) => p.host,
      search: (p) => p.host,
      facet: (p) => p.host,
      facetSpec: { role: "node", primary: true } },
    { field: "description", label: "Description", sort: (p) => p.description, search: (p) => p.description, filter: fromSort("text") },
    {
      field: "tags",
      label: "Tags",
      facet: (p, ctx) => {
        const entry = ctx.index.get(`${p.host}:${p.name}`);
        return Array.from(new Set([...(p.tags ?? []), ...(entry?.tags ?? [])]));
      },
      facetSpec: { emptyLabel: "(untagged)" } },
  ] });

function ProjectsSection({ projectsData, projectsLoading, vmsData, projectInstanceCounts, refetch, lastUpdatedAt }: ProjectsSectionProps) {
  const lastUpdated = useRelativeTime(lastUpdatedAt);
  const [expandedProjects, setExpandedProjects] = React.useState<Set<string>>(new Set());
  // note: one index built once, instead of getProjectInstances re-scanning the whole
  // instance list inside a predicate that ran from both a reduce and a map on every render.
  const projectIndex = React.useMemo(() => {
    const index = new Map<string, { names: string[]; tags: string[]; count: number }>();
    for (const vm of (vmsData ?? []) as any[]) {
      const key = `${vm.host}:${vm.project || "default"}`;
      const entry = index.get(key) ?? { names: [], tags: [], count: 0 };
      entry.names.push(vm.name);
      if (vm.tags?.length) entry.tags.push(...vm.tags);
      entry.count += 1;
      index.set(key, entry);
    }
    return index;
  }, [vmsData]);

  const allProjects = React.useMemo(
    () => (projectsData ?? []).flatMap((n: any) => n.projects as ProjectInfo[]),
    [projectsData],
  );

  const filterContext = React.useMemo(() => ({ index: projectIndex }), [projectIndex]);

  // The declared accessors see the project row only. Instance names, their tags and the instance
  // count live in the index, so the search reaches them from here and then passes through every
  // active facet like any other hit.
  const searchProjectIndex = React.useCallback(
    (project: ProjectInfo, query: string) => {
      const entry = projectIndex.get(`${project.host}:${project.name}`);
      if (!entry) return false;
      return (
        entry.names.some((n) => n.toLowerCase().includes(query)) ||
        entry.tags.some((t) => t.toLowerCase().includes(query)) ||
        String(entry.count).includes(query)
      );
    },
    [projectIndex],
  );

  const filters = useTableFilters(PROJECTS, allProjects, {
    context: filterContext,
    extraSearch: searchProjectIndex,
    // Tags render through TagFilter rather than the toolbar, so keep the facet out of the
    // toolbar's own controls. It still applies, and it still shows as a removable chip.
    availableFields: PROJECTS.facetFields.filter((f) => f !== "tags") });
  const selectedTags = filters.selectedValues("tags");
  const { isReadOnly } = useReadOnlyMode();

  // Get unique nodes from projects
  // Collect all unique tags from projects and instances
  const allTags = React.useMemo(() => {
    const tags = new Set<string>();
    
    // Tags from projects - handle null/undefined projectsData
    if (projectsData && Array.isArray(projectsData)) {
      projectsData.forEach((nodeProjects) => {
        if (nodeProjects && nodeProjects.projects) {
          nodeProjects.projects.forEach((project) => {
            if (project.tags && Array.isArray(project.tags)) {
              project.tags.forEach((tag) => tags.add(tag));
            }
          });
        }
      });
    }
    
    // Tags from instances
    vmsData.forEach((vm) => {
      vm.tags?.forEach((tag) => tags.add(tag));
    });
    
    return Array.from(tags).sort();
  }, [projectsData, vmsData]);

  const toggleProject = (projectKey: string) => {
    const newExpanded = new Set(expandedProjects);
    if (newExpanded.has(projectKey)) {
      newExpanded.delete(projectKey);
    } else {
      newExpanded.add(projectKey);
    }
    setExpandedProjects(newExpanded);
  };

  const getProjectInstances = (host: string, projectName: string) => {
    return vmsData.filter(vm => 
      vm.host === host && (vm.project || "default") === projectName
    );
  };

  const getProjectStats = (host: string, projectName: string) => {
    const instances = getProjectInstances(host, projectName);
    const running = instances.filter(vm => vm.status === "Running").length;
    const stopped = instances.filter(vm => vm.status === "Stopped").length;
    const other = instances.length - running - stopped;
    return { running, stopped, other, total: instances.length };
  };

  // note: this used to union the index hits back on top of filteredRows. Those extras never went
  // through the facet masks, so a project the Node or Tags facet excluded still rendered a card,
  // and the toolbar counted filteredRows while the grid showed more. The search now reaches the
  // index through the hook's extraSearch, so one set feeds both.
  const visibleProjects = filters.filteredRows;

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <h2 className="text-lg font-semibold">Projects</h2>
          {!!lastUpdatedAt && (
            <p className="text-xs text-muted-foreground">
              Updated {lastUpdated}
            </p>
          )}
        </div>
      </div>

      <TableToolbar filters={filters} noun="projects" />

      {/* Tag Filter: an alternative renderer for the same facet, not a second control. Its
          colours come from the tag prefix, which the generic popover cannot show. */}
      {allTags.length > 0 && (
        <TagFilter
          availableTags={allTags}
          selectedTags={selectedTags}
          onTagsChange={(tags) => filters.setFacet("tags", tags)}
        />
      )}
      
      {/* important: two-up starts at lg and three-up at xl, not md and lg. Measured live, the
          old thresholds gave a 224px card at both 768 and 1024, which leaves the header's title
          column about 52px once the host badge and the kebab have taken their fixed width, so a
          name as short as "Ryo-Labs" broke mid-word. The card needs roughly 340px to hold its own
          header. At 1280 a three-up card is 309px, which clears it, so the widest band is
          unchanged. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
        {projectsLoading ? (
          <div className="col-span-full flex justify-center py-8">
            <LoadingLottieMediumEnhanced showProgress={false} />
          </div>
        ) : (
          <>
            {visibleProjects.map((project) => {
                  const projectKey = `${project.host}:${project.name}`;
                  const instanceCount = projectInstanceCounts.get(projectKey) || 0;
                  const isExpanded = expandedProjects.has(projectKey);
                  const instances = getProjectInstances(project.host, project.name);
                  const stats = getProjectStats(project.host, project.name);
                  
                  return (
                    <ProjectCard
                      key={projectKey}
                      project={project}
                      projectKey={projectKey}
                      instanceCount={instanceCount}
                      isExpanded={isExpanded}
                      instances={instances}
                      stats={stats}
                      toggleProject={toggleProject}
                      refetch={refetch}
                      isReadOnly={isReadOnly}
                    />
                  );
                })}
            {filters.activeFilterCount > 0 && visibleProjects.length === 0 && (
              <div className="col-span-full text-center py-12">
                <FolderKanban className="h-12 w-12 text-muted-foreground mx-auto mb-3 opacity-50" />
                <h3 className="text-lg font-semibold text-muted-foreground mb-2">
                  No projects found
                </h3>
                <p className="text-sm text-muted-foreground">
                  Try adjusting your search query
                </p>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// Alarm Statistics Component
function AlarmStatistics({ prefetchedWarnings, prefetchedSystemWarnings }: { prefetchedWarnings?: any; prefetchedSystemWarnings?: any } = {}) {
  // use prefetched data if available, otherwise fall back to own hooks (standalone usage)
  const { data: warningsDataFetched } = useWarnings();
  const { data: systemWarningsFetched } = useSystemWarnings();
  const { isPartner } = usePermissions();
  
  const warningsData = prefetchedWarnings ?? warningsDataFetched;
  const systemWarnings = prefetchedSystemWarnings ?? systemWarningsFetched;
  
  const actualWarnings = (warningsData as any)?.warnings || [];
  const certWarnings = systemWarnings?.certificate_warnings || [];
  const nodeWarnings = systemWarnings?.node_warnings || [];
  
  // Count warnings by severity
  const counts = React.useMemo(() => {
    const result = { critical: 0, high: 0, warning: 0, info: 0, total: 0, certIssues: 0, inactiveNodes: 0 };
    
    actualWarnings.forEach((w: any) => {
      result.total++;
      const severity = (w.severity || '').toLowerCase();
      if (severity === 'critical' || severity === 'error') result.critical++;
      else if (severity === 'high') result.high++;
      else if (severity === 'warning' || severity === 'medium') result.warning++;
      else result.info++;
    });
    
    certWarnings.forEach((w: any) => {
      result.total++;
      result.certIssues++;
    });
    
    nodeWarnings.forEach(() => {
      result.total++;
      result.inactiveNodes++;
    });
    
    return result;
  }, [actualWarnings, certWarnings, nodeWarnings]);

  // Don't render if no warnings
  if (counts.total === 0) {
    return null;
  }

  // important: this card is platform health, and every tile in it links to a page
  // useRequireRole(Role.PARTNER) bounces a tenant away from. Certificate expiry and inactive
  // nodes are not a tenant's concern, and offering them as navigation is a dead end.
  if (!isPartner) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <div>
          <CardTitle className="text-lg">System Alerts</CardTitle>
          <p className="text-sm text-muted-foreground">Active warnings requiring attention</p>
        </div>
        <Link 
          href="/inventory/warnings" 
          className="text-sm text-primary hover:underline flex items-center gap-1"
        >
          View All ({counts.total})
          <ChevronRight className="h-4 w-4" />
        </Link>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          {/* Critical */}
          <Link 
            href="/inventory/warnings?severity=critical" 
            className="flex items-center gap-3 p-3 rounded-lg border bg-red-500/5 border-red-500/20 hover:bg-red-500/10 hover:border-red-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-red-500/10 group-hover:bg-red-500/20 transition-colors">
              <Flame className="h-4 w-4 text-red-500" />
            </div>
            <div>
              <p className="text-xl font-bold text-red-600">{counts.critical}</p>
              <p className="text-xs text-muted-foreground">Critical</p>
            </div>
          </Link>

          {/* Major/High */}
          <Link 
            href="/inventory/warnings?severity=high" 
            className="flex items-center gap-3 p-3 rounded-lg border bg-orange-500/5 border-orange-500/20 hover:bg-orange-500/10 hover:border-orange-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-orange-500/10 group-hover:bg-orange-500/20 transition-colors">
              <ShieldAlert className="h-4 w-4 text-orange-500" />
            </div>
            <div>
              <p className="text-xl font-bold text-orange-600">{counts.high}</p>
              <p className="text-xs text-muted-foreground">Major</p>
            </div>
          </Link>

          {/* Warning */}
          <Link 
            href="/inventory/warnings?severity=warning" 
            className="flex items-center gap-3 p-3 rounded-lg border bg-yellow-500/5 border-yellow-500/20 hover:bg-yellow-500/10 hover:border-yellow-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-yellow-500/10 group-hover:bg-yellow-500/20 transition-colors">
              <AlertTriangle className="h-4 w-4 text-yellow-500" />
            </div>
            <div>
              <p className="text-xl font-bold text-yellow-600">{counts.warning}</p>
              <p className="text-xs text-muted-foreground">Warning</p>
            </div>
          </Link>

          {/* Info */}
          <Link 
            href="/inventory/warnings?severity=low" 
            className="flex items-center gap-3 p-3 rounded-lg border bg-blue-500/5 border-blue-500/20 hover:bg-blue-500/10 hover:border-blue-500/40 transition-all cursor-pointer group"
          >
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-500/10 group-hover:bg-blue-500/20 transition-colors">
              <Info className="h-4 w-4 text-blue-500" />
            </div>
            <div>
              <p className="text-xl font-bold text-blue-600">{counts.info}</p>
              <p className="text-xs text-muted-foreground">Info</p>
            </div>
          </Link>

          {/* Cert Issues */}
          {counts.certIssues > 0 && (
            <Link 
              href="/inventory/clusters/certificates"
              className="flex items-center gap-3 p-3 rounded-lg border bg-purple-500/5 border-purple-500/20 hover:bg-purple-500/10 hover:border-purple-500/40 transition-all cursor-pointer group"
            >
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-purple-500/10 group-hover:bg-purple-500/20 transition-colors">
                <Shield className="h-4 w-4 text-purple-500" />
              </div>
              <div>
                <p className="text-xl font-bold text-purple-600">{counts.certIssues}</p>
                <p className="text-xs text-muted-foreground">Cert Issues</p>
              </div>
            </Link>
          )}

          {/* Inactive Nodes */}
          {counts.inactiveNodes > 0 && (
            <Link 
              href="/inventory/warnings?tab=nodes"
              className="flex items-center gap-3 p-3 rounded-lg border bg-rose-500/5 border-rose-500/20 hover:bg-rose-500/10 hover:border-rose-500/40 transition-all cursor-pointer group"
            >
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-rose-500/10 group-hover:bg-rose-500/20 transition-colors">
                <WifiOff className="h-4 w-4 text-rose-500" />
              </div>
              <div>
                <p className="text-xl font-bold text-rose-600">{counts.inactiveNodes}</p>
                <p className="text-xs text-muted-foreground">Inactive</p>
              </div>
            </Link>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function Dashboard() {
  const { data: vmsData, isLoading: vmsLoading, refetch, dataUpdatedAt } = useInstances();
  const { data: projectsData, isLoading: projectsLoading } = useAllProjects();
  const { data: clustersData, isLoading: clustersLoading } = useClusters();
  const queryClient = useQueryClient();

  // pre-fetch alarm & node data in parallel with primary queries (eliminates waterfall)
  const { data: warningsData } = useWarnings();
  const { data: systemWarnings } = useSystemWarnings();

  // note: no success toast. The refresh control reports its own outcome inline
  // (spinner, tick, "Updated just now"), and a toast here would evict whatever
  // the operator was actually reading, since TOAST_LIMIT is 1.
  const handleOverviewRefresh = React.useCallback(async () => {
    await Promise.all([
      refetch({ throwOnError: true }),
      queryClient.refetchQueries({ queryKey: queryKeys.allProjects, type: "active" }),
      queryClient.refetchQueries({ queryKey: ["clusters"], type: "active" }),
      queryClient.refetchQueries({ queryKey: queryKeys.nodes, type: "active" }),
      queryClient.refetchQueries({ queryKey: ["warnings"], type: "active" }),
      queryClient.refetchQueries({ queryKey: ["systemWarnings"], type: "active" }),
    ]);
  }, [queryClient, refetch]);

  // calculate instance count per project
  const projectInstanceCounts = new Map<string, number>();
  vmsData?.vms.forEach((vm) => {
    const projectKey = `${vm.host}:${vm.project || "default"}`;
    projectInstanceCounts.set(projectKey, (projectInstanceCounts.get(projectKey) || 0) + 1);
  });

  const isPrimaryLoading = vmsLoading || projectsLoading || clustersLoading;
  // important: never hard-block the dashboard forever on a slow/stuck query.
  // After a grace period, surface a retry panel instead of an infinite 95%
  // loader (the backend now responds fast; this is a defensive safety net).
  const [loaderTimedOut, setLoaderTimedOut] = React.useState(false);
  React.useEffect(() => {
    if (!isPrimaryLoading) {
      setLoaderTimedOut(false);
      return;
    }
    const t = setTimeout(() => setLoaderTimedOut(true), 25000);
    return () => clearTimeout(t);
  }, [isPrimaryLoading]);

  if (isPrimaryLoading && !loaderTimedOut) {
    return <LoadingLottiePageEnhanced context="dashboard" />;
  }

  if (isPrimaryLoading && loaderTimedOut) {
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-center">
        <WifiOff className="h-10 w-10 text-muted-foreground" />
        <div className="space-y-1">
          <p className="text-base font-medium">This is taking longer than usual</p>
          <p className="max-w-md text-sm text-muted-foreground">
            The dashboard could not load all data in time. The backend or a cluster may be slow to respond.
          </p>
        </div>
        <Button
          onClick={() => {
            setLoaderTimedOut(false);
            void handleOverviewRefresh();
          }}
        >
          <RotateCw className="mr-2 h-4 w-4" />
          Retry
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Stats Overview - shows its own loading state */}
      <StatsOverview data={vmsData} clustersData={clustersData} isLoading={vmsLoading} clustersLoading={clustersLoading} />

      {/* Alarm Statistics - Only shows when there are warnings (data already prefetched above) */}
      <AlarmStatistics prefetchedWarnings={warningsData} prefetchedSystemWarnings={systemWarnings} />

      {/* Nodes Status */}
      <NodeStatusCards lastUpdatedAt={dataUpdatedAt} onRefresh={handleOverviewRefresh} />

      {/* Projects Overview */}
        <React.Suspense fallback={<LoadingLottieMediumEnhanced showProgress={false} />}>
        <ProjectsSection 
        projectsData={projectsData} 
        projectsLoading={projectsLoading}
        vmsData={vmsData?.vms || []}
        projectInstanceCounts={projectInstanceCounts}
        refetch={refetch}
        lastUpdatedAt={dataUpdatedAt}
      />
        </React.Suspense>
    </div>
  );
}
