/**
 * Console Toolbar - Controls bar for console window.
 * Provides start/stop/restart, clipboard manager, on-screen keyboard,
 * zoom controls, BIOS boot keys, and fullscreen toggle.
 */

"use client";

import * as React from "react";
import { useState, useCallback } from "react";
import {
  Play,
  Square,
  RotateCcw,
  Loader2,
  Keyboard,
  ClipboardList,
  ZoomIn,
  ZoomOut,
  Rocket,
  ChevronDown,
  MousePointer2,
  RefreshCw,
} from "lucide-react";
import { RebuildDialog } from "@/components/rebuild-dialog";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
  DropdownMenuLabel,
} from "@/components/ui/dropdown-menu";
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
import { cn } from "@/lib/utils";
import {
  FORCE_OFF_HINT,
  FORCE_ON_HINT,
  FORCE_OPTION_LABEL,
} from "@/lib/instance-action-copy";
import { InstanceStatus, useConsoleStore } from "@/lib/console-store";
import { toast } from "@/hooks/use-toast";
import { useManualRefresh } from "@/hooks/use-manual-refresh";
import {
  isActorScopeChangedError,
  useInstancePowerAction,
} from "@/hooks/use-instance-power-action";

interface ConsoleToolbarProps {
  consoleId: string;
  instanceName: string;
  instanceType: string;
  host: string;
  project: string;
  instanceStatus: InstanceStatus;
  isFullPage: boolean;
  onFullPageToggle: () => void;
  onSendKeys?: (keys: string) => void;
  onPaste?: (text: string) => void;
  // New props
  showKeyboard?: boolean;
  onToggleKeyboard?: () => void;
  showCursor?: boolean;
  onToggleCursor?: () => void;
  showClipboard?: boolean;
  onToggleClipboard?: () => void;
  zoom?: number;
  onZoomChange?: (zoom: number) => void;
  onReconnect?: () => void; // Manual reconnect button
  /** Whether the caller may start, stop, restart or rebuild (tenant grants); false disables them. */
  canOperate?: boolean;
  /** Why power controls are disabled, for example a paid term that ended. */
  operateReason?: string;
  /** The graphical console's guest actions (ConsoleActions), shown on the Graphics tab. */
  guestActions?: React.ReactNode;
}

type OperationType = 'start' | 'stop' | 'restart';

interface ConfirmDialogState {
  open: boolean;
  operation: OperationType | null;
  title: string;
  description: string;
  // Power-cycle instead of requesting a graceful guest shutdown.
  force: boolean;
}

// BIOS boot key options
const BIOS_KEYS = [
  { key: 'Delete', label: 'Del', description: 'Most BIOS (Dell, ASUS, MSI)' },
  { key: 'F2', label: 'F2', description: 'UEFI/BIOS (Lenovo, Acer, ASUS)' },
  { key: 'F12', label: 'F12', description: 'Boot Menu (Dell, Lenovo)' },
  { key: 'Escape', label: 'Esc', description: 'Boot Menu (HP)' },
  { key: 'F10', label: 'F10', description: 'BIOS (HP, Compaq)' },
  { key: 'F1', label: 'F1', description: 'BIOS (Lenovo ThinkPad)' },
];

// Zoom range
const MIN_ZOOM = 50;
const MAX_ZOOM = 200;
const ZOOM_STEP = 10;

export function ConsoleToolbar({
  consoleId,
  instanceName,
  instanceType,
  host,
  project,
  instanceStatus,
  isFullPage,
  onFullPageToggle,
  onSendKeys,
  onPaste,
  showKeyboard = false,
  onToggleKeyboard,
  showCursor = false,
  onToggleCursor,
  showClipboard = false,
  onToggleClipboard,
  zoom = 100,
  onZoomChange,
  onReconnect,
  canOperate = true,
  operateReason,
  guestActions,
}: ConsoleToolbarProps) {
  const updateInstanceStatus = useConsoleStore(s => s.updateInstanceStatus);
  const executePowerAction = useInstancePowerAction();
  // important: a reconnect tears down and rebuilds the WebSocket. Without a guard
  // a double-click (or the old onClick + onTouchEnd pair on touch devices) did
  // that twice in a row and left the session racing itself.
  const { pending: reconnectPending, run: runReconnect } = useManualRefresh(
    () => onReconnect?.(),
    { errorTitle: "Reconnect failed" },
  );
  const [loading, setLoading] = useState<OperationType | null>(null);
  const [spamming, setSpamming] = useState(false);
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>({
    open: false,
    operation: null,
    title: "",
    description: "",
    force: false,
  });

  // Execute instance operation. `force` power-cycles instead of asking the guest
  // to shut down: required for a VM with no OS installed (or one sitting at a
  // boot menu/UEFI shell), which has nothing listening for the ACPI request.
  const executeOperation = useCallback(async (operation: OperationType, force = false) => {
    setLoading(operation);

    const transitionStatus: Record<OperationType, InstanceStatus> = {
      start: 'Starting',
      stop: 'Stopping',
      restart: 'Restarting',
    };
    updateInstanceStatus(consoleId, transitionStatus[operation]);

    try {
      await executePowerAction(operation, {
        name: instanceName,
        host,
        project,
        force,
        timeout: 30,
      });
      
      const finalStatus: Record<OperationType, InstanceStatus> = {
        start: 'Running',
        stop: 'Stopped',
        restart: 'Running',
      };
      updateInstanceStatus(consoleId, finalStatus[operation]);
      
      toast({
        title: "Success",
        description: `Instance ${operation} completed`,
      });

    } catch (error) {
      if (isActorScopeChangedError(error)) return;
      console.error(`Error ${operation}ing instance:`, error);
      updateInstanceStatus(consoleId, 'Error');
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : `Failed to ${operation}`,
        variant: "destructive",
      });
    } finally {
      setLoading(null);
    }
  }, [consoleId, executePowerAction, host, instanceName, project, updateInstanceStatus]);

  // Handle operation with confirmation for stop/restart
  const handleOperation = useCallback((operation: OperationType) => {
    if (operation === 'start') {
      executeOperation(operation);
      return;
    }

    setConfirmDialog({
      open: true,
      operation,
      title: `${operation.charAt(0).toUpperCase() + operation.slice(1)} Instance?`,
      description: operation === 'stop'
        ? `Are you sure you want to stop "${instanceName}"? This will terminate all running processes.`
        : `Are you sure you want to restart "${instanceName}"? This will briefly interrupt all running processes.`,
      force: false,
    });
  }, [instanceName, executeOperation]);

  // Confirm operation
  const confirmOperation = useCallback(() => {
    if (confirmDialog.operation) {
      executeOperation(confirmDialog.operation, confirmDialog.force);
    }
    setConfirmDialog({ open: false, operation: null, title: "", description: "", force: false });
  }, [confirmDialog.operation, confirmDialog.force, executeOperation]);

  // BIOS spam key - sends key 15 times with 100ms interval
  const handleBiosSpam = useCallback(async (key: string) => {
    if (!onSendKeys || spamming) return;
    
    setSpamming(true);
    toast({
      title: `Spamming ${key}`,
      description: "Sending key 15 times...",
    });

    for (let i = 0; i < 15; i++) {
      onSendKeys(key);
      await new Promise(r => setTimeout(r, 100));
    }

    setSpamming(false);
    toast({
      title: "Done",
      description: `Sent ${key} 15 times`,
    });
  }, [onSendKeys, spamming]);

  // Zoom handlers
  const handleZoomIn = useCallback(() => {
    if (onZoomChange && zoom < MAX_ZOOM) {
      onZoomChange(Math.min(zoom + ZOOM_STEP, MAX_ZOOM));
    }
  }, [zoom, onZoomChange]);

  const handleZoomOut = useCallback(() => {
    if (onZoomChange && zoom > MIN_ZOOM) {
      onZoomChange(Math.max(zoom - ZOOM_STEP, MIN_ZOOM));
    }
  }, [zoom, onZoomChange]);

  const handleZoomReset = useCallback(() => {
    if (onZoomChange) {
      onZoomChange(100);
    }
  }, [onZoomChange]);

  const powerReason = operateReason ?? "You do not have permission to change this instance's power state";
  const isRunning = instanceStatus === 'Running';
  const isStopped = instanceStatus === 'Stopped';
  const isFrozen = instanceStatus === 'Frozen';
  const isTransitioning = ['Starting', 'Stopping', 'Restarting'].includes(instanceStatus);

  return (
    <TooltipProvider>
      <div 
        className="flex items-center gap-1 px-2 py-1.5 border-b bg-muted/30 touch-manipulation flex-wrap"
        onMouseDown={(e) => e.stopPropagation()}
        onTouchStart={(e) => e.stopPropagation()}
        onPointerDown={(e) => e.stopPropagation()}
      >
        {/* Power controls */}
        <div className="flex items-center gap-0.5">
          {/* Start */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                disabled={!canOperate || isRunning || isTransitioning || loading !== null || isFrozen}
                onClick={() => handleOperation('start')}
                onTouchEnd={(e) => { e.preventDefault(); if (canOperate && !isRunning && !isTransitioning && loading === null) handleOperation('start'); }}
                aria-label="Start instance"
              >
                {loading === 'start' ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Play className="h-4 w-4 text-green-500" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{!canOperate ? powerReason : isFrozen ? "Frozen by billing policy" : "Start Instance"}</TooltipContent>
          </Tooltip>

          {/* Stop */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                disabled={!canOperate || isStopped || isTransitioning || loading !== null}
                onClick={() => handleOperation('stop')}
                onTouchEnd={(e) => { e.preventDefault(); if (canOperate && !isStopped && !isTransitioning && loading === null) handleOperation('stop'); }}
                aria-label="Stop instance"
              >
                {loading === 'stop' ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Square className="h-4 w-4 text-red-500" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{canOperate ? "Stop Instance" : powerReason}</TooltipContent>
          </Tooltip>

          {/* Restart */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                disabled={!canOperate || !isRunning || isTransitioning || loading !== null}
                onClick={() => handleOperation('restart')}
                onTouchEnd={(e) => { e.preventDefault(); if (canOperate && isRunning && !isTransitioning && loading === null) handleOperation('restart'); }}
                aria-label="Restart instance"
              >
                {loading === 'restart' ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RotateCcw className="h-4 w-4 text-blue-500" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{canOperate ? "Restart Instance" : powerReason}</TooltipContent>
          </Tooltip>

          {/* Rebuild (reinstall root disk) */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                disabled={!canOperate || isTransitioning || loading !== null}
                onClick={() => setRebuildOpen(true)}
                onTouchEnd={(e) => { e.preventDefault(); if (canOperate && !isTransitioning && loading === null) setRebuildOpen(true); }}
                aria-label="Rebuild instance"
              >
                <RefreshCw className="h-4 w-4 text-amber-500" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{canOperate ? "Rebuild Instance (reinstall root disk)" : powerReason}</TooltipContent>
          </Tooltip>
        </div>

        <div className="w-px h-5 bg-border mx-1" />

        {/* Clipboard Manager */}
        {onToggleClipboard && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant={showClipboard ? "secondary" : "ghost"}
                size="icon"
                className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                onClick={onToggleClipboard}
                onTouchEnd={(e) => { e.preventDefault(); onToggleClipboard(); }}
              >
                <ClipboardList className="h-4 w-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Clipboard Manager</TooltipContent>
          </Tooltip>
        )}

        {/* On-Screen Keyboard Toggle */}
        {onToggleKeyboard && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant={showKeyboard ? "secondary" : "ghost"}
                size="icon"
                className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                onClick={onToggleKeyboard}
                onTouchEnd={(e) => { e.preventDefault(); onToggleKeyboard(); }}
              >
                <Keyboard className="h-4 w-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>On-Screen Keyboard</TooltipContent>
          </Tooltip>
        )}

        {/* On-Screen Cursor Toggle */}
        {onToggleCursor && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant={showCursor ? "secondary" : "ghost"}
                size="icon"
                className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                onClick={onToggleCursor}
                onTouchEnd={(e) => { e.preventDefault(); onToggleCursor(); }}
              >
                <MousePointer2 className="h-4 w-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>On-Screen Cursor</TooltipContent>
          </Tooltip>
        )}

        <div className="w-px h-5 bg-border mx-1" />

        {/* Zoom Controls */}
        {onZoomChange && (
          <div className="flex items-center gap-0.5">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                  onClick={handleZoomOut}
                  onTouchEnd={(e) => { e.preventDefault(); handleZoomOut(); }}
                  disabled={zoom <= MIN_ZOOM}
                >
                  <ZoomOut className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Zoom Out</TooltipContent>
            </Tooltip>

            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2 min-h-[32px] touch-manipulation text-xs font-mono"
                  onClick={handleZoomReset}
                  onTouchEnd={(e) => { e.preventDefault(); handleZoomReset(); }}
                >
                  {zoom}%
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reset Zoom (100%)</TooltipContent>
            </Tooltip>

            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
                  onClick={handleZoomIn}
                  onTouchEnd={(e) => { e.preventDefault(); handleZoomIn(); }}
                  disabled={zoom >= MAX_ZOOM}
                >
                  <ZoomIn className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Zoom In</TooltipContent>
            </Tooltip>
          </div>
        )}

        {/* Send keys, screenshot, full screen, fit (Graphics tab) */}
        {guestActions && (
          <>
            <div className="w-px h-5 bg-border mx-1" />
            <div className="flex items-center gap-0.5">{guestActions}</div>
          </>
        )}

        {/* BIOS Boot Keys (for VMs) - positioned before Reconnect */}
        {instanceType === 'virtual-machine' && (
          <DropdownMenu>
            <Tooltip>
              <TooltipTrigger asChild>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-8 px-2 min-h-[32px] touch-manipulation active:scale-95 gap-1"
                    disabled={spamming}
                  >
                    {spamming ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Rocket className="h-4 w-4" />
                    )}
                    <span className="text-xs hidden sm:inline">BIOS</span>
                    <ChevronDown className="h-3 w-3" />
                  </Button>
                </DropdownMenuTrigger>
              </TooltipTrigger>
              <TooltipContent>BIOS Boot Keys (Spam)</TooltipContent>
            </Tooltip>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuLabel className="text-xs">
                Spam key to enter BIOS/Boot Menu
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              {BIOS_KEYS.map((bios) => (
                <DropdownMenuItem 
                  key={bios.key}
                  onClick={() => handleBiosSpam(bios.key)}
                  disabled={spamming}
                >
                  <div className="flex items-center justify-between w-full">
                    <span className="font-mono font-medium">{bios.label}</span>
                    <span className="text-xs text-muted-foreground">{bios.description}</span>
                  </div>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}

        {/* Manual Reconnect Button */}
        {onReconnect && (
          <>
            <div className="w-px h-5 bg-border mx-1" />
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  aria-busy={reconnectPending}
                  aria-label="Reconnect to console"
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2 min-h-[32px] touch-manipulation active:scale-95 text-xs gap-1"
                  disabled={reconnectPending}
                  onClick={() => void runReconnect()}
                >
                  <RotateCcw
                    className={cn(
                      "h-3.5 w-3.5",
                      reconnectPending && "animate-spin motion-reduce:animate-none",
                    )}
                  />
                  Reconnect
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reconnect to console</TooltipContent>
            </Tooltip>
          </>
        )}
      </div>

      {/* Confirm dialog */}
      <AlertDialog open={confirmDialog.open} onOpenChange={(open) => !open && setConfirmDialog({ ...confirmDialog, open: false })}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirmDialog.title}</AlertDialogTitle>
            <AlertDialogDescription>{confirmDialog.description}</AlertDialogDescription>
          </AlertDialogHeader>
          {/* A graceful stop asks the guest OS to shut down over ACPI. A VM with
              no OS installed, or one sitting at a boot menu/UEFI shell, has
              nothing listening and will just time out. Force is the only way to
              power-cycle those. Copy lives in lib/instance-action-copy.ts, which
              two other dialogs share. */}
          <label className="flex items-start gap-2 rounded-md border p-3 cursor-pointer">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={confirmDialog.force}
              onChange={(e) =>
                setConfirmDialog((prev) => ({ ...prev, force: e.target.checked }))
              }
            />
            <span className="text-sm">
              <span className="font-medium">{FORCE_OPTION_LABEL}</span>
              <span className="block text-xs text-muted-foreground mt-0.5">
                {confirmDialog.force ? FORCE_ON_HINT : FORCE_OFF_HINT}
              </span>
            </span>
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmOperation}>
              {confirmDialog.force ? 'Force ' : ''}
              {confirmDialog.operation === 'stop' ? 'Stop' : 'Restart'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Rebuild dialog (reinstall root disk from a fresh source) */}
      {rebuildOpen && (
        <RebuildDialog
          open={rebuildOpen}
          onOpenChange={setRebuildOpen}
          instance={{ name: instanceName, host, project }}
          onDone={() => {
            // The root disk was reinstalled; empty mode leaves it stopped,
            // image/cloud-init modes leave it running. Reflect stopped as a safe
            // default the console's own status stream will quickly correct.
            updateInstanceStatus(consoleId, 'Stopped');
          }}
        />
      )}
    </TooltipProvider>
  );
}
