"use client";

/**
 * The graphical console's guest actions, shared by the console window's toolbar and the full-page
 * console: key combinations the browser would otherwise keep, a screenshot, full screen with the
 * keyboard captured, and fit to window.
 *
 * Combinations go through lib/spice/input.ts as scancodes, and every action is recorded in the
 * audit trail (lib/console-actions-api.ts). Without the console permission the controls are
 * disabled with the reason.
 */

import * as React from "react";
import { Camera, ChevronDown, Expand, Keyboard, Minimize, ScanLine } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
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
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { toast } from "@/hooks/use-toast";
import { useInstance } from "@/hooks/use-lxd";
import { recordConsoleAction } from "@/lib/console-actions-api";
import { COMBOS, isWindowsGuest, type KeyCombo } from "@/lib/spice/keys";
import { sendCombo, type SpiceInputConnection } from "@/lib/spice/input";
import { CustomComboDialog } from "./console-custom-combo-dialog";
import { useConsoleFullscreen } from "./use-console-fullscreen";

const ICON_BUTTON = "h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95";

export interface ConsoleActionsProps {
  instanceName: string;
  host: string;
  project: string;
  getConnection: () => SpiceInputConnection | null;
  getCanvas: () => HTMLCanvasElement | null;
  /** What full screen covers: the console's own frame, so the toolbar stays reachable. */
  getFullscreenTarget: () => HTMLElement | null;
  /** Fit the display to the space it has. Absent where the parent sizes the display itself. */
  onFitToWindow?: () => void;
  /** Whether the caller may use this instance's console (tenant grants); false disables everything. */
  canUseConsole: boolean;
  disabledReason?: string;
  onAfterSend?: () => void;
}

function stamp(date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob | null> {
  return new Promise((resolve) => {
    try {
      canvas.toBlob((blob) => resolve(blob), "image/png");
    } catch {
      resolve(null);
    }
  });
}

export function ConsoleActions({
  instanceName, host, project, getConnection, getCanvas, getFullscreenTarget, onFitToWindow,
  canUseConsole, disabledReason, onAfterSend,
}: ConsoleActionsProps) {
  const [menuOpen, setMenuOpen] = React.useState(false);
  const [customOpen, setCustomOpen] = React.useState(false);
  const [confirming, setConfirming] = React.useState<KeyCombo | null>(null);
  // The guest's OS decides whether Ctrl+Alt+Del needs a confirmation; read when the menu opens.
  const { data: instance } = useInstance(instanceName, host, menuOpen && canUseConsole, project);
  const guestOs = instance?.config?.["image.os"] ?? null;
  const fullscreen = useConsoleFullscreen(getFullscreenTarget);
  const clipboardImages = typeof window !== "undefined" && window.isSecureContext && "ClipboardItem" in window;
  const reason = disabledReason ?? "You do not have console access to this instance";

  const send = React.useCallback(async (combo: KeyCombo) => {
    const sent = await sendCombo(getConnection(), combo);
    if (!sent) {
      toast({ title: "Could not send keys", description: "The console is not connected yet.", variant: "destructive" });
      return;
    }
    recordConsoleAction(instanceName, { host, project, action: "keys.send", combo: combo.id });
    toast({ title: `${combo.label} sent` });
    onAfterSend?.();
  }, [getConnection, instanceName, host, project, onAfterSend]);

  const choose = (combo: KeyCombo) => {
    if (combo.confirmUnlessWindows && !isWindowsGuest(guestOs)) {
      setConfirming(combo);
      return;
    }
    void send(combo);
  };

  const screenshot = async (mode: "save" | "copy") => {
    const canvas = getCanvas();
    const blob = canvas ? await canvasBlob(canvas) : null;
    if (!blob) {
      toast({ title: "Could not capture screenshot", description: "There is no console picture to capture yet.", variant: "destructive" });
      return;
    }
    if (mode === "copy") {
      try {
        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
        toast({ title: "Screenshot copied" });
      } catch {
        toast({ title: "Could not copy screenshot", description: "This browser refused the clipboard. Save it instead.", variant: "destructive" });
        return;
      }
    } else {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${instanceName}-${stamp()}.png`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1_000);
      toast({ title: "Screenshot saved", description: link.download });
    }
    recordConsoleAction(instanceName, { host, project, action: "screenshot.take" });
  };

  const system = COMBOS.filter((c) => c.group === "system");
  const windows = COMBOS.filter((c) => c.group === "windows");
  const terminals = COMBOS.filter((c) => c.group === "terminals");
  const item = (combo: KeyCombo) => (
    <DropdownMenuItem key={combo.id} onSelect={() => choose(combo)} className="flex-col items-start gap-0.5">
      <span>{combo.label}</span>
      {combo.hint && <span className="text-xs text-muted-foreground">{combo.hint}</span>}
    </DropdownMenuItem>
  );

  return (
    <>
      <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild disabled={!canUseConsole}>
              <Button variant="ghost" size="sm" className="h-8 gap-1 px-2 touch-manipulation" aria-label="Send keys to the guest">
                <Keyboard className="h-4 w-4" aria-hidden="true" />
                <span className="hidden text-xs sm:inline">Send Keys</span>
                <ChevronDown className="h-3 w-3" aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>{canUseConsole ? "Ctrl+Alt+Del, lock, switch user and other keys" : reason}</TooltipContent>
        </Tooltip>
        <DropdownMenuContent align="start" className="w-72">
          <DropdownMenuLabel>System</DropdownMenuLabel>
          {system.map(item)}
          <DropdownMenuSeparator />
          <DropdownMenuLabel>Windows</DropdownMenuLabel>
          {windows.map(item)}
          <DropdownMenuSeparator />
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>Linux Terminals (Ctrl+Alt+F1 to F12)</DropdownMenuSubTrigger>
            <DropdownMenuSubContent className="w-64">{terminals.map(item)}</DropdownMenuSubContent>
          </DropdownMenuSub>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => setCustomOpen(true)}>Custom Combination…</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <DropdownMenu>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild disabled={!canUseConsole}>
              <Button variant="ghost" size="icon" className={ICON_BUTTON} aria-label="Take a screenshot">
                <Camera className="h-4 w-4" aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>
            {canUseConsole ? "Screenshot of the guest display (without the pointer)" : reason}
          </TooltipContent>
        </Tooltip>
        <DropdownMenuContent align="start">
          <DropdownMenuItem onSelect={() => void screenshot("save")}>Save as PNG</DropdownMenuItem>
          <DropdownMenuItem disabled={!clipboardImages} onSelect={() => void screenshot("copy")}>
            Copy to Clipboard
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {onFitToWindow && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className={ICON_BUTTON} onClick={onFitToWindow} aria-label="Fit the display to the window">
              <ScanLine className="h-4 w-4" aria-hidden="true" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Fit to Window</TooltipContent>
        </Tooltip>
      )}

      {fullscreen.supported && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant={fullscreen.active ? "secondary" : "ghost"}
              size="icon"
              className={ICON_BUTTON}
              onClick={() => void fullscreen.toggle()}
              aria-label={fullscreen.active ? "Leave full screen" : "Full screen with keyboard capture"}
              aria-pressed={fullscreen.active}
            >
              {fullscreen.active ? <Minimize className="h-4 w-4" aria-hidden="true" /> : <Expand className="h-4 w-4" aria-hidden="true" />}
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            {fullscreen.active
              ? fullscreen.keyboardLocked ? "Hold Esc to leave full screen" : "Press Esc to leave full screen"
              : fullscreen.keyboardLockSupported
                ? "Full screen: Win, Alt+Tab and Esc go to the guest"
                : "Full screen (this browser keeps Win and Alt+Tab)"}
          </TooltipContent>
        </Tooltip>
      )}

      <CustomComboDialog open={customOpen} onOpenChange={setCustomOpen} onSend={choose} />

      <AlertDialog open={confirming !== null} onOpenChange={(open) => !open && setConfirming(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Send {confirming?.label}?</AlertDialogTitle>
            <AlertDialogDescription>
              {guestOs
                ? `This guest reports ${guestOs}. `
                : "This guest does not say which operating system it runs. "}
              On most Linux systems Ctrl+Alt+Del restarts the machine at once. On Windows it opens the security
              screen.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (confirming) void send(confirming);
                setConfirming(null);
              }}
            >
              Send
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
