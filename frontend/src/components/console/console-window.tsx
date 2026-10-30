/**
 * Console Window: the floating (or full-page) frame around a ConsoleWorkspace.
 *
 * important: the element tree is the same whether the window floats, fills the page or is
 * minimized, so React never remounts the workspace and its consoles stay connected. Minimizing or
 * toggling full page used to unmount it, which dropped the shell and the display every time.
 */

"use client";

import * as React from "react";
import { memo, useCallback, useEffect, useRef, useState } from "react";
import Draggable from "react-draggable";
import { Resizable } from "react-resizable";
import { ExternalLink, Grip, Maximize2, Minimize2, Minus, Monitor, Terminal, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { OverflowText } from "@/components/ui/overflow-text";
import { SessionStatusBadge } from "@/components/terminal/session-status";
import { cn } from "@/lib/utils";
import { useConsoleStore, type ConsoleCapabilities, type ConsoleConnectionStatus, type ConsoleTab, type InstanceStatus } from "@/lib/console-store";
import { ConsoleWorkspace } from "./console-workspace";

import "react-resizable/css/styles.css";
import { toast } from "@/hooks/use-toast";
import { hasDrafts } from "@/lib/instance-files/draft-store";

interface ConsoleWindowProps {
  consoleId: string;
  onClose?: () => void;
  onMinimize?: () => void;
  onMaximize?: () => void;
  isFullPage?: boolean;
  /** False while minimized: the window stays mounted (and connected) but is not shown. */
  visible?: boolean;
  className?: string;
}

const MIN_SIZE = { width: 600, height: 400 };
const WINDOW_BUTTON = "h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95";

const MemoWorkspace = memo(ConsoleWorkspace);

export function ConsoleWindow({ consoleId, onClose, onMinimize, onMaximize, isFullPage = false, visible = true, className }: ConsoleWindowProps) {
  const consoleInstance = useConsoleStore(useCallback((s) => s.consoles.find((c) => c.id === consoleId), [consoleId]));
  const zIndex = useConsoleStore((s) => 1000 + Math.max(0, s.zIndexOrder.indexOf(consoleId)));
  const setActiveTab = useConsoleStore((s) => s.setActiveTab);
  const updateConnectionStatus = useConsoleStore((s) => s.updateConnectionStatus);
  const updateInstanceStatus = useConsoleStore((s) => s.updateInstanceStatus);
  const setCapabilities = useConsoleStore((s) => s.setCapabilities);
  const setConsoleSessionId = useConsoleStore((s) => s.setConsoleSessionId);
  const bringToFront = useConsoleStore((s) => s.bringToFront);
  const updateConsolePosition = useConsoleStore((s) => s.updateConsolePosition);
  const closeConsole = useConsoleStore((s) => s.closeConsole);

  const nodeRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState(() => ({
    width: Math.max(MIN_SIZE.width, consoleInstance?.position.width ?? 900),
    height: Math.max(MIN_SIZE.height, consoleInstance?.position.height ?? 650),
  }));
  const [viewport, setViewport] = useState({ width: 1400, height: 900 });

  useEffect(() => {
    const read = () => setViewport({ width: window.innerWidth, height: window.innerHeight });
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, []);

  // Stable callbacks, so the memoized workspace does not re-render while the window is resized.
  const onTabChange = useCallback((tab: ConsoleTab) => setActiveTab(consoleId, tab), [consoleId, setActiveTab]);
  const onInstanceStatus = useCallback((status: InstanceStatus) => updateInstanceStatus(consoleId, status), [consoleId, updateInstanceStatus]);
  const onConnectionStatus = useCallback(
    (status: ConsoleConnectionStatus, detail?: string) => updateConnectionStatus(consoleId, status, detail),
    [consoleId, updateConnectionStatus],
  );
  const onCapabilities = useCallback((caps: ConsoleCapabilities) => setCapabilities(consoleId, caps), [consoleId, setCapabilities]);
  const onSessionId = useCallback((sessionId: string) => setConsoleSessionId(consoleId, sessionId), [consoleId, setConsoleSessionId]);
  const getFullscreenTarget = useCallback(() => frameRef.current, []);

  if (!consoleInstance) return null;

  const openInNewTab = () => {
    // A new tab is a new document, so unsaved text in this one's editor would be left behind.
    if (hasDrafts()) {
      toast({ title: "Unsaved changes", description: "Save or discard the file you are editing first." });
      return;
    }
    const tab = consoleInstance.activeTab === "iso" || consoleInstance.activeTab === "boot" ? "text" : consoleInstance.activeTab;
    const params = new URLSearchParams({
      instance: consoleInstance.instanceName,
      host: consoleInstance.host,
      project: consoleInstance.project,
      type: consoleInstance.instanceType,
      tab,
      continuation: "1",
    });
    // Closing unmounts this window, whose sockets close and end its own LXD operations; the new
    // tab opens its own. Nobody else's session is touched.
    closeConsole(consoleId);
    window.open(`/console/${consoleInstance.id}?${params.toString()}`, "_blank", "noopener");
  };

  const width = Math.min(size.width, viewport.width - 16);
  const height = Math.min(size.height, viewport.height - 16);
  // note: the stored position is where the window sat on the screen it was opened on. Draggable's
  // bounds apply only while dragging, so on a narrower screen (a phone) the window rendered
  // mostly off screen with its title bar out of reach; it is clamped here instead.
  const maxX = Math.max(0, viewport.width - width);
  const maxY = Math.max(0, viewport.height - height - 48);
  const x = Math.min(Math.max(0, consoleInstance.position.x), maxX);
  const y = Math.min(Math.max(0, consoleInstance.position.y), maxY);

  const frame = (
    <div
      ref={frameRef}
      className={cn("flex flex-col overflow-hidden border bg-background", isFullPage ? "h-full w-full" : "rounded-lg shadow-xl", className)}
      style={!isFullPage ? { width, height } : undefined}
    >
      <div
        className={cn("flex items-center gap-2 border-b bg-muted/50 px-3 py-2", !isFullPage && "console-drag-handle cursor-move")}
        onMouseDown={() => bringToFront(consoleId)}
      >
        {!isFullPage && <Grip className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />}
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {consoleInstance.instanceType === "virtual-machine" ? (
            <Monitor className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          ) : (
            <Terminal className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          )}
          <span className="min-w-0 font-medium" translate="no">
            <OverflowText value={consoleInstance.instanceName} label="instance name" />
          </span>
          <Badge variant="outline" className="hidden shrink-0 text-xs sm:inline-flex" translate="no">{consoleInstance.host}</Badge>
          {consoleInstance.project !== "default" && (
            <Badge variant="secondary" className="hidden shrink-0 text-xs md:inline-flex" translate="no">{consoleInstance.project}</Badge>
          )}
          <SessionStatusBadge status={consoleInstance.connectionStatus} chrome={false} />
        </div>
        <div
          className="flex items-center gap-1 touch-manipulation"
          onMouseDown={(e) => e.stopPropagation()}
          onTouchStart={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
        >
          <Button variant="ghost" size="icon" className={WINDOW_BUTTON} onClick={openInNewTab} aria-label="Open in a new tab" title="Open in a new tab">
            <ExternalLink className="h-4 w-4" aria-hidden="true" />
          </Button>
          {onMinimize && (
            <Button variant="ghost" size="icon" className={WINDOW_BUTTON} onClick={onMinimize} aria-label="Minimize" title="Minimize (stays connected)">
              <Minus className="h-4 w-4" aria-hidden="true" />
            </Button>
          )}
          {onMaximize && (
            <Button
              variant="ghost"
              size="icon"
              className={WINDOW_BUTTON}
              onClick={onMaximize}
              aria-label={isFullPage ? "Exit full page" : "Full page"}
              title={isFullPage ? "Exit full page" : "Full page"}
            >
              {isFullPage ? <Minimize2 className="h-4 w-4" aria-hidden="true" /> : <Maximize2 className="h-4 w-4" aria-hidden="true" />}
            </Button>
          )}
          {onClose && (
            <Button
              variant="ghost"
              size="icon"
              className={cn(WINDOW_BUTTON, "hover:bg-destructive hover:text-destructive-foreground")}
              onClick={onClose}
              aria-label="Close console"
              title="Close"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </Button>
          )}
        </div>
      </div>

      <MemoWorkspace
        consoleId={consoleId}
        instanceName={consoleInstance.instanceName}
        host={consoleInstance.host}
        project={consoleInstance.project}
        instanceType={consoleInstance.instanceType}
        activeTab={consoleInstance.activeTab}
        onTabChange={onTabChange}
        instanceStatus={consoleInstance.instanceStatus}
        onInstanceStatus={onInstanceStatus}
        onConnectionStatus={onConnectionStatus}
        onCapabilities={onCapabilities}
        isFullPage={isFullPage}
        visible={visible}
        getFullscreenTarget={getFullscreenTarget}
        reportToStore
        sessionId={consoleInstance.sessionId ?? null}
        onSessionId={onSessionId}
      />
    </div>
  );

  // One tree for every mode: Draggable and Resizable stay, disabled in full page.
  return (
    <Draggable
      nodeRef={nodeRef}
      handle=".console-drag-handle"
      disabled={isFullPage}
      bounds={{ left: 0, top: 0, right: maxX, bottom: maxY }}
      position={isFullPage ? { x: 0, y: 0 } : { x, y }}
      onStop={(_e, data) => updateConsolePosition(consoleId, { x: data.x, y: data.y })}
    >
      <div
        ref={nodeRef}
        className={cn(isFullPage ? "fixed inset-0" : "absolute", !visible && "hidden")}
        style={{ zIndex: isFullPage ? 2000 : zIndex }}
        onMouseDown={() => bringToFront(consoleId)}
        aria-hidden={!visible}
      >
        <Resizable
          width={width}
          height={height}
          onResize={(_e, data) => setSize(data.size)}
          onResizeStop={(_e, data) => updateConsolePosition(consoleId, { width: data.size.width, height: data.size.height })}
          minConstraints={[Math.min(MIN_SIZE.width, viewport.width - 16), Math.min(MIN_SIZE.height, viewport.height - 16)]}
          maxConstraints={[viewport.width - 16, viewport.height - 16]}
          resizeHandles={isFullPage ? [] : ["se", "e", "s"]}
        >
          {frame}
        </Resizable>
      </div>
    </Draggable>
  );
}
