/**
 * Console Workspace: tabs, toolbar, panels, the consoles themselves and the resource bar, shared
 * by the floating console window and the full-page console.
 *
 * Every console tab the operator opened stays connected while another tab is in front (they used
 * to disconnect and reconnect on every switch), and the Graphics tab asks before taking over a
 * display somebody else is watching.
 */

"use client";

import * as React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "@/hooks/use-toast";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { cn } from "@/lib/utils";
import { consoleAPI, type ActiveConsoleSession } from "@/lib/console-api";
import type { ConsoleCapabilities, ConsoleConnectionStatus, ConsoleTab, InstanceStatus } from "@/lib/console-store";
import type { SpiceStats, SpiceStatus } from "@/lib/spice/session";
import type { MouseMode } from "@/lib/spice/pointer";
import { DEFAULT_FONT_SIZE } from "@/lib/xterm-setup";
import { ConsoleTabs } from "./console-tabs";
import { ConsoleToolbar, type PowerOperation } from "./console-toolbar";
import { ConsoleActions } from "./console-actions";
import { ConnectionDetails } from "./connection-details";
import { GraphicsConsole, type ConsoleZoom, type GraphicsConsoleHandle } from "./graphics-console";
import { TextConsole, type TextConsoleHandle } from "./text-console";
import { SerialConsole, type SerialConsoleHandle } from "./serial-console";
import { ISOManagerPanel } from "./iso-manager-panel";
import { OnScreenKeyboard } from "./on-screen-keyboard";
import { OnScreenCursor } from "./on-screen-cursor";
import { ClipboardManager, rememberClipboard } from "./clipboard-manager";
import { ConsoleResourceBar } from "./resource-bar";
import { ConflictNotice, SearchBar, terminalSequence, useFullscreenChrome } from "./console-workspace-parts";

export interface ConsoleWorkspaceProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  instanceType: string;
  activeTab: ConsoleTab;
  onTabChange: (tab: ConsoleTab) => void;
  instanceStatus: InstanceStatus;
  onInstanceStatus: (status: InstanceStatus) => void;
  /** The front tab's connection, for the title chip and the tray. */
  onConnectionStatus?: (status: ConsoleConnectionStatus, detail?: string) => void;
  onCapabilities?: (capabilities: ConsoleCapabilities) => void;
  isFullPage: boolean;
  /** False while the window is minimized: sessions stay open, nothing is resized or focused. */
  visible?: boolean;
  /** What full screen covers (the window or the page). */
  getFullscreenTarget: () => HTMLElement | null;
  /** Skip the "someone is watching" check (a console handed over from this browser). */
  skipConflictCheck?: boolean;
  /** Text console status goes to the console store (floating windows). */
  reportToStore?: boolean;
  sessionId?: string | null;
  onSessionId?: (sessionId: string) => void;
}

type CapsState = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; caps: ConsoleCapabilities };

const RESIZE_PREF_KEY = "orcastra-console-resize-guest";

function readResizePreference(): boolean {
  try {
    return localStorage.getItem(RESIZE_PREF_KEY) !== "0";
  } catch {
    return true;
  }
}

function graphicsToConnection(status: SpiceStatus): ConsoleConnectionStatus {
  if (status === "connected") return "connected";
  if (status === "ended") return "disconnected";
  if (status === "error") return "error";
  return "connecting";
}

export function ConsoleWorkspace({
  consoleId,
  instanceName,
  host,
  project,
  instanceType,
  activeTab,
  onTabChange,
  instanceStatus,
  onInstanceStatus,
  onConnectionStatus,
  onCapabilities,
  isFullPage,
  visible = true,
  getFullscreenTarget,
  skipConflictCheck = false,
  reportToStore = false,
  sessionId = null,
  onSessionId,
}: ConsoleWorkspaceProps) {
  const graphicsRef = useRef<GraphicsConsoleHandle | null>(null);
  const textRef = useRef<TextConsoleHandle | null>(null);
  const serialRef = useRef<SerialConsoleHandle | null>(null);
  // Read by the power follow-up timer, which outlives the render it was scheduled in.
  const liveRef = useRef<{ graphics: boolean; serial: boolean }>({ graphics: false, serial: false });
  const rootRef = useRef<HTMLDivElement>(null);

  const [caps, setCaps] = useState<CapsState>({ status: "loading" });
  const [mounted, setMounted] = useState<Set<ConsoleTab>>(() => new Set([activeTab]));
  const [conflict, setConflict] = useState<ActiveConsoleSession[] | null>(null);
  const [graphicsCleared, setGraphicsCleared] = useState(skipConflictCheck);
  const [zoom, setZoom] = useState<ConsoleZoom>("fit");
  const [textZoom, setTextZoom] = useState(100);
  const [resizeGuest, setResizeGuest] = useState(true);
  const [panels, setPanels] = useState({ keyboard: false, cursor: false, clipboard: false, search: false });
  const [graphics, setGraphics] = useState<{ status: SpiceStatus; stats: SpiceStats | null; agent: boolean; mouseMode: MouseMode; view: { surface: { width: number; height: number } | null; scale: number } | null }>({
    status: "loading", stats: null, agent: false, mouseMode: "client", view: null,
  });
  const [textStatus, setTextStatus] = useState<ConsoleConnectionStatus>("connecting");
  const [serialStatus, setSerialStatus] = useState<ConsoleConnectionStatus>("connecting");
  const chrome = useFullscreenChrome(getFullscreenTarget);

  const { hasPermission, termReadOnlyReason } = useTenantAccess();
  const canOperate = hasPermission(host, project, instanceName, "operate");
  const canUseConsole = hasPermission(host, project, instanceName, "console");
  const operateReason = termReadOnlyReason(host, project, instanceName);

  useEffect(() => setResizeGuest(readResizePreference()), []);

  // Capabilities: whether there is a display, and the instance's state.
  const loadCapabilities = useCallback(async () => {
    setCaps({ status: "loading" });
    try {
      const data = await consoleAPI.getCapabilities(instanceName, host, project);
      const next: ConsoleCapabilities = {
        textConsole: data.text_console ?? true,
        graphicsConsole: data.graphics_console ?? false,
        graphicsType: data.graphics_type ?? "none",
        graphicsAddress: data.graphics_address ?? undefined,
        graphicsPort: data.graphics_port ?? undefined,
        audioEnabled: data.audio_enabled ?? false,
        usbRedirect: data.usb_redirect ?? false,
        isVM: data.instance_type === "virtual-machine",
        state: data.state ?? "Unknown",
      };
      setCaps({ status: "ready", caps: next });
      onCapabilities?.(next);
      if (data.state && data.state !== "Unknown") onInstanceStatus(data.state as InstanceStatus);
    } catch (error) {
      setCaps({ status: "error", message: error instanceof Error ? error.message : "The console could not be reached" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instanceName, host, project]);

  useEffect(() => {
    void loadCapabilities();
  }, [loadCapabilities]);

  // A tab, once opened, stays mounted (and connected) until the window closes.
  useEffect(() => {
    setMounted((prev) => (prev.has(activeTab) ? prev : new Set(prev).add(activeTab)));
  }, [activeTab]);

  // Before the first display connect: is somebody else watching?
  useEffect(() => {
    if (activeTab !== "graphics" || graphicsCleared || conflict !== null || caps.status !== "ready") return;
    let cancelled = false;
    consoleAPI.getActiveSessions(instanceName, host, project).then(
      (sessions) => {
        if (cancelled) return;
        // Our own serial console is one of them when that tab is open.
        const others = mounted.has("serial") ? sessions.slice(1) : sessions;
        if (others.length > 0) setConflict(others);
        else setGraphicsCleared(true);
      },
      () => !cancelled && setGraphicsCleared(true),
    );
    return () => {
      cancelled = true;
    };
  }, [activeTab, caps.status, conflict, graphicsCleared, host, instanceName, mounted, project]);

  // The front tab's connection is what the title and tray show. No tab mounts while the
  // capabilities failed, so the failure itself is the status.
  const capsError = caps.status === "error" ? caps.message : null;
  useEffect(() => {
    if (capsError !== null) onConnectionStatus?.("error", capsError);
    else if (activeTab === "graphics") onConnectionStatus?.(graphicsToConnection(graphics.status));
    else if (activeTab === "text") onConnectionStatus?.(textStatus);
    else if (activeTab === "serial") onConnectionStatus?.(serialStatus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, capsError, graphics.status, textStatus, serialStatus]);

  // Keyboard focus follows the front tab.
  useEffect(() => {
    if (!visible) return;
    const timer = window.setTimeout(() => {
      if (activeTab === "graphics") graphicsRef.current?.focus();
      else if (activeTab === "text") textRef.current?.focus();
      else if (activeTab === "serial") serialRef.current?.focus();
    }, 50);
    return () => window.clearTimeout(timer);
  }, [activeTab, visible, graphics.status]);

  // Terminal zoom is a font size, so the glyphs stay sharp.
  useEffect(() => {
    const px = Math.round((DEFAULT_FONT_SIZE * textZoom) / 100);
    textRef.current?.setFontSize(px);
    serialRef.current?.setFontSize(px);
  }, [textZoom]);

  const onRootKey = (event: React.KeyboardEvent) => {
    if (event.ctrlKey && event.shiftKey && event.code === "KeyF" && (activeTab === "text" || activeTab === "serial")) {
      event.preventDefault();
      setPanels((p) => ({ ...p, search: true }));
    }
  };

  const textTarget = activeTab === "serial" ? serialRef.current : textRef.current;

  const paste = useCallback(async (text: string) => {
    if (activeTab === "graphics") {
      const handle = graphicsRef.current;
      if (!handle) return;
      if (handle.setGuestClipboard(text)) {
        toast({ title: "On the guest's clipboard", description: "Paste it in the guest with Ctrl+V." });
      } else {
        const skipped = await handle.typeText(text);
        toast({
          title: "Typed into the guest",
          description: skipped ? `${skipped} character(s) have no key on a US layout and were skipped.` : `${text.length} character(s).`,
        });
      }
      handle.focus();
      return;
    }
    textTarget?.paste(text);
    textTarget?.focus();
  }, [activeTab, textTarget]);

  const onScreenKey = useCallback((key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }, skipFocus?: boolean) => {
    if (activeTab === "graphics") {
      graphicsRef.current?.sendKey(key, modifiers);
      if (!skipFocus) graphicsRef.current?.focus();
      return;
    }
    textTarget?.sendRaw(terminalSequence(key, modifiers));
    if (!skipFocus) textTarget?.focus();
  }, [activeTab, textTarget]);

  const onPowerDone = useCallback((operation: PowerOperation) => {
    if (operation === "stop") return;
    // The display and the serial line follow a restart on their own (the relay closes with 1012
    // when the instance powers down). This brings back a tab that is not back yet, a display in
    // its backoff or a serial console that ended, without dropping one that already is. A shell
    // waits for the agent, so the Text tab offers a new session instead (Enter or the button).
    window.setTimeout(() => {
      if (!liveRef.current.graphics) graphicsRef.current?.reconnect();
      if (!liveRef.current.serial) serialRef.current?.reconnect();
    }, 2500);
  }, []);

  const setResizeGuestPreference = (enabled: boolean) => {
    setResizeGuest(enabled);
    try {
      localStorage.setItem(RESIZE_PREF_KEY, enabled ? "1" : "0");
    } catch {
      // per-browser convenience only
    }
  };

  if (caps.status === "loading") {
    return (
      <div className="flex flex-1 items-center justify-center" role="status" aria-live="polite">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="h-7 w-7 animate-spin text-primary motion-reduce:animate-none" aria-hidden="true" />
          <p className="text-sm text-muted-foreground">Opening the console of {instanceName}…</p>
        </div>
      </div>
    );
  }
  if (caps.status === "error") {
    return (
      <div className="flex flex-1 items-center justify-center px-6" role="alert">
        <div className="flex max-w-md flex-col items-center gap-3 text-center">
          <AlertCircle className="h-7 w-7 text-destructive" aria-hidden="true" />
          <p className="text-sm text-muted-foreground">{caps.message}</p>
          <Button variant="outline" size="sm" onClick={() => void loadCapabilities()}>Retry</Button>
        </div>
      </div>
    );
  }

  const capabilities = caps.caps;
  const isGraphics = activeTab === "graphics";
  const isTerminal = activeTab === "text" || activeTab === "serial";
  const graphicsAvailable = capabilities.isVM && capabilities.graphicsConsole;

  const header = (
    <div
      className={cn(
        chrome.fullscreen &&
          "absolute inset-x-0 top-0 z-30 bg-background shadow-md transition-transform duration-200 motion-reduce:transition-none",
        chrome.fullscreen && !chrome.revealed && "-translate-y-full",
      )}
      onMouseEnter={chrome.reveal}
      onMouseLeave={chrome.conceal}
      onFocusCapture={chrome.reveal}
      onBlurCapture={chrome.conceal}
    >
      <ConsoleTabs activeTab={activeTab} onTabChange={onTabChange} instanceType={instanceType} capabilities={capabilities} />
      {(isGraphics || isTerminal) && (
        <ConsoleToolbar
          instanceName={instanceName}
          instanceType={instanceType}
          host={host}
          project={project}
          instanceStatus={instanceStatus}
          onInstanceStatus={onInstanceStatus}
          onPowerDone={onPowerDone}
          onSendKeys={isGraphics ? (key) => graphicsRef.current?.sendKey(key) : undefined}
          showKeyboard={panels.keyboard}
          onToggleKeyboard={() => setPanels((p) => ({ ...p, keyboard: !p.keyboard }))}
          showCursor={panels.cursor}
          onToggleCursor={isGraphics ? () => setPanels((p) => ({ ...p, cursor: !p.cursor })) : undefined}
          showClipboard={panels.clipboard}
          onToggleClipboard={() => setPanels((p) => ({ ...p, clipboard: !p.clipboard }))}
          onToggleSearch={isTerminal ? () => setPanels((p) => ({ ...p, search: !p.search })) : undefined}
          zoom={isGraphics ? zoom : textZoom}
          onZoomChange={(next) => (isGraphics ? setZoom(next) : setTextZoom(typeof next === "number" ? next : 100))}
          allowFit={isGraphics}
          resizeGuest={resizeGuest}
          onResizeGuestChange={isGraphics ? setResizeGuestPreference : undefined}
          agentConnected={graphics.agent}
          onReconnect={() => {
            if (isGraphics) graphicsRef.current?.reconnect();
            else textTarget?.reconnect();
          }}
          canOperate={canOperate}
          operateReason={operateReason}
          status={isGraphics ? (
            <ConnectionDetails
              status={graphics.status}
              stats={graphics.stats}
              mouseMode={graphics.mouseMode}
              agentConnected={graphics.agent}
              resolution={graphics.view?.surface ?? null}
              scale={graphics.view?.scale ?? null}
            />
          ) : undefined}
          guestActions={isGraphics && graphicsAvailable ? (
            <ConsoleActions
              instanceName={instanceName}
              host={host}
              project={project}
              getConnection={() => graphicsRef.current?.getSpiceConnection() ?? null}
              getCanvas={() => graphicsRef.current?.getCanvas() ?? null}
              getFullscreenTarget={getFullscreenTarget}
              canUseConsole={canUseConsole}
              onAfterSend={() => graphicsRef.current?.focus()}
            />
          ) : undefined}
        />
      )}
    </div>
  );

  return (
    <div ref={rootRef} className="relative flex min-h-0 flex-1 flex-col" onKeyDown={onRootKey}>
      {chrome.fullscreen && (
        <div className="absolute inset-x-0 top-0 z-30 h-2" onMouseEnter={chrome.reveal} aria-hidden="true" />
      )}
      {header}

      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        {panels.clipboard && (
          <ClipboardManager onClose={() => setPanels((p) => ({ ...p, clipboard: false }))} onInsert={(text) => void paste(text)} />
        )}

        <div
          className="relative min-h-0 flex-1"
          onMouseDown={(e) => e.stopPropagation()}
          onTouchStart={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
        >
          {mounted.has("graphics") && graphicsAvailable && (
            <div className={cn("absolute inset-0", !isGraphics && "hidden")}>
              {graphicsCleared ? (
                <GraphicsConsole
                  ref={graphicsRef}
                  consoleId={consoleId}
                  instanceName={instanceName}
                  host={host}
                  project={project}
                  zoom={zoom}
                  resizeGuest={resizeGuest}
                  active={visible && isGraphics}
                  onStatus={(status) => {
                    liveRef.current.graphics = status === "connected";
                    setGraphics((g) => ({ ...g, status }));
                  }}
                  onStats={(stats) => setGraphics((g) => ({ ...g, stats }))}
                  onAgent={(agent) => setGraphics((g) => ({ ...g, agent }))}
                  onMouseMode={(mouseMode) => setGraphics((g) => ({ ...g, mouseMode }))}
                  onView={(view) => setGraphics((g) => ({ ...g, view }))}
                  onGuestClipboard={rememberClipboard}
                />
              ) : conflict ? (
                <ConflictNotice
                  sessions={conflict}
                  onOpen={() => {
                    setConflict(null);
                    setGraphicsCleared(true);
                  }}
                  onCancel={() => onTabChange("text")}
                />
              ) : (
                <div className="flex h-full items-center justify-center bg-black" role="status">
                  <Loader2 className="h-7 w-7 animate-spin text-white/70 motion-reduce:animate-none" aria-hidden="true" />
                </div>
              )}
            </div>
          )}
          {mounted.has("text") && (
            <div className={cn("absolute inset-0", activeTab !== "text" && "hidden")}>
              <TextConsole
                ref={textRef}
                consoleId={consoleId}
                instanceName={instanceName}
                host={host}
                project={project}
                fontSize={Math.round((DEFAULT_FONT_SIZE * textZoom) / 100)}
                reportToStore={reportToStore}
                onStatus={setTextStatus}
                onSessionId={onSessionId}
              />
            </div>
          )}
          {mounted.has("serial") && (
            <div className={cn("absolute inset-0", activeTab !== "serial" && "hidden")}>
              <SerialConsole
                ref={serialRef}
                instanceName={instanceName}
                host={host}
                project={project}
                fontSize={Math.round((DEFAULT_FONT_SIZE * textZoom) / 100)}
                onStatus={(status) => {
                  liveRef.current.serial = status === "connected";
                  setSerialStatus(status);
                }}
              />
            </div>
          )}
          {activeTab === "iso" && capabilities.isVM && (
            <div className="absolute inset-0 overflow-auto">
              <ISOManagerPanel consoleId={consoleId} instanceName={instanceName} host={host} project={project} />
            </div>
          )}
          {isTerminal && panels.search && (
            <SearchBar
              onSearch={(term, forward) => {
                const target = activeTab === "serial" ? serialRef.current : textRef.current;
                return target ? target.search(term, forward) : false;
              }}
              onClose={() => {
                setPanels((p) => ({ ...p, search: false }));
                textTarget?.focus();
              }}
            />
          )}
          {panels.cursor && isGraphics && (
            <OnScreenCursor screenId={`spice-screen-${consoleId}`} isActive onClose={() => setPanels((p) => ({ ...p, cursor: false }))} />
          )}
        </div>
      </div>

      {panels.keyboard && (isGraphics || isTerminal) && (
        <OnScreenKeyboard onKeyPress={onScreenKey} onClose={() => setPanels((p) => ({ ...p, keyboard: false }))} />
      )}

      {!chrome.fullscreen && (
        <ConsoleResourceBar
          sessionId={sessionId}
          instanceName={instanceName}
          host={host}
          project={project}
          connectionStatus={isGraphics ? graphicsToConnection(graphics.status) : activeTab === "serial" ? serialStatus : textStatus}
          protocol={isGraphics ? "graphics" : "terminal"}
          compact={!isFullPage}
        />
      )}
    </div>
  );
}
