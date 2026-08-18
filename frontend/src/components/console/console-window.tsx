/**
 * Console Window - Main console container with tabs, toolbar, and content.
 * Supports floating/draggable window mode and fullscreen mode.
 */

"use client";

import * as React from "react";
import { useState, useEffect, useCallback, useMemo } from "react";
import Draggable from "react-draggable";
import { Resizable } from "react-resizable";
import {
  X,
  Minus,
  Maximize2,
  Minimize2,
  Monitor,
  Terminal,
  Loader2,
  AlertCircle,
  Server,
  Grip,
  ExternalLink,
  AlertTriangle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
import { useConsoleStore, ConsoleTab } from "@/lib/console-store";
import { API_URL, getAccessToken } from "@/lib/api";
import { ConsoleTabs } from "./console-tabs";
import { ConsoleToolbar } from "./console-toolbar";
import { TextConsole, TextConsoleHandle } from "./text-console";
import { GraphicsConsole, GraphicsConsoleHandle } from "./graphics-console";
// import { BootPriorityPanel } from "./boot-priority-panel"; // Boot Order tab is hidden
import { ISOManagerPanel } from "./iso-manager-panel";
import { OnScreenKeyboard } from "./on-screen-keyboard";
import { OnScreenCursor } from "./on-screen-cursor";
import { ClipboardManager } from "./clipboard-manager";
import { ConsoleResourceBar } from "./resource-bar";

import "react-resizable/css/styles.css";

// Types for session conflict
interface ActiveSession {
  operation_id: string;
  instance_name: string;
  description: string;
  created_at: string;
  may_cancel: boolean;
}

interface ConsoleWindowProps {
  consoleId: string;
  onClose?: () => void;
  onMinimize?: () => void;
  onMaximize?: () => void;
  isFullPage?: boolean;
  className?: string;
}

export function ConsoleWindow({
  consoleId,
  onClose,
  onMinimize,
  onMaximize,
  isFullPage = false,
  className,
}: ConsoleWindowProps) {
  const {
    consoles,
    setActiveTab,
    updateConnectionStatus,
    setCapabilities,
    bringToFront,
    updateConsolePosition,
    closeConsole,
    updateInstanceStatus,
  } = useConsoleStore();

  const consoleInstance = consoles.find((c) => c.id === consoleId);
  
  // Local state for window dimensions
  const [size, setSize] = useState({ width: 900, height: 650 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sessionConflict, setSessionConflict] = useState<{
    show: boolean;
    sessions: ActiveSession[];
  }>({ show: false, sessions: [] });
  const [sessionCheckComplete, setSessionCheckComplete] = useState(false);
  const [killingSession, setKillingSession] = useState(false);
  const nodeRef = React.useRef<HTMLDivElement>(null);
  const graphicsRef = React.useRef<GraphicsConsoleHandle | null>(null);
  const textRef = React.useRef<TextConsoleHandle | null>(null);
  
  // New feature states
  const [showKeyboard, setShowKeyboard] = useState(false);
  const [showCursor, setShowCursor] = useState(false);
  const [showClipboard, setShowClipboard] = useState(false);
  const [zoom, setZoom] = useState(100);

  // Skip session conflict check for embedded console - just connect directly
  // Session conflict check is only useful for full page console opened from external link
  // LXD handles multiple console connections gracefully
  useEffect(() => {
    if (!consoleInstance) return;
    // Mark as complete immediately - no session check needed for embedded console
    setSessionCheckComplete(true);
  }, [consoleInstance?.instanceName, consoleInstance?.host, consoleInstance?.project]);

  // Kill existing session
  const handleKillSession = async (operationId: string) => {
    if (!consoleInstance) return;
    
    setKillingSession(true);
    try {
      const response = await fetch(
        `${API_URL}/console/${encodeURIComponent(consoleInstance.instanceName)}/active-sessions/${operationId}` +
          `?host=${encodeURIComponent(consoleInstance.host)}`,
        { method: "DELETE", headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
      );
      
      if (response.ok) {
        // Remove killed session from list
        setSessionConflict((prev) => ({
          ...prev,
          sessions: prev.sessions.filter((s) => s.operation_id !== operationId),
          show: prev.sessions.length > 1,
        }));
      }
    } catch (err) {
      console.error("[Console] Failed to kill session:", err);
    } finally {
      setKillingSession(false);
    }
  };

  // Fetch capabilities on mount, and again when the operator retries.
  // important: retry re-runs this fetch. It used to call window.location.reload(),
  // which in a multi-window console manager tore down every other live console
  // and terminal session to recover one failed panel.
  const fetchCapabilities = useCallback(async () => {
    if (!consoleInstance) return;
    {
      setLoading(true);
      setError(null);

      try {
        const response = await fetch(
          `${API_URL}/console/${encodeURIComponent(consoleInstance.instanceName)}/capabilities` +
            `?host=${encodeURIComponent(consoleInstance.host)}` +
            `&project=${encodeURIComponent(consoleInstance.project)}`,
          { headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
        );

        if (!response.ok) {
          throw new Error("Failed to fetch capabilities");
        }

        const data = await response.json();

        // Map backend response to frontend capabilities
        // Backend returns: text_console, graphics_console, graphics_type, instance_type, state
        setCapabilities(consoleId, {
          textConsole: data.text_console ?? true,
          graphicsConsole: data.graphics_console ?? false,
          graphicsType: data.graphics_type ?? 'none',
          graphicsAddress: data.graphics_address,
          graphicsPort: data.graphics_port,
          audioEnabled: data.audio_enabled ?? false,
          usbRedirect: data.usb_redirect ?? false,
          isVM: data.instance_type === 'virtual-machine',
          state: data.state ?? 'Unknown',
        });
        
        // Update instance status from backend response to ensure correct button state
        const instanceState = data.state ?? 'Unknown';
        if (instanceState && instanceState !== 'Unknown') {
          updateInstanceStatus(consoleId, instanceState as any);
        }
      } catch (err) {
        // Use globalThis.console to avoid shadowing
        globalThis.console.error("Error fetching capabilities:", err);
        setError(err instanceof Error ? err.message : "Failed to connect");
      } finally {
        setLoading(false);
      }
    }
  }, [consoleInstance?.instanceName, consoleInstance?.host, consoleInstance?.project, consoleId, setCapabilities, updateInstanceStatus]);

  useEffect(() => {
    void fetchCapabilities();
  }, [fetchCapabilities]);

  // Handle resize
  const onResize = useCallback(
    (_event: React.SyntheticEvent, { size: newSize }: { size: { width: number; height: number } }) => {
      setSize(newSize);
    },
    []
  );

  // Clipboard handlers - paste text to console
  const handlePaste = useCallback(async (text: string) => {
    if (consoleInstance?.activeTab === 'text') {
      textRef.current?.paste?.(text);
      // Focus text console
      setTimeout(() => textRef.current?.focus?.(), 10);
    } else if (consoleInstance?.activeTab === 'graphics') {
      // Use the new pasteText method that sends via SPICE input channel
      await (graphicsRef.current as any)?.pasteText?.(text);
      // Focus graphics console
      setTimeout(() => (graphicsRef.current as any)?.focus?.(), 10);
    }
  }, [consoleInstance?.activeTab]);

  // Key handlers for virtual keyboard and BIOS spam
  const handleSendKeys = useCallback((keys: string) => {
    if (consoleInstance?.activeTab === 'graphics') {
      // Graphics console (SPICE) - use sendKeys which now uses SPICE input channel
      if (keys.toLowerCase() === 'ctrl+alt+del') {
        graphicsRef.current?.sendCtrlAltDel?.();
      } else {
        graphicsRef.current?.sendKeys?.(keys);
      }
    } else if (consoleInstance?.activeTab === 'text') {
      // Text console (xterm) - write key sequence
      textRef.current?.paste?.(keys);
    }
  }, [consoleInstance?.activeTab]);

  // Handler for on-screen keyboard with modifier support
  // skipFocus parameter: if true, don't focus the console after sending key (for native mobile keyboard)
  const handleVirtualKeyPress = useCallback((key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }, skipFocus?: boolean) => {
    // Process key based on modifiers
    let processedKey = key;
    
    // For single character keys, apply shift for uppercase
    if (key.length === 1 && modifiers?.shift) {
      processedKey = key.toUpperCase();
    }
    
    // Handle special keys for terminal (xterm)
    const specialKeyMap: Record<string, string> = {
      'Enter': '\r',
      'Tab': '\t',
      'Escape': '\x1b',
      'Backspace': '\x7f',
      'Delete': '\x1b[3~',
      'ArrowUp': '\x1b[A',
      'ArrowDown': '\x1b[B',
      'ArrowRight': '\x1b[C',
      'ArrowLeft': '\x1b[D',
      'Home': '\x1b[H',
      'End': '\x1b[F',
      'PageUp': '\x1b[5~',
      'PageDown': '\x1b[6~',
      'Insert': '\x1b[2~',
      'F1': '\x1bOP',
      'F2': '\x1bOQ',
      'F3': '\x1bOR',
      'F4': '\x1bOS',
      'F5': '\x1b[15~',
      'F6': '\x1b[17~',
      'F7': '\x1b[18~',
      'F8': '\x1b[19~',
      'F9': '\x1b[20~',
      'F10': '\x1b[21~',
      'F11': '\x1b[23~',
      'F12': '\x1b[24~',
    };
    
    // Check if it's a special key
    if (specialKeyMap[key]) {
      processedKey = specialKeyMap[key];
    }
    
    // Handle Ctrl+key combinations for terminal
    if (modifiers?.ctrl && key.length === 1) {
      const charCode = key.toLowerCase().charCodeAt(0);
      if (charCode >= 97 && charCode <= 122) {
        // Ctrl+A = 1, Ctrl+B = 2, etc.
        processedKey = String.fromCharCode(charCode - 96);
      }
    }
    
    // Send to console
    if (consoleInstance?.activeTab === 'text') {
      textRef.current?.paste?.(processedKey);
      // Focus text console only if not from native keyboard
      if (!skipFocus) {
        setTimeout(() => textRef.current?.focus?.(), 10);
      }
    } else if (consoleInstance?.activeTab === 'graphics') {
      // For graphics console, use the SPICE input channel via sendKeyWithModifiers
      // This properly handles modifier keys (Ctrl, Alt, Shift, Meta)
      if (modifiers?.ctrl && modifiers?.alt && key.toLowerCase() === 'delete') {
        graphicsRef.current?.sendCtrlAltDel?.();
      } else {
        // Use sendKeyWithModifiers if available, otherwise fallback to sendKeys
        const handle = graphicsRef.current as any;
        if (handle?.sendKeyWithModifiers) {
          handle.sendKeyWithModifiers(key, modifiers);
        } else {
          // Fallback: send the processed key (with shift applied for uppercase letters)
          graphicsRef.current?.sendKeys?.(processedKey);
        }
      }
      // Focus graphics console only if not from native keyboard
      if (!skipFocus) {
        setTimeout(() => (graphicsRef.current as any)?.focus?.(), 10);
      }
    }
  }, [consoleInstance?.activeTab]);

  // Focus console content (for fullpage auto-focus)
  const focusConsole = useCallback(() => {
    if (consoleInstance?.activeTab === 'text') {
      textRef.current?.focus?.();
    } else if (consoleInstance?.activeTab === 'graphics') {
      (graphicsRef.current as any)?.focus?.();
    }
  }, [consoleInstance?.activeTab]);

  // Auto-focus when entering fullpage mode or when mounted as fullpage
  useEffect(() => {
    if (isFullPage && !loading && !error && sessionCheckComplete) {
      // Small delay to ensure DOM is ready
      const timer = setTimeout(() => focusConsole(), 200);
      return () => clearTimeout(timer);
    }
  }, [isFullPage, loading, error, sessionCheckComplete, focusConsole]);

  // important: this must sit ABOVE the early return below. It used to be after it, so a render
  // where consoleInstance was null skipped the hook entirely and the next render with an
  // instance called one more hook than the previous one. React maps hook state by call order,
  // so that either throws "Rendered more hooks than during the previous render" or silently
  // hands this memo another hook's slot.
  const availableTabs = useMemo<ConsoleTab[]>(() => {
    const tabs: ConsoleTab[] = ["text"];
    if (consoleInstance?.capabilities?.graphicsConsole) {
      tabs.push("graphics");
    }
    if (consoleInstance?.capabilities?.isVM) {
      tabs.push("boot");
      tabs.push("iso");
    }
    return tabs;
  }, [consoleInstance?.capabilities]);

  if (!consoleInstance) {
    return null;
  }

  // Get status badge
  const getStatusBadge = () => {
    switch (consoleInstance.connectionStatus) {
      case "connected":
        return <Badge variant="default" className="bg-green-500">Connected</Badge>;
      case "connecting":
        return <Badge variant="secondary">Connecting...</Badge>;
      case "disconnected":
        return <Badge variant="outline">Disconnected</Badge>;
      case "error":
        return <Badge variant="destructive">Error</Badge>;
      default:
        return null;
    }
  };

  const windowContent = (
    <>
      {/* Session Conflict Dialog */}
      <AlertDialog open={sessionConflict.show} onOpenChange={(open) => setSessionConflict((prev) => ({ ...prev, show: open }))}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Active Console Session Detected
            </AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <p>
                Another console session is already running for <span className="font-semibold">{consoleInstance?.instanceName}</span>.
              </p>
              <p className="text-sm">
                Running multiple console sessions may cause display issues. You can:
              </p>
              <ul className="text-sm list-disc pl-5 space-y-1">
                <li>Kill the existing session and continue</li>
                <li>Continue anyway (not recommended)</li>
                <li>Cancel and use the existing session</li>
              </ul>
              {sessionConflict.sessions.map((session) => {
                const d = new Date(session.created_at);
                const timeStr = `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`;
                return (
                <div key={session.operation_id} className="mt-2 p-2 bg-muted rounded text-sm">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs">{session.operation_id.substring(0, 12)}...</span>
                    <span className="text-muted-foreground text-xs">
                      {timeStr}
                    </span>
                  </div>
                  <p className="text-muted-foreground">{session.description}</p>
                </div>
              )})}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="flex-col sm:flex-row gap-2">
            <AlertDialogCancel onClick={() => closeConsole(consoleId, false)}>
              Cancel
            </AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={() => {
                sessionConflict.sessions.forEach((s) => handleKillSession(s.operation_id));
                setSessionConflict({ show: false, sessions: [] });
              }}
              disabled={killingSession}
            >
              {killingSession ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Killing...
                </>
              ) : (
                "Kill & Continue"
              )}
            </Button>
            <AlertDialogAction onClick={() => setSessionConflict({ show: false, sessions: [] })}>
              Continue Anyway
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <div
        className={cn(
          "flex flex-col bg-background border rounded-lg shadow-xl overflow-hidden",
          isFullPage ? "h-full" : "",
          className
        )}
        style={!isFullPage ? { width: size.width, height: size.height } : undefined}
      >
        {/* Title bar */}
        <div
          className={cn(
            "flex items-center gap-2 px-3 py-2 border-b bg-muted/50",
            !isFullPage && "cursor-move console-drag-handle"
          )}
          onClick={() => bringToFront(consoleId)}
        >
        <Grip className="h-4 w-4 text-muted-foreground" />
        <div className="flex items-center gap-2 flex-1 min-w-0">
          {consoleInstance.capabilities?.isVM ? (
            <Monitor className="h-4 w-4 text-blue-500" />
          ) : (
            <Terminal className="h-4 w-4 text-green-500" />
          )}
          <span className="font-medium truncate">{consoleInstance.instanceName}</span>
          <Badge variant="outline" className="text-xs">
            {consoleInstance.host}
          </Badge>
          {consoleInstance.project !== "default" && (
            <Badge variant="secondary" className="text-xs">
              {consoleInstance.project}
            </Badge>
          )}
          {getStatusBadge()}
        </div>

        {/* Window controls */}
        <div 
          className="flex items-center gap-1 touch-manipulation"
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
          onTouchStart={(e) => e.stopPropagation()}
          onTouchEnd={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
        >
          {/* Open in New Tab */}
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
            onClick={async (e) => {
              e.stopPropagation();
              
              // 1. First disconnect the current graphics console to release the session
              if (graphicsRef.current) {
                graphicsRef.current.disconnect();
              }
              
              // 2. Small delay to allow WebSocket to close properly
              await new Promise(resolve => setTimeout(resolve, 100));
              
              // 3. Kill all sessions for this instance
              try {
                await fetch(
                  `${API_URL}/console/${encodeURIComponent(consoleInstance.instanceName)}/kill-all-sessions?host=${encodeURIComponent(consoleInstance.host)}`,
                  { method: 'POST', headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
                );
              } catch (err) {
                // Ignore - sessions might already be closed
              }
              
              // 4. Another small delay to ensure backend processed the kill
              await new Promise(resolve => setTimeout(resolve, 200));
              
              // 5. Build URL with continuation flag (skip session check in new tab)
              const tab = consoleInstance.activeTab === 'graphics' ? 'graphics' : 'text';
              const url = `/console/${consoleInstance.id}?instance=${encodeURIComponent(consoleInstance.instanceName)}&host=${encodeURIComponent(consoleInstance.host)}&project=${encodeURIComponent(consoleInstance.project)}&type=${encodeURIComponent(consoleInstance.instanceType)}&tab=${tab}&continuation=1`;
              
              // 6. Close console window (without killing again - already done)
              closeConsole(consoleId, false);
              
              // 7. Open in new tab
              window.open(url, '_blank');
            }}
            title="Open in New Tab"
          >
            <ExternalLink className="h-4 w-4" />
          </Button>
          {onMinimize && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
              onClick={(e) => {
                e.stopPropagation();
                onMinimize();
              }}
              onTouchEnd={(e) => { e.preventDefault(); e.stopPropagation(); onMinimize(); }}
              title="Minimize"
            >
              <Minus className="h-4 w-4" />
            </Button>
          )}
          {onMaximize && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 min-h-[32px] min-w-[32px] touch-manipulation active:scale-95"
              onClick={(e) => {
                e.stopPropagation();
                onMaximize();
              }}
              onTouchEnd={(e) => { e.preventDefault(); e.stopPropagation(); onMaximize(); }}
              title={isFullPage ? "Exit Full Page" : "Full Page Mode"}
            >
              {isFullPage ? (
                <Minimize2 className="h-4 w-4" />
              ) : (
                <Maximize2 className="h-4 w-4" />
              )}
            </Button>
          )}
          {onClose && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 min-h-[32px] min-w-[32px] hover:bg-destructive hover:text-destructive-foreground touch-manipulation active:scale-95"
              onClick={(e) => {
                e.stopPropagation();
                onClose();
              }}
              onTouchEnd={(e) => { e.preventDefault(); e.stopPropagation(); onClose(); }}
              title="Close"
            >
              <X className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {/* Loading state */}
      {loading && (
        <div className="flex-1 flex items-center justify-center">
          <div className="flex flex-col items-center gap-4">
            <Loader2 className="h-8 w-8 animate-spin text-primary" />
            <p className="text-sm text-muted-foreground">Connecting to {consoleInstance.instanceName}...</p>
          </div>
        </div>
      )}

      {/* Error state */}
      {error && !loading && (
        <div className="flex-1 flex items-center justify-center">
          <div className="flex flex-col items-center gap-4">
            <AlertCircle className="h-8 w-8 text-destructive" />
            <p className="text-sm text-muted-foreground">{error}</p>
            <Button variant="outline" size="sm" onClick={() => void fetchCapabilities()}>
              Retry
            </Button>
          </div>
        </div>
      )}

      {/* Main content */}
      {!loading && !error && (
        <>
          {/* Tabs */}
          <ConsoleTabs
            activeTab={consoleInstance.activeTab}
            onTabChange={(tab) => setActiveTab(consoleId, tab)}
            instanceType={consoleInstance.instanceType}
            capabilities={consoleInstance.capabilities}
          />

          {/* Toolbar */}
          {(consoleInstance.activeTab === "text" || consoleInstance.activeTab === "graphics") && (
            <ConsoleToolbar
              consoleId={consoleId}
              instanceName={consoleInstance.instanceName}
              instanceType={consoleInstance.instanceType}
              host={consoleInstance.host}
              project={consoleInstance.project}
              instanceStatus={consoleInstance.instanceStatus}
              isFullPage={isFullPage || consoleInstance.fullPage}
              onFullPageToggle={onMaximize || (() => {})}
              onSendKeys={handleSendKeys}
              onPaste={handlePaste}
              showKeyboard={showKeyboard}
              onToggleKeyboard={() => setShowKeyboard(prev => !prev)}
              showCursor={showCursor}
              onToggleCursor={() => setShowCursor(prev => !prev)}
              showClipboard={showClipboard}
              onToggleClipboard={() => setShowClipboard(prev => !prev)}
              zoom={zoom}
              onZoomChange={setZoom}
              onReconnect={() => graphicsRef.current?.reconnect()}
            />
          )}

          {/* Console content with sidebar panels */}
          <div className="flex-1 min-h-0 flex flex-col relative overflow-hidden">
            {/* Main area with optional sidebar */}
            <div className="flex-1 min-h-0 flex relative overflow-hidden">
              {/* Clipboard Manager Sidebar */}
              {showClipboard && (
                <ClipboardManager
                  onClose={() => setShowClipboard(false)}
                  onInsert={handlePaste}
                />
              )}

              {/* Console display area */}
              <div 
                className="flex-1 min-h-0 relative"
                onMouseDown={(e) => {
                  // Stop propagation to prevent draggable from catching mouse events inside console
                  e.stopPropagation();
                }}
                onTouchStart={(e) => {
                  // Stop propagation to prevent draggable from catching touch events
                  // preventDefault() is NOT called here - let touch-to-mouse translation work
                  // so SPICE can receive simulated mouse events
                  e.stopPropagation();
                }}
                onTouchMove={(e) => {
                  // Stop propagation to prevent window drag, but allow touch move inside canvas
                  e.stopPropagation();
                }}
                onTouchEnd={(e) => {
                  e.stopPropagation();
                }}
                onPointerDown={(e) => {
                  e.stopPropagation();
                }}
                onPointerMove={(e) => {
                  e.stopPropagation();
                }}
                onPointerUp={(e) => {
                  e.stopPropagation();
                }}
              >
                {/* Console display - zoom applies to both text and graphics */}
                <div 
                  className="absolute inset-0 overflow-auto"
                  style={consoleInstance.activeTab === "text" && zoom !== 100 ? { 
                    // Use CSS zoom for text console - works better than transform
                    zoom: `${zoom}%`,
                  } : undefined}
                >
                {consoleInstance.activeTab === "text" && (
                  <TextConsole
                    ref={textRef}
                    consoleId={consoleId}
                    instanceName={consoleInstance.instanceName}
                    host={consoleInstance.host}
                    project={consoleInstance.project}
                    onReady={() => isFullPage && focusConsole()}
                  />
                )}

                {consoleInstance.activeTab === "graphics" && consoleInstance.capabilities?.graphicsConsole && (
                  // Only render GraphicsConsole after session check is complete and no conflict showing
                  sessionCheckComplete && !sessionConflict.show ? (
                    <GraphicsConsole
                      ref={graphicsRef}
                      consoleId={consoleId}
                      instanceName={consoleInstance.instanceName}
                      host={consoleInstance.host}
                      project={consoleInstance.project}
                      zoom={zoom}
                    />
                  ) : (
                    <div className="flex-1 flex items-center justify-center bg-black">
                      <div className="flex flex-col items-center gap-4">
                        <Loader2 className="h-8 w-8 animate-spin text-white/70" />
                        <p className="text-sm text-white/70">
                          {sessionConflict.show ? "Waiting for session conflict resolution..." : "Checking active sessions..."}
                        </p>
                      </div>
                    </div>
                  )
                )}

                {/* Boot Order panel is hidden (tab removed from console-tabs.tsx)
                    Uncomment below to re-enable the Boot Order functionality
                {consoleInstance.activeTab === "boot" && consoleInstance.capabilities?.isVM && (
                  <BootPriorityPanel
                    host={consoleInstance.host}
                    project={consoleInstance.project}
                    currentInstanceName={consoleInstance.instanceName}
                  />
                )}
                */}

                {consoleInstance.activeTab === "iso" && consoleInstance.capabilities?.isVM && (
                  <ISOManagerPanel
                    consoleId={consoleId}
                    instanceName={consoleInstance.instanceName}
                    host={consoleInstance.host}
                    project={consoleInstance.project}
                  />
                )}
              </div>

              {/* On-Screen Cursor Overlay - positioned above console */}
              {showCursor && consoleInstance.activeTab === "graphics" && (
                <OnScreenCursor
                  screenId={`spice-screen-${consoleId}`}
                  isActive={showCursor}
                  onClose={() => setShowCursor(false)}
                />
              )}
            </div>
            {/* End of Console display area */}
          </div>
          {/* End of Main area with optional sidebar */}

            {/* On-Screen Keyboard (at bottom, outside the flex row) */}
            {showKeyboard && (
              <OnScreenKeyboard
                onKeyPress={handleVirtualKeyPress}
                onClose={() => setShowKeyboard(false)}
              />
            )}
          </div>
          {/* End of Console content with sidebar panels */}

          {/* Session Intelligence & Resource Display Bar */}
          <ConsoleResourceBar
            sessionId={consoleInstance.sessionId ?? null}
            instanceName={consoleInstance.instanceName}
            host={consoleInstance.host}
            project={consoleInstance.project}
            connectionStatus={consoleInstance.connectionStatus}
            protocol={consoleInstance.activeTab === "graphics" ? "graphics" : "terminal"}
            compact={!isFullPage}
          />
        </>
      )}
    </div>
    </>
  );

  // Full page mode - no draggable wrapper
  if (isFullPage) {
    return windowContent;
  }

  // Calculate zIndex from zIndexOrder
  const zIndexOrder = useConsoleStore.getState().zIndexOrder;
  const zIndex = 1000 + zIndexOrder.indexOf(consoleId);

  // Floating window mode - draggable and resizable
  return (
    <Draggable
      nodeRef={nodeRef}
      handle=".console-drag-handle"
      bounds={{
        left: 0,
        top: 0,
        right: window.innerWidth - size.width,
        bottom: window.innerHeight - size.height - 100,
      }}
      position={consoleInstance.position}
      onStop={(_e, data) => {
        updateConsolePosition(consoleId, { x: data.x, y: data.y });
      }}
    >
      <div
        ref={nodeRef}
        className="absolute"
        style={{ zIndex }}
        onClick={() => bringToFront(consoleId)}
      >
        <Resizable
          width={size.width}
          height={size.height}
          onResize={onResize}
          minConstraints={[600, 400]}
          maxConstraints={[1400, 900]}
          resizeHandles={["se", "e", "s"]}
        >
          {windowContent}
        </Resizable>
      </div>
    </Draggable>
  );
}
