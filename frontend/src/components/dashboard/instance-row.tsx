"use client";

import { formatInstanceType } from "@/lib/utils";
import * as React from "react";
import {
  Server,
  Container,
  Play,
  Square,
  RotateCw,
  Trash2,
  Terminal,
  Monitor,
  Loader2,
  Lock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InstanceDetailDialog } from "@/components/instance-detail/instance-detail-dialog";
import { TagManager } from "@/components/tag-manager";
import { Badge } from "@/components/ui/badge";
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
  useStartVM,
  useStopVM,
  useRestartVM,
  useDeleteInstance,
  isDeleteOutcomeUnknown,
  useUpdateInstanceTags } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { toast } from "@/hooks/use-toast";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";
import { VMInfo } from "@/types";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { instanceStatusDisplay } from "@/lib/instance-status";
import { useActorQueryScope } from "@/hooks/use-query-scope";

interface InstanceRowProps {
  vm: VMInfo;
  onTagsUpdate?: () => void;
  isReadOnly?: boolean;
}

export function InstanceRow({ vm, onTagsUpdate, isReadOnly = false }: InstanceRowProps) {
  const startVM = useStartVM();
  const stopVM = useStopVM();
  const restartVM = useRestartVM();
  const deleteInstance = useDeleteInstance();
  const updateInstanceTags = useUpdateInstanceTags();
  const [isDeleting, setIsDeleting] = React.useState(false);
  const [showMonitoring, setShowMonitoring] = React.useState(false);
  const { isPartner } = usePermissions();
  const { hasPermission, termReadOnlyReason } = useTenantAccess();
  const { scopeKey } = useActorQueryScope();
  const activeActorScopeRef = React.useRef(scopeKey);
  activeActorScopeRef.current = scopeKey;

  const canOperate = hasPermission(vm.host, vm.project || "default", vm.name, "operate");
  const canConsole = hasPermission(vm.host, vm.project || "default", vm.name, "console");
  // A paid term that ended says why the controls are off, rather than a bare "no permission".
  const termReason = termReadOnlyReason(vm.host, vm.project || "default", vm.name);
  const noOperate = termReason ?? "No operate permission";
  const noConsole = termReason ?? "No console permission";

  // An instance name is unique only within one project on one host, and the projects grid can
  // hold two expanded cards at once, so button names carry the project and host too.
  // check-icon-button-names.mjs catches a missing name, never an ambiguous one.
  const rowLabel = `${vm.name} in ${vm.project || "default"} on ${vm.host}`;
  // One row renders both kinds, so a toast names the one that was acted on.
  const instanceNoun = vm.type === "virtual-machine" ? "virtual machine" : "container";
  const instanceSubject = vm.type === "virtual-machine" ? "Virtual machine" : "Container";

  const handleTagsChange = async (newTags: string[]) => {
    const requestActorScope = scopeKey;
    if (isReadOnly) {
      toast({
        title: "Read-only mode active",
        description: "Tag updates are disabled in read-only mode.",
        variant: "destructive" });
      return;
    }
    try {
      await updateInstanceTags.mutateAsync({
        host: vm.host,
        instanceName: vm.name,
        tags: newTags,
        project: vm.project || "default" });
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: "Tags updated",
        description: `Tags for ${vm.name} have been updated.`,
        variant: "success" });
      onTagsUpdate?.();
    } catch (error) {
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: "Could not update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  const handleStart = async () => {
    const requestActorScope = scopeKey;
    if (isReadOnly) {
      toast({
        title: "Read-only mode active",
        description: "Instance actions are disabled in read-only mode.",
        variant: "destructive" });
      return;
    }
    try {
      await startVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" } });
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: `${instanceSubject} started`,
        description: `${vm.name} is now starting.`,
        variant: "success" });
    } catch (error) {
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: `Could not start ${instanceNoun}`,
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  const handleStop = async () => {
    const requestActorScope = scopeKey;
    if (isReadOnly) {
      toast({
        title: "Read-only mode active",
        description: "Instance actions are disabled in read-only mode.",
        variant: "destructive" });
      return;
    }
    try {
      await stopVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" } });
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: `${instanceSubject} stopped`,
        description: `${vm.name} is now stopping.`,
        variant: "success" });
    } catch (error) {
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: `Could not stop ${instanceNoun}`,
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  const handleRestart = async () => {
    const requestActorScope = scopeKey;
    if (isReadOnly) {
      toast({
        title: "Read-only mode active",
        description: "Instance actions are disabled in read-only mode.",
        variant: "destructive" });
      return;
    }
    try {
      await restartVM.mutateAsync({
        name: vm.name,
        request: { host: vm.host, project: vm.project || "default" } });
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: `${instanceSubject} restarted`,
        description: `${vm.name} is now restarting.`,
        variant: "success" });
    } catch (error) {
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: `Could not restart ${instanceNoun}`,
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  const handleDelete = async () => {
    const requestActorScope = scopeKey;
    if (isReadOnly) {
      toast({
        title: "Read-only mode active",
        description: "Instance deletion is disabled in read-only mode.",
        variant: "destructive" });
      return;
    }
    setIsDeleting(true);
    try {
      await deleteInstance.mutateAsync({
        name: vm.name,
        host: vm.host,
        force: true,
        project: vm.project || "default" });
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast({
        title: `${instanceSubject} deleted`,
        description: `${vm.name} has been deleted.`,
        variant: "success" });
    } catch (error) {
      if (activeActorScopeRef.current !== requestActorScope) return;
      toast(isDeleteOutcomeUnknown(error)
        ? {
            title: `Still deleting ${instanceNoun}`,
            description: `${vm.name} is taking longer than usual on the cluster. It leaves the list when the delete finishes.` }
        : {
            title: `Could not delete ${instanceNoun}`,
            description: error instanceof Error ? error.message : "Unknown error",
            variant: "destructive" });
    } finally {
      if (activeActorScopeRef.current === requestActorScope) {
        setIsDeleting(false);
      }
    }
  };

  const statusDisplay = instanceStatusDisplay(vm.status);
  const isRunning = String(vm.status).toLowerCase() === "running";
  const isPending = startVM.isPending || stopVM.isPending || restartVM.isPending;

  return (
    <div
      data-slot="project-instance-row"
      className="p-2 md:p-3 rounded-lg bg-muted/30 hover:bg-muted/50 transition-colors border"
    >
      {/* The wrap follows this row's own width, not the viewport: inside a project card the row
          gets far less room than an `sm` breakpoint assumes.

          flex-wrap rather than an auto-fit grid: auto-fit builds equal-width tracks, and here one
          child flexes while the action bar is fixed, so a track narrower than the bar would let
          it overlap the column beside it. Flex items never overlap.

          The 180px basis is the wrap threshold, not a width. The action bar is 188px at six h-7
          buttons and 284px at or below a 768px viewport, where globals.css raises every button to
          a 44px touch target, so the row stacks whenever its card is narrower than that. */}
      <div className="flex flex-wrap items-start gap-2 md:gap-3">
        <div
          className="min-w-0 flex-1 basis-[180px] space-y-2 cursor-pointer"
          onClick={() => setShowMonitoring(true)}
        >
          <div className="flex items-center gap-2 flex-wrap">
            {vm.type === "virtual-machine" ? (
              <Server className="h-4 w-4 text-muted-foreground flex-shrink-0" />
            ) : (
              <Container className="h-4 w-4 text-muted-foreground flex-shrink-0" />
            )}
            <span className="text-sm font-medium truncate">{vm.name}</span>
            <Badge variant={statusDisplay.tone} className="text-xs">
              {statusDisplay.label}
            </Badge>
            {termReason && (
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground" data-testid="term-read-only">
                <Lock className="h-3 w-3" aria-hidden="true" />
                Read only
              </span>
            )}
          </div>
          
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            <div className="flex items-center gap-1">
              <span className="font-medium">Type:</span>
              <span>{formatInstanceType(vm.type)}</span>
            </div>
            
            {vm.architecture && (
              <div className="flex items-center gap-1">
                <span className="font-medium">Arch:</span>
                <span>{vm.architecture}</span>
              </div>
            )}
            
            {vm.ip_addresses && vm.ip_addresses.length > 0 && (
              <div className="flex items-center gap-1">
                <span className="font-medium">IP:</span>
                <span>{vm.ip_addresses[0]}</span>
              </div>
            )}
          </div>

          <div className="mt-2" onClick={(e) => e.stopPropagation()}>
            <TagManager
              tags={vm.tags || []}
              onTagsChange={handleTagsChange}
              readonly={isReadOnly}
              maxTags={10}
              placeholder="Add instance tag…"
            />
          </div>
        </div>

        <TooltipProvider>
          <div className="flex items-center gap-1 shrink-0 ml-auto">
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
              <TooltipContent>{!canOperate ? noOperate : isReadOnly ? "Disabled in read-only mode" : "Start"}</TooltipContent>
            </Tooltip>

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
              <TooltipContent>{!canOperate ? noOperate : isReadOnly ? "Disabled in read-only mode" : "Stop"}</TooltipContent>
            </Tooltip>

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
              <TooltipContent>{!canOperate ? noOperate : isReadOnly ? "Disabled in read-only mode" : "Restart"}</TooltipContent>
            </Tooltip>

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
                  title={!canConsole ? noConsole : !isRunning ? "Instance must be running" : "Open Terminal"}
                  aria-label={`Open terminal for instance ${rowLabel}`}
                >
                  <Terminal className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{!canConsole ? noConsole : !isRunning ? "Instance must be running" : "Open Terminal"}</TooltipContent>
            </Tooltip>

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
                  title={!canConsole ? noConsole : !isRunning ? "Instance must be running" : "Open Console"}
                  aria-label={`Open console for instance ${rowLabel}`}
                >
                  <Monitor className="h-3.5 w-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{!canConsole ? noConsole : !isRunning ? "Instance must be running" : "Open Console"}</TooltipContent>
            </Tooltip>

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
      
      <InstanceDetailDialog
        instance={vm}
        isOpen={showMonitoring}
        onClose={() => setShowMonitoring(false)}
        project={vm.project || "default"}
      />
    </div>
  );
}
