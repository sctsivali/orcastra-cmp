"use client";

import * as React from "react";
import Link from "next/link";
import { RefreshCw, FolderKanban, ChevronDown, ChevronUp, Server, Container, Play, Square, RotateCw, Trash2, MoreVertical, Cpu, HardDrive, MemoryStick, Settings, Tag, Terminal, Bell, AlertTriangle, Info, Flame, ShieldAlert, Shield, WifiOff, ChevronRight, Monitor, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { LoadingLottieSmall, LoadingLottieMediumEnhanced } from "@/components/loading-lottie";
import { VMTable } from "@/components/vm-table";
import { NodeStatusCards } from "@/components/node-status";
import { StatsOverview } from "@/components/stats-overview";
import { InstanceMonitoringModal } from "@/components/instance-monitoring-modal";
import { TagManager, TagFilter } from "@/components/tag-manager";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useInstances, useAllProjects, useStartVM, useStopVM, useRestartVM, useDeleteVM, useDeleteProject, useUpdateProject, useUpdateInstanceTags, useUpdateProjectTags, useClusters, useWarnings, useSystemWarnings } from "@/hooks/use-lxd";
import { useQueryClient } from "@tanstack/react-query";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { usePermissions } from "@/hooks/use-permissions";
import { toast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";
import { VMInfo, ProjectListResponse, ProjectInfo } from "@/types";
import { useTenantAccess } from "@/hooks/use-tenant-access";

interface ProjectsSectionProps {
  projectsData: ProjectListResponse[] | undefined;
  projectsLoading: boolean;
  vmsData: VMInfo[];
  projectInstanceCounts: Map<string, number>;
  refetch: () => void;
  isRefetching: boolean;
  lastUpdated: string | null;
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

  const handleTagsChange = async (newTags: string[]) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Tag updates are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    try {
      await updateInstanceTags.mutateAsync({
        host: vm.host,
        instanceName: vm.name,
        tags: newTags,
        project: vm.project || "default",
      });
      toast({
        title: "Tags Updated",
        description: `Tags for ${vm.name} have been updated.`,
        variant: "success",
      });
      onTagsUpdate?.();
    } catch (error) {
      toast({
        title: "Failed to update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  const handleStart = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance actions are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    try {
      await startVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host },
      });
      toast({
        title: "VM Started",
        description: `${vm.name} is now starting.`,
        variant: "success",
      });
    } catch (error) {
      toast({
        title: "Failed to start VM",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  const handleStop = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance actions are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    try {
      await stopVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host },
      });
      toast({
        title: "VM Stopped",
        description: `${vm.name} is now stopping.`,
        variant: "success",
      });
    } catch (error) {
      toast({
        title: "Failed to stop VM",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  const handleRestart = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance actions are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    try {
      await restartVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host },
      });
      toast({
        title: "VM Restarted",
        description: `${vm.name} is now restarting.`,
        variant: "success",
      });
    } catch (error) {
      toast({
        title: "Failed to restart VM",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  const handleDelete = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Instance deletion is disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    setIsDeleting(true);
    try {
      await deleteVM.mutateAsync({
        name: vm.name,
        host: vm.host,
        force: true,
      });
      toast({
        title: "VM Deleted",
        description: `${vm.name} has been deleted.`,
        variant: "success",
      });
    } catch (error) {
      toast({
        title: "Failed to delete VM",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
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
                  disabled={isRunning || isPending || isReadOnly || !canOperate}
                >
                  {startVM.isPending ? (
                    <LoadingLottieSmall />
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
                  disabled={!isRunning || isPending || isReadOnly || !canOperate}
                >
                  {stopVM.isPending ? (
                    <LoadingLottieSmall />
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
                  disabled={!isRunning || isPending || isReadOnly || !canOperate}
                >
                  {restartVM.isPending ? (
                    <LoadingLottieSmall />
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
                      project: vm.project || "default",
                    });
                  }}
                  disabled={!isRunning || !canConsole}
                  title={!canConsole ? "No console permission" : !isRunning ? "Instance must be running" : "Open Terminal"}
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
                      project: vm.project || "default",
                    });
                  }}
                  disabled={!isRunning || !canConsole}
                  title={!canConsole ? "No console permission" : !isRunning ? "Instance must be running" : "Open Console"}
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
                    >
                      {isDeleting ? (
                        <LoadingLottieSmall />
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
      <InstanceMonitoringModal
        instance={vm}
        isOpen={showMonitoring}
        onClose={() => setShowMonitoring(false)}
        project="default"
      />
    </div>
  );
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
        variant: "destructive",
      });
      return;
    }
    try {
      await updateProjectTags.mutateAsync({
        host: project.host,
        projectName: project.name,
        tags: newTags,
      });
      toast({
        title: "Tags Updated",
        description: `Tags for project '${project.name}' have been updated.`,
        variant: "success",
      });
      refetch();
    } catch (error) {
      toast({
        title: "Failed to update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
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

  const handleSave = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Project updates are disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
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
        variant: "success",
      });

      setEditDialogOpen(false);
      refetch(); // Refresh the project list
    } catch (error) {
      toast({
        title: "Failed to update project",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (e: React.MouseEvent) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Project deletion is disabled in read-only mode",
        variant: "destructive",
      });
      return;
    }
    e.stopPropagation();
    setIsDeleting(true);
    try {
      await deleteProject.mutateAsync({
        host: project.host,
        projectName: project.name,
      });
      toast({
        title: "Project Deleted",
        description: `Project '${project.name}' has been deleted from ${project.host}.`,
        variant: "success",
      });
      setDeleteDialogOpen(false);
    } catch (error) {
      toast({
        title: "Failed to delete project",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <Card className="hover:border-primary/50 transition-colors">
      <CardHeader className="pb-3 px-3 md:px-6">
        <div className="flex flex-col sm:flex-row items-start sm:justify-between gap-2">
          <div 
            className="flex items-center gap-2 flex-1"
          >
            <FolderKanban className="h-4 w-4 text-muted-foreground" />
            <CardTitle className="text-base">{project.name}</CardTitle>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-xs">
              {project.host}
            </Badge>
            {/* Delete/Edit Button - Partner+ only */}
            {!isReadOnly && isPartner && (
              <>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                    <Button variant="ghost" size="icon" className="h-7 w-7">
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
                  <AlertDialogTitle>Delete project '{project.name}'?</AlertDialogTitle>
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
          <p className="text-xs text-muted-foreground line-clamp-2">
            {project.description}
          </p>
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
        <div className="grid grid-cols-3 gap-2 pt-2">
          {/* CPU */}
          <div className="flex items-center gap-1.5 p-2 rounded-md bg-blue-500/10 border border-blue-500/20">
            <Cpu className="h-4 w-4 text-blue-500 flex-shrink-0" />
            <div className="flex flex-col min-w-0">
              {limits.cpuLimit ? (
                <>
                  <span className="text-xs font-semibold text-blue-600 dark:text-blue-400">
                    {resourceUsage.cpuUsed} of {limits.cpuLimit}
                  </span>
                  <span className="text-[10px] text-muted-foreground">cores</span>
                </>
              ) : (
                <>
                  <span className="text-xs font-semibold text-blue-600 dark:text-blue-400">
                    Unlimited
                  </span>
                  <span className="text-[10px] text-muted-foreground">CPU</span>
                </>
              )}
            </div>
          </div>

          {/* RAM */}
          <div className="flex items-center gap-1.5 p-2 rounded-md bg-purple-500/10 border border-purple-500/20">
            <MemoryStick className="h-4 w-4 text-purple-500 flex-shrink-0" />
            <div className="flex flex-col min-w-0">
              {limits.ramLimitBytes ? (
                <>
                  <span className="text-xs font-semibold text-purple-600 dark:text-purple-400">
                    {(resourceUsage.ramUsedBytes / 1024 / 1024 / 1024).toFixed(1)} of {(limits.ramLimitBytes / 1024 / 1024 / 1024).toFixed(1)}
                  </span>
                  <span className="text-[10px] text-muted-foreground">GiB</span>
                </>
              ) : (
                <>
                  <span className="text-xs font-semibold text-purple-600 dark:text-purple-400">
                    Unlimited
                  </span>
                  <span className="text-[10px] text-muted-foreground">RAM</span>
                </>
              )}
            </div>
          </div>

          {/* Storage */}
          <div className="flex items-center gap-1.5 p-2 rounded-md bg-orange-500/10 border border-orange-500/20">
            <HardDrive className="h-4 w-4 text-orange-500 flex-shrink-0" />
            <div className="flex flex-col min-w-0">
              {limits.storageLimitBytes ? (
                <>
                  <span className="text-xs font-semibold text-orange-600 dark:text-orange-400">
                    {(resourceUsage.storageUsedBytes / 1024 / 1024 / 1024).toFixed(1)} of {(limits.storageLimitBytes / 1024 / 1024 / 1024).toFixed(1)}
                  </span>
                  <span className="text-[10px] text-muted-foreground">GiB</span>
                </>
              ) : (
                <>
                  <span className="text-xs font-semibold text-orange-600 dark:text-orange-400">
                    Unlimited
                  </span>
                  <span className="text-[10px] text-muted-foreground">Storage</span>
                </>
              )}
            </div>
          </div>
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
                <div className="h-2 w-2 rounded-full bg-emerald-500" />
                <span className="text-muted-foreground">{stats.running} running</span>
              </div>
              <div className="flex items-center gap-1">
                <div className="h-2 w-2 rounded-full bg-zinc-400" />
                <span className="text-muted-foreground">{stats.stopped} stopped</span>
              </div>
              {stats.other > 0 && (
                <div className="flex items-center gap-1">
                  <div className="h-2 w-2 rounded-full bg-amber-500" />
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
                Note: Renaming projects is not supported by LXD.
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

function ProjectsSection({ projectsData, projectsLoading, vmsData, projectInstanceCounts, refetch, isRefetching, lastUpdated }: ProjectsSectionProps) {
  const [expandedProjects, setExpandedProjects] = React.useState<Set<string>>(new Set());
  const [searchQuery, setSearchQuery] = React.useState("");
  const [selectedTags, setSelectedTags] = React.useState<string[]>([]);
  const [selectedNode, setSelectedNode] = React.useState<string>("all");
  const { isReadOnly } = useReadOnlyMode();
  const queryClient = useQueryClient();

  // Refresh both instances AND projects
  const handleRefresh = React.useCallback(() => {
    refetch(); // refresh instances
    queryClient.invalidateQueries({ queryKey: ["projects", "all"] }); // refresh projects
  }, [refetch, queryClient]);

  // Get unique nodes from projects
  const availableNodes = React.useMemo(() => {
    if (!projectsData || !Array.isArray(projectsData)) return [];
    const nodes = new Set<string>();
    projectsData.forEach((nodeProjects) => {
      if (nodeProjects && nodeProjects.projects) {
        nodeProjects.projects.forEach((project) => {
          if (project.host) nodes.add(project.host);
        });
      }
    });
    return Array.from(nodes).sort();
  }, [projectsData]);

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

  // Filter projects based on search query, tags, and node
  const filterProjects = (project: ProjectInfo) => {
    // Filter by node first
    if (selectedNode !== "all" && project.host !== selectedNode) {
      return false;
    }

    // Filter by tags
    if (selectedTags.length > 0) {
      const projectTags = project.tags || [];
      const instances = getProjectInstances(project.host, project.name);
      const instanceTags = instances.flatMap(vm => vm.tags || []);
      const allProjectTags = Array.from(new Set([...projectTags, ...instanceTags]));
      
      const hasMatchingTag = selectedTags.some(tag => allProjectTags.includes(tag));
      if (!hasMatchingTag) return false;
    }

    // Then filter by search query
    if (!searchQuery.trim()) return true;
    
    const query = searchQuery.toLowerCase();
    const matchesName = project.name.toLowerCase().includes(query);
    const matchesHost = project.host.toLowerCase().includes(query);
    const matchesDescription = project.description?.toLowerCase().includes(query);
    const projectKey = `${project.host}:${project.name}`;
    const instanceCount = projectInstanceCounts.get(projectKey) || 0;
    const matchesInstanceCount = instanceCount.toString().includes(query);
    
    // Also search in instance names and tags within the project
    const instances = getProjectInstances(project.host, project.name);
    const matchesInstanceName = instances.some(vm => 
      vm.name.toLowerCase().includes(query)
    );
    const matchesTags = [...(project.tags || []), ...instances.flatMap(vm => vm.tags || [])]
      .some(tag => tag.toLowerCase().includes(query));
    
    return matchesName || matchesHost || matchesDescription || matchesInstanceCount || matchesInstanceName || matchesTags;
  };

  // Get filtered projects count
  const filteredProjectsCount = projectsData?.reduce((count, nodeProjects) => {
    return count + nodeProjects.projects.filter(filterProjects).length;
  }, 0) || 0;

  const totalProjectsCount = projectsData?.reduce((count, nodeProjects) => {
    return count + nodeProjects.projects.length;
  }, 0) || 0;

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <h2 className="text-lg font-semibold">Projects</h2>
          {lastUpdated && (
            <p className="text-xs text-muted-foreground">
              Last updated: {lastUpdated}
            </p>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={handleRefresh}
          disabled={isRefetching}
          className="gap-2"
        >
          <RefreshCw
            className={cn("h-4 w-4", isRefetching && "animate-spin")}
          />
          Refresh
        </Button>
      </div>

      {/* Search Bar and Node Filter */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 min-w-[200px]">
          <Input
            type="text"
            placeholder="Search projects by name, host, description, or instance..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="pl-4"
          />
          {searchQuery && (
            <Button
              variant="ghost"
              size="icon"
              className="absolute right-1 top-1/2 -translate-y-1/2 h-7 w-7"
              onClick={() => setSearchQuery("")}
            >
              <span className="sr-only">Clear search</span>
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </Button>
          )}
        </div>
        {availableNodes.length > 1 && (
          <Select value={selectedNode} onValueChange={setSelectedNode}>
            <SelectTrigger className="w-[180px]">
              <Server className="h-4 w-4 mr-2" />
              <SelectValue placeholder="All Nodes" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Nodes ({availableNodes.length})</SelectItem>
              {availableNodes.map((node: string) => (
                <SelectItem key={node} value={node}>{node}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {(searchQuery || selectedTags.length > 0 || selectedNode !== "all") && (
          <div className="text-sm text-muted-foreground whitespace-nowrap">
            {filteredProjectsCount} of {totalProjectsCount}
          </div>
        )}
      </div>

      {/* Tag Filter */}
      {allTags.length > 0 && (
        <TagFilter
          availableTags={allTags}
          selectedTags={selectedTags}
          onTagsChange={setSelectedTags}
        />
      )}
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {projectsLoading ? (
          <div className="col-span-full">
            <LoadingLottieMediumEnhanced showProgress={true} />
          </div>
        ) : (
          <>
            {projectsData?.map((nodeProjects) =>
              nodeProjects.projects
                .filter(filterProjects)
                .map((project) => {
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
                })
            )}
            {searchQuery && filteredProjectsCount === 0 && (
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
  const { data: vmsData, isLoading: vmsLoading, refetch, isRefetching, dataUpdatedAt } = useInstances();
  const { data: projectsData, isLoading: projectsLoading } = useAllProjects();
  const { data: clustersData, isLoading: clustersLoading } = useClusters();

  // pre-fetch alarm & node data in parallel with primary queries (eliminates waterfall)
  const { data: warningsData } = useWarnings();
  const { data: systemWarnings } = useSystemWarnings();

  const lastUpdated = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString('en-GB', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
      })
    : null;

  // calculate instance count per project
  const projectInstanceCounts = new Map<string, number>();
  vmsData?.vms.forEach((vm) => {
    const projectKey = `${vm.host}:${vm.project || "default"}`;
    projectInstanceCounts.set(projectKey, (projectInstanceCounts.get(projectKey) || 0) + 1);
  });

  // Progressive loading: render layout immediately with skeleton placeholders
  // instead of blocking the entire page with a full-page loading animation
  const isPrimaryLoading = vmsLoading || projectsLoading || clustersLoading;

  return (
    <div className="space-y-8">
      {/* Stats Overview - shows its own loading state */}
      <StatsOverview data={vmsData} clustersData={clustersData} isLoading={vmsLoading} clustersLoading={clustersLoading} />

      {/* Alarm Statistics - Only shows when there are warnings (data already prefetched above) */}
      <AlarmStatistics prefetchedWarnings={warningsData} prefetchedSystemWarnings={systemWarnings} />

      {/* Nodes Status */}
      <NodeStatusCards />

      {/* Projects Overview */}
      <ProjectsSection 
        projectsData={projectsData} 
        projectsLoading={projectsLoading}
        vmsData={vmsData?.vms || []}
        projectInstanceCounts={projectInstanceCounts}
        refetch={refetch}
        isRefetching={isRefetching}
        lastUpdated={lastUpdated}
      />
    </div>
  );
}
