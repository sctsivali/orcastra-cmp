/**
 * Console Toolbar: power, clipboard, on-screen keyboard and pointer, display scale, the graphical
 * console's guest actions, BIOS keys, connection details and reconnect.
 *
 * Shared by the floating console window and the full-page console, so it holds no store state:
 * the instance's status goes back through `onInstanceStatus`.
 */

"use client";

import * as React from "react";
import { useCallback, useState } from "react";
import {
  ChevronDown,
  ClipboardList,
  Keyboard,
  Loader2,
  MonitorSmartphone,
  MousePointer2,
  Play,
  RefreshCw,
  Rocket,
  RotateCcw,
  Search,
  Square,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { RebuildDialog } from "@/components/rebuild-dialog";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
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
import { FORCE_OFF_HINT, FORCE_ON_HINT, FORCE_OPTION_LABEL } from "@/lib/instance-action-copy";
import type { InstanceStatus } from "@/lib/console-store";
import { toast } from "@/hooks/use-toast";
import { useManualRefresh } from "@/hooks/use-manual-refresh";
import { isActorScopeChangedError, useInstancePowerAction } from "@/hooks/use-instance-power-action";
import type { ConsoleZoom } from "./graphics-console";

export type PowerOperation = "start" | "stop" | "restart";

interface ConsoleToolbarProps {
  instanceName: string;
  instanceType: string;
  host: string;
  project: string;
  instanceStatus: InstanceStatus;
  onInstanceStatus: (status: InstanceStatus) => void;
  /** Called once a power action finished, so open sessions can reconnect to the new instance. */
  onPowerDone?: (operation: PowerOperation) => void;
  onSendKeys?: (key: string) => void;
  showKeyboard?: boolean;
  onToggleKeyboard?: () => void;
  showCursor?: boolean;
  onToggleCursor?: () => void;
  showClipboard?: boolean;
  onToggleClipboard?: () => void;
  onToggleSearch?: () => void;
  /** Absent hides the display controls. "fit" is offered only where `allowFit` is set. */
  zoom?: ConsoleZoom;
  onZoomChange?: (zoom: ConsoleZoom) => void;
  allowFit?: boolean;
  /** The guest follows the console's size (needs its agent). */
  resizeGuest?: boolean;
  onResizeGuestChange?: (enabled: boolean) => void;
  agentConnected?: boolean;
  onReconnect?: () => void;
  canOperate?: boolean;
  operateReason?: string;
  guestActions?: React.ReactNode;
  /** The connection details chip. */
  status?: React.ReactNode;
}

const BIOS_KEYS = [
  { key: "Delete", label: "Del", description: "Most BIOS (Dell, ASUS, MSI)" },
  { key: "F2", label: "F2", description: "UEFI/BIOS (Lenovo, Acer, ASUS)" },
  { key: "F12", label: "F12", description: "Boot Menu (Dell, Lenovo)" },
  { key: "Escape", label: "Esc", description: "Boot Menu (HP)" },
  { key: "F10", label: "F10", description: "BIOS (HP, Compaq)" },
  { key: "F1", label: "F1", description: "BIOS (Lenovo ThinkPad)" },
];

const MIN_ZOOM = 50;
const MAX_ZOOM = 200;
const ZOOM_STEP = 10;
const ICON = "h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95";
const PAST: Record<PowerOperation, string> = { start: "started", stop: "stopped", restart: "restarted" };

export function ConsoleToolbar({
  instanceName,
  instanceType,
  host,
  project,
  instanceStatus,
  onInstanceStatus,
  onPowerDone,
  onSendKeys,
  showKeyboard = false,
  onToggleKeyboard,
  showCursor = false,
  onToggleCursor,
  showClipboard = false,
  onToggleClipboard,
  onToggleSearch,
  zoom,
  onZoomChange,
  allowFit = false,
  resizeGuest = true,
  onResizeGuestChange,
  agentConnected = false,
  onReconnect,
  canOperate = true,
  operateReason,
  guestActions,
  status,
}: ConsoleToolbarProps) {
  const executePowerAction = useInstancePowerAction();
  // important: a reconnect tears down and rebuilds the socket; a double click must not race it.
  const { pending: reconnectPending, run: runReconnect } = useManualRefresh(() => onReconnect?.(), {
    errorTitle: "Reconnect failed",
  });
  const [loading, setLoading] = useState<PowerOperation | null>(null);
  const [spamming, setSpamming] = useState(false);
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [confirm, setConfirm] = useState<{ operation: PowerOperation; force: boolean } | null>(null);

  // `force` power-cycles instead of asking the guest to shut down: a VM with no OS, or one at a
  // boot menu, has nothing listening for the ACPI request.
  const execute = useCallback(async (operation: PowerOperation, force = false) => {
    setLoading(operation);
    onInstanceStatus(operation === "start" ? "Starting" : operation === "stop" ? "Stopping" : "Restarting");
    try {
      await executePowerAction(operation, { name: instanceName, host, project, force, timeout: 30 });
      onInstanceStatus(operation === "stop" ? "Stopped" : "Running");
      toast({ title: `Instance ${PAST[operation]}`, description: instanceName });
      onPowerDone?.(operation);
    } catch (error) {
      if (isActorScopeChangedError(error)) return;
      onInstanceStatus("Error");
      toast({
        title: `Could not ${operation} the instance`,
        description: error instanceof Error ? error.message : "The cluster refused the request.",
        variant: "destructive",
      });
    } finally {
      setLoading(null);
    }
  }, [executePowerAction, host, instanceName, onInstanceStatus, onPowerDone, project]);

  const handleBiosSpam = useCallback(async (key: string) => {
    if (!onSendKeys || spamming) return;
    setSpamming(true);
    for (let i = 0; i < 15; i++) {
      onSendKeys(key);
      await new Promise((r) => setTimeout(r, 100));
    }
    setSpamming(false);
    toast({ title: `Sent ${key} 15 times`, description: "Watch the console for the boot menu." });
  }, [onSendKeys, spamming]);

  const numericZoom = typeof zoom === "number" ? zoom : 100;
  const stepZoom = (delta: number) => {
    if (!onZoomChange) return;
    onZoomChange(Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, numericZoom + delta)));
  };

  const powerReason = operateReason ?? "You do not have permission to change this instance's power state";
  const isRunning = instanceStatus === "Running";
  const isStopped = instanceStatus === "Stopped";
  const isFrozen = instanceStatus === "Frozen";
  const isTransitioning = ["Starting", "Stopping", "Restarting"].includes(instanceStatus);
  const busy = loading !== null;

  const powerButton = (
    operation: PowerOperation | "rebuild",
    label: string,
    icon: React.ReactNode,
    disabled: boolean,
    onClick: () => void,
    tooltip: string,
  ) => (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon" className={ICON} disabled={disabled} onClick={onClick} aria-label={label}>
          {loading === operation ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> : icon}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  );

  const toggleButton = (active: boolean, label: string, icon: React.ReactNode, onClick: () => void) => (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant={active ? "secondary" : "ghost"}
          size="icon"
          className={ICON}
          onClick={onClick}
          aria-label={label}
          aria-pressed={active}
        >
          {icon}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );

  return (
    <TooltipProvider>
      <div
        className="flex flex-wrap items-center gap-1 border-b bg-muted/30 px-2 py-1.5 touch-manipulation"
        onMouseDown={(e) => e.stopPropagation()}
        onTouchStart={(e) => e.stopPropagation()}
        onPointerDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-0.5">
          {powerButton(
            "start",
            "Start instance",
            <Play className="h-4 w-4 text-status-online" aria-hidden="true" />,
            !canOperate || isRunning || isTransitioning || busy || isFrozen,
            () => void execute("start"),
            !canOperate ? powerReason : isFrozen ? "Frozen by billing policy" : "Start Instance",
          )}
          {powerButton(
            "stop",
            "Stop instance",
            <Square className="h-4 w-4 text-status-offline" aria-hidden="true" />,
            !canOperate || isStopped || isTransitioning || busy,
            () => setConfirm({ operation: "stop", force: false }),
            canOperate ? "Stop Instance" : powerReason,
          )}
          {powerButton(
            "restart",
            "Restart instance",
            <RotateCcw className="h-4 w-4 text-primary" aria-hidden="true" />,
            !canOperate || !isRunning || isTransitioning || busy,
            () => setConfirm({ operation: "restart", force: false }),
            canOperate ? "Restart Instance" : powerReason,
          )}
          {powerButton(
            "rebuild",
            "Rebuild instance",
            <RefreshCw className="h-4 w-4 text-status-pending" aria-hidden="true" />,
            !canOperate || isTransitioning || busy,
            () => setRebuildOpen(true),
            canOperate ? "Rebuild Instance (reinstall root disk)" : powerReason,
          )}
        </div>

        <div className="mx-1 h-5 w-px bg-border" aria-hidden="true" />

        {onToggleClipboard && toggleButton(showClipboard, "Clipboard", <ClipboardList className="h-4 w-4" aria-hidden="true" />, onToggleClipboard)}
        {onToggleKeyboard && toggleButton(showKeyboard, "On-Screen Keyboard", <Keyboard className="h-4 w-4" aria-hidden="true" />, onToggleKeyboard)}
        {onToggleCursor && toggleButton(showCursor, "On-Screen Pointer (touch)", <MousePointer2 className="h-4 w-4" aria-hidden="true" />, onToggleCursor)}
        {onToggleSearch && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon" className={ICON} onClick={onToggleSearch} aria-label="Search the output">
                <Search className="h-4 w-4" aria-hidden="true" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Search (Ctrl+Shift+F)</TooltipContent>
          </Tooltip>
        )}

        {onZoomChange && zoom !== undefined && (
          <>
            <div className="mx-1 h-5 w-px bg-border" aria-hidden="true" />
            <div className="flex items-center gap-0.5">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="ghost" size="icon" className={ICON} onClick={() => stepZoom(-ZOOM_STEP)} disabled={zoom !== "fit" && numericZoom <= MIN_ZOOM} aria-label="Zoom out">
                    <ZoomOut className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Zoom Out</TooltipContent>
              </Tooltip>
              {allowFit ? (
                <DropdownMenu>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="sm" className="h-8 min-w-[56px] gap-1 px-2 text-xs tabular-nums" aria-label="Display size">
                          {zoom === "fit" ? "Fit" : `${zoom}%`}
                          <ChevronDown className="h-3 w-3" aria-hidden="true" />
                        </Button>
                      </DropdownMenuTrigger>
                    </TooltipTrigger>
                    <TooltipContent>Display size</TooltipContent>
                  </Tooltip>
                  <DropdownMenuContent align="start" className="w-72">
                    <DropdownMenuLabel>Display</DropdownMenuLabel>
                    <DropdownMenuItem onSelect={() => onZoomChange("fit")}>
                      <span className="flex-1">Fit to Window</span>
                      {zoom === "fit" && <span className="text-xs text-muted-foreground">Current</span>}
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => onZoomChange(100)}>
                      <span className="flex-1">Original Size (100%)</span>
                      {zoom === 100 && <span className="text-xs text-muted-foreground">Current</span>}
                    </DropdownMenuItem>
                    {onResizeGuestChange && (
                      <>
                        <DropdownMenuSeparator />
                        <DropdownMenuCheckboxItem
                          checked={resizeGuest}
                          disabled={!agentConnected}
                          onCheckedChange={(checked) => onResizeGuestChange(Boolean(checked))}
                          className="flex-col items-start gap-0.5"
                        >
                          <span className="flex items-center gap-1.5">
                            <MonitorSmartphone className="h-3.5 w-3.5" aria-hidden="true" />
                            Match Guest Resolution to Window
                          </span>
                          <span className="text-xs text-muted-foreground">
                            {agentConnected ? "Uses the guest agent; text stays sharp at any size" : "Needs the SPICE agent in the guest"}
                          </span>
                        </DropdownMenuCheckboxItem>
                      </>
                    )}
                  </DropdownMenuContent>
                </DropdownMenu>
              ) : (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button variant="ghost" size="sm" className="h-8 min-w-[52px] px-2 text-xs tabular-nums" onClick={() => onZoomChange(100)} aria-label="Reset zoom to 100%">
                      {numericZoom}%
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Reset Zoom (100%)</TooltipContent>
                </Tooltip>
              )}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="ghost" size="icon" className={ICON} onClick={() => stepZoom(ZOOM_STEP)} disabled={zoom !== "fit" && numericZoom >= MAX_ZOOM} aria-label="Zoom in">
                    <ZoomIn className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Zoom In</TooltipContent>
              </Tooltip>
            </div>
          </>
        )}

        {guestActions && (
          <>
            <div className="mx-1 h-5 w-px bg-border" aria-hidden="true" />
            <div className="flex items-center gap-0.5">{guestActions}</div>
          </>
        )}

        {instanceType === "virtual-machine" && onSendKeys && (
          <DropdownMenu>
            <Tooltip>
              <TooltipTrigger asChild>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="sm" className="h-8 gap-1 px-2 touch-manipulation" disabled={spamming} aria-label="BIOS boot keys">
                    {spamming ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Rocket className="h-4 w-4" aria-hidden="true" />}
                    <span className="hidden text-xs sm:inline">BIOS</span>
                    <ChevronDown className="h-3 w-3" aria-hidden="true" />
                  </Button>
                </DropdownMenuTrigger>
              </TooltipTrigger>
              <TooltipContent>Press a boot key repeatedly to reach the BIOS or boot menu</TooltipContent>
            </Tooltip>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuLabel className="text-xs">Press during boot</DropdownMenuLabel>
              <DropdownMenuSeparator />
              {BIOS_KEYS.map((bios) => (
                <DropdownMenuItem key={bios.key} onClick={() => void handleBiosSpam(bios.key)} disabled={spamming}>
                  <div className="flex w-full items-center justify-between">
                    <span className="font-mono font-medium">{bios.label}</span>
                    <span className="text-xs text-muted-foreground">{bios.description}</span>
                  </div>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}

        <div className="ml-auto flex items-center gap-1">
          {status}
          {onReconnect && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  aria-busy={reconnectPending}
                  aria-label="Reconnect to console"
                  variant="ghost"
                  size="sm"
                  className="h-8 gap-1 px-2 text-xs touch-manipulation active:scale-95"
                  disabled={reconnectPending}
                  onClick={() => void runReconnect()}
                >
                  <RotateCcw className={cn("h-3.5 w-3.5", reconnectPending && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />
                  <span className="hidden sm:inline">Reconnect</span>
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reconnect to console</TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>

      <AlertDialog open={confirm !== null} onOpenChange={(open) => !open && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirm?.operation === "stop" ? "Stop Instance?" : "Restart Instance?"}</AlertDialogTitle>
            <AlertDialogDescription>
              {confirm?.operation === "stop"
                ? `Stopping "${instanceName}" ends every running process and closes this console.`
                : `Restarting "${instanceName}" briefly interrupts every running process; the console reconnects when it is back.`}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {/* A graceful stop asks the guest OS to shut down over ACPI; force is the only way to
              power-cycle a VM with nothing listening. Copy lives in lib/instance-action-copy.ts. */}
          <label className="flex cursor-pointer items-start gap-2 rounded-md border p-3">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={confirm?.force ?? false}
              onChange={(e) => setConfirm((prev) => (prev ? { ...prev, force: e.target.checked } : prev))}
            />
            <span className="text-sm">
              <span className="font-medium">{FORCE_OPTION_LABEL}</span>
              <span className="mt-0.5 block text-xs text-muted-foreground">{confirm?.force ? FORCE_ON_HINT : FORCE_OFF_HINT}</span>
            </span>
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className={cn(confirm?.operation === "stop" && "bg-destructive text-destructive-foreground hover:bg-destructive/90")}
              onClick={() => {
                if (confirm) void execute(confirm.operation, confirm.force);
                setConfirm(null);
              }}
            >
              {confirm?.force ? "Force " : ""}
              {confirm?.operation === "stop" ? "Stop" : "Restart"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {rebuildOpen && (
        <RebuildDialog
          open={rebuildOpen}
          onOpenChange={setRebuildOpen}
          instance={{ name: instanceName, host, project }}
          // The root disk was reinstalled: empty mode leaves it stopped, the image modes running.
          onDone={() => onInstanceStatus("Stopped")}
        />
      )}
    </TooltipProvider>
  );
}
