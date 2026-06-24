/**
 * Expanded Console Page - full-page console view for VMs and containers
 * (route /console/[id]).
 *
 * Thin page chrome over two shared console engines:
 *   - <TextConsole>     (lib/use-terminal-socket) for the shell.
 *   - <GraphicsConsole> (lib/spice) for the SPICE/VGA display.
 * Page-specific chrome (toolbar, dialogs) lives in components/console/full-page.
 */

"use client";

import * as React from "react";
import { Suspense, useEffect, useState, useRef, useCallback } from "react";
import { useParams, useSearchParams } from "next/navigation";
import {
  Terminal as TerminalIcon,
  ArrowLeft,
  Monitor,
  Search,
  CheckCircle2,
  AlertCircle,
  X,
} from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import { API_URL, getAccessToken } from "@/lib/api";
import { OnScreenKeyboard } from "@/components/console/on-screen-keyboard";
import { OnScreenCursor } from "@/components/console/on-screen-cursor";
import { ClipboardManager } from "@/components/console/clipboard-manager";
import { ConsoleResourceBar } from "@/components/console/resource-bar";
import { TextConsole, type TextConsoleHandle } from "@/components/console/text-console";
import { DEFAULT_FONT_SIZE } from "@/lib/xterm-setup";
import {
  GraphicsConsole,
  type GraphicsConsoleHandle,
  type GraphicsStatus,
} from "@/components/console/graphics-console";
import { MAX_PASTE_KEYSTROKE_CHARS } from "@/lib/spice/spice-input";
import { ConsolePageToolbar, type BiosKey } from "@/components/console/full-page/console-page-toolbar";
import { ConsolePageDialogs, type ConflictSession } from "@/components/console/full-page/console-page-dialogs";
import { useInstancePower } from "@/lib/use-instance-power";

type ConsoleStatus = GraphicsStatus;
type ConsoleType = "text" | "graphics";

const BIOS_KEYS: BiosKey[] = [
  { key: "Delete", label: "Del", description: "Most BIOS (Dell, ASUS, MSI)", keyCode: 46 },
  { key: "F2", label: "F2", description: "UEFI/BIOS (Lenovo, Acer, ASUS)", keyCode: 113 },
  { key: "F12", label: "F12", description: "Boot Menu (Dell, Lenovo)", keyCode: 123 },
  { key: "Escape", label: "Esc", description: "Boot Menu (HP)", keyCode: 27 },
  { key: "F10", label: "F10", description: "BIOS (HP, Compaq)", keyCode: 121 },
  { key: "F1", label: "F1", description: "BIOS (Lenovo ThinkPad)", keyCode: 112 },
];

// Special keys -> xterm escape sequences for the on-screen keyboard (text mode).
const TEXT_SPECIAL_KEYS: Record<string, string> = {
  Enter: "\r", Tab: "\t", Escape: "\x1b", Backspace: "\x7f", Delete: "\x1b[3~",
  ArrowUp: "\x1b[A", ArrowDown: "\x1b[B", ArrowRight: "\x1b[C", ArrowLeft: "\x1b[D",
  Home: "\x1b[H", End: "\x1b[F", PageUp: "\x1b[5~", PageDown: "\x1b[6~", Insert: "\x1b[2~",
  F1: "\x1bOP", F2: "\x1bOQ", F3: "\x1bOR", F4: "\x1bOS", F5: "\x1b[15~", F6: "\x1b[17~",
  F7: "\x1b[18~", F8: "\x1b[19~", F9: "\x1b[20~", F10: "\x1b[21~", F11: "\x1b[23~", F12: "\x1b[24~",
};

function ConsoleLoading() {
  return (
    <div className="flex items-center justify-center h-screen bg-[#1a1b26]">
      <div className="flex flex-col items-center gap-4">
        <Spinner className="h-8 w-8 text-blue-400" />
        <span className="text-gray-400">Loading console...</span>
      </div>
    </div>
  );
}

function ConsoleContent() {
  const params = useParams();
  const searchParams = useSearchParams();

  const instanceName = searchParams.get("instance") || "";
  const host = searchParams.get("host") || "";
  const project = searchParams.get("project") || "default";
  const instanceType = searchParams.get("type") || "virtual-machine";
  const initialTab = (searchParams.get("tab") as ConsoleType) || "graphics";
  const isContinuation = searchParams.get("continuation") === "1";

  const isVM = instanceType === "virtual-machine";
  const [activeTab, setActiveTab] = useState<ConsoleType>(isVM ? initialTab : "text");

  const textRef = useRef<TextConsoleHandle | null>(null);
  const graphicsRef = useRef<GraphicsConsoleHandle | null>(null);

  const [sessionConflict, setSessionConflict] = useState<{ show: boolean; sessions: ConflictSession[] }>({
    show: false,
    sessions: [],
  });
  const [sessionCheckComplete, setSessionCheckComplete] = useState(isContinuation);
  const [killingSession, setKillingSession] = useState(false);

  const [textStatus, setTextStatus] = useState<ConsoleStatus>("disconnected");
  const [graphicsStatus, setGraphicsStatus] = useState<ConsoleStatus>("loading");
  const [sessionId, setSessionId] = useState<string>("");
  const [showSearch, setShowSearch] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");

  const [showKeyboard, setShowKeyboard] = useState(false);
  const [showCursor, setShowCursor] = useState(false);
  const [showClipboard, setShowClipboard] = useState(false);
  const [zoom, setZoom] = useState(100);
  const [biosSpamming, setBiosSpamming] = useState(false);
  const [textReconnectKey, setTextReconnectKey] = useState(0);

  const consoleId = String(params.id ?? instanceName);
  const screenId = `spice-screen-${consoleId}`;

  // Check for active sessions before connecting (unless continuation).
  useEffect(() => {
    if (isContinuation || !instanceName || !host) {
      setSessionCheckComplete(true);
      return;
    }
    const check = async () => {
      try {
        const response = await fetch(
          `${API_URL}/console/${encodeURIComponent(instanceName)}/active-sessions` +
            `?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`,
          { headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.has_active_session && data.sessions.length > 0) {
            setSessionConflict({ show: true, sessions: data.sessions });
          }
        }
      } catch (err) {
        console.warn("[Console] Failed to check active sessions:", err);
      } finally {
        setSessionCheckComplete(true);
      }
    };
    check();
  }, [instanceName, host, project, isContinuation]);

  const handleKillSession = useCallback(
    async (operationId: string) => {
      setKillingSession(true);
      try {
        const response = await fetch(
          `${API_URL}/console/${encodeURIComponent(instanceName)}/active-sessions/${operationId}` +
            `?host=${encodeURIComponent(host)}`,
          { method: "DELETE", headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } },
        );
        if (response.ok) {
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
    },
    [instanceName, host],
  );

  const handleSearch = useCallback(() => {
    if (searchTerm) textRef.current?.search(searchTerm);
  }, [searchTerm]);

  const handleReconnect = useCallback(() => {
    if (activeTab === "text") setTextReconnectKey((k) => k + 1);
    else graphicsRef.current?.reconnect();
  }, [activeTab]);

  const power = useInstancePower({ instanceName, host, project, onAfterRestart: handleReconnect });

  const handleBiosSpam = useCallback(
    async (key: string, keyCode: number) => {
      if (biosSpamming) return;
      setBiosSpamming(true);
      toast({ title: `Spamming ${key}`, description: "Sending key 15 times..." });
      for (let i = 0; i < 15; i++) {
        const ok = graphicsRef.current?.sendKeyCode(keyCode);
        if (!ok && i === 0) {
          toast({ title: "Failed", description: "SPICE connection not ready", variant: "destructive" });
          setBiosSpamming(false);
          return;
        }
        await new Promise((r) => setTimeout(r, 100));
      }
      setBiosSpamming(false);
      toast({ title: "Done", description: `Sent ${key} 15 times` });
    },
    [biosSpamming],
  );

  const handleVirtualKeyPress = useCallback(
    (key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }) => {
      if (activeTab === "text") {
        let out = key;
        if (key.length === 1 && modifiers?.shift) out = key.toUpperCase();
        if (TEXT_SPECIAL_KEYS[key]) out = TEXT_SPECIAL_KEYS[key];
        if (modifiers?.ctrl && key.length === 1) {
          const c = key.toLowerCase().charCodeAt(0);
          if (c >= 97 && c <= 122) out = String.fromCharCode(c - 96);
        }
        textRef.current?.paste(out);
        return;
      }
      if (modifiers?.ctrl && modifiers?.alt && (key === "Delete" || key === "Del")) {
        graphicsRef.current?.sendCtrlAltDel();
        return;
      }
      graphicsRef.current?.sendKeyWithModifiers(key, modifiers);
      setTimeout(() => graphicsRef.current?.focus(), 10);
    },
    [activeTab],
  );

  const handleClipboardInsert = useCallback(
    async (text: string) => {
      if (activeTab === "text") {
        textRef.current?.paste(text);
        toast({ title: "Inserted", description: "Text sent to console" });
        return;
      }
      const truncated = text.length > MAX_PASTE_KEYSTROKE_CHARS;
      await graphicsRef.current?.pasteText(text);
      setTimeout(() => graphicsRef.current?.focus(), 10);
      toast(
        truncated
          ? {
              title: "Inserted (truncated)",
              description: `Only the first ${MAX_PASTE_KEYSTROKE_CHARS} characters were typed. Use the guest agent clipboard for bulk text.`,
              variant: "destructive",
            }
          : { title: "Inserted", description: "Text sent to console" },
      );
    },
    [activeTab],
  );

  const handleZoomIn = useCallback(() => setZoom((z) => Math.min(z + 10, 200)), []);
  const handleZoomOut = useCallback(() => setZoom((z) => Math.max(z - 10, 50)), []);
  const handleZoomReset = useCallback(() => setZoom(100), []);

  const currentStatus = activeTab === "text" ? textStatus : graphicsStatus;
  const consoleReady = sessionCheckComplete && !sessionConflict.show;

  // Text console zoom is applied by changing the font size (WebGL text stays crisp and
  // the grid reflows, unlike CSS zoom which reflows the layout and blurs glyphs). The
  // graphics console consumes `zoom` directly as a prop. Reapplied after a remount
  // (textReconnectKey) so the level survives a reconnect/restart.
  useEffect(() => {
    if (activeTab === "text" && consoleReady) {
      textRef.current?.setFontSize(Math.round((DEFAULT_FONT_SIZE * zoom) / 100));
    }
  }, [zoom, activeTab, consoleReady, textReconnectKey]);

  // Kill backend console ops when the tab is closed.
  useEffect(() => {
    const handleBeforeUnload = () => {
      const killUrl = `${API_URL}/console/${encodeURIComponent(instanceName)}/kill-all-sessions?host=${encodeURIComponent(host)}`;
      navigator.sendBeacon(killUrl, new Blob([""], { type: "application/json" }));
      graphicsRef.current?.disconnect();
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [instanceName, host]);

  // Ctrl+Shift+F toggles search.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key === "F") {
        e.preventDefault();
        setShowSearch((s) => !s);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const StatusBadge = () => {
    switch (currentStatus) {
      case "connecting":
      case "loading":
        return (
          <Badge variant="secondary" className="gap-1 bg-yellow-500/20 text-yellow-400">
            <Spinner className="h-3 w-3" /> Connecting
          </Badge>
        );
      case "connected":
        return (
          <Badge variant="secondary" className="gap-1 bg-green-500/20 text-green-400">
            <CheckCircle2 className="h-3 w-3" /> Connected
          </Badge>
        );
      case "error":
        return (
          <Badge variant="secondary" className="gap-1 bg-red-500/20 text-red-400">
            <AlertCircle className="h-3 w-3" /> Error
          </Badge>
        );
      default:
        return (
          <Badge variant="secondary" className="gap-1 bg-gray-500/20 text-gray-400">
            Disconnected
          </Badge>
        );
    }
  };

  return (
    <div className="flex flex-col h-screen bg-[#1a1b26]">
      {/* Header */}
      <div className="flex items-center justify-between h-12 px-4 bg-[#16161e] border-b border-[#292e42]">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" className="h-8 w-8 text-gray-400 hover:text-white" onClick={() => window.close()}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          {activeTab === "graphics" ? <Monitor className="h-5 w-5 text-purple-400" /> : <TerminalIcon className="h-5 w-5 text-blue-400" />}
          <div className="flex items-center gap-2">
            <span className="font-medium text-gray-200">{instanceName}</span>
            <span className="text-gray-500">@</span>
            <span className="text-gray-400">{host}</span>
            <span className="text-gray-600 text-sm">({project})</span>
          </div>
          <StatusBadge />
        </div>

        {isVM && (
          <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as ConsoleType)}>
            <TabsList className="h-8 bg-[#1f2335]">
              <TabsTrigger value="graphics" className="h-6 px-3 text-xs gap-1.5">
                <Monitor className="h-3.5 w-3.5" /> Graphics
              </TabsTrigger>
              <TabsTrigger value="text" className="h-6 px-3 text-xs gap-1.5">
                <TerminalIcon className="h-3.5 w-3.5" /> Text
              </TabsTrigger>
            </TabsList>
          </Tabs>
        )}

        <div className="flex items-center gap-2">
          <TooltipProvider delayDuration={100}>
            <ConsolePageToolbar
              isVM={isVM}
              activeTab={activeTab}
              showStart={power.showStart}
              showRestartStop={power.showRestartStop}
              operationLoading={power.operationLoading}
              isOperationDisabled={power.isOperationDisabled}
              onOperation={power.requestOperation}
              showClipboard={showClipboard}
              showKeyboard={showKeyboard}
              showCursor={showCursor}
              onToggleClipboard={() => setShowClipboard((s) => !s)}
              onToggleKeyboard={() => setShowKeyboard((s) => !s)}
              onToggleCursor={() => setShowCursor((s) => !s)}
              zoom={zoom}
              onZoomIn={handleZoomIn}
              onZoomOut={handleZoomOut}
              onZoomReset={handleZoomReset}
              showSearch={showSearch}
              onToggleSearch={() => setShowSearch((s) => !s)}
              biosSpamming={biosSpamming}
              biosKeys={BIOS_KEYS}
              onBiosSpam={handleBiosSpam}
              onReconnect={handleReconnect}
            />
          </TooltipProvider>
        </div>
      </div>

      <ConsolePageDialogs
        instanceName={instanceName}
        confirmDialog={power.confirmDialog}
        onConfirmOpenChange={power.setConfirmOpen}
        onConfirm={power.confirm}
        sessionConflict={sessionConflict}
        killingSession={killingSession}
        onKillAll={async () => {
          for (const session of sessionConflict.sessions) await handleKillSession(session.operation_id);
          setSessionConflict({ show: false, sessions: [] });
        }}
        onContinueAnyway={() => setSessionConflict({ show: false, sessions: [] })}
        onCancelConflict={() => window.close()}
      />

      {/* Search bar (text mode only) */}
      {showSearch && activeTab === "text" && (
        <div className="flex items-center gap-2 px-4 py-2 bg-[#1f2335] border-b border-[#292e42]">
          <Search className="h-4 w-4 text-gray-400" />
          <input
            type="text"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="Search console output..."
            className="flex-1 bg-transparent text-sm text-gray-200 outline-none placeholder:text-gray-500"
            autoFocus
          />
          <Button variant="ghost" size="sm" className="h-7 text-xs text-gray-400 hover:text-white" onClick={handleSearch}>
            Find Next
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7 text-gray-400 hover:text-white" onClick={() => setShowSearch(false)}>
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}

      {/* Console Content */}
      <div className="flex-1 overflow-hidden relative flex flex-col">
        <div className="flex-1 overflow-hidden relative flex">
          {showClipboard && <ClipboardManager onClose={() => setShowClipboard(false)} onInsert={handleClipboardInsert} />}

          <div className="flex-1 overflow-hidden relative">
            {!sessionCheckComplete && (
              <div className="absolute inset-0 flex flex-col items-center justify-center bg-[#1a1b26] z-20">
                <Spinner className="h-8 w-8 text-blue-400 mb-4" />
                <p className="text-gray-400">Checking for active sessions...</p>
              </div>
            )}

            <div className="absolute inset-0 overflow-hidden">
              {/* Text Console */}
              {activeTab === "text" && consoleReady && (
                <div className="absolute inset-0 p-2" style={{ zIndex: 10 }} onClick={() => textRef.current?.focus()}>
                  <TextConsole
                    key={textReconnectKey}
                    ref={textRef}
                    consoleId={consoleId}
                    instanceName={instanceName}
                    host={host}
                    project={project}
                    onStatusChange={(s) => setTextStatus(s)}
                    onSessionIdChange={setSessionId}
                  />
                </div>
              )}

              {/* Graphics Console (shared SPICE engine) */}
              {isVM && activeTab === "graphics" && consoleReady && (
                <div className="absolute inset-0 bg-black" style={{ zIndex: 10 }}>
                  <GraphicsConsole
                    ref={graphicsRef}
                    consoleId={consoleId}
                    instanceName={instanceName}
                    host={host}
                    project={project}
                    zoom={zoom}
                    onStatusChange={setGraphicsStatus}
                    onReady={() => graphicsRef.current?.focus()}
                  />
                </div>
              )}
            </div>

            {showCursor && activeTab === "graphics" && (
              <OnScreenCursor screenId={screenId} isActive={showCursor} onClose={() => setShowCursor(false)} />
            )}
          </div>
        </div>

        {showKeyboard && <OnScreenKeyboard onKeyPress={handleVirtualKeyPress} onClose={() => setShowKeyboard(false)} />}

        <ConsoleResourceBar
          sessionId={sessionId || null}
          instanceName={instanceName}
          host={host}
          project={project}
          connectionStatus={currentStatus}
          protocol={activeTab === "graphics" ? "graphics" : "terminal"}
        />
      </div>
    </div>
  );
}

export default function ExpandedConsolePage() {
  return (
    <Suspense fallback={<ConsoleLoading />}>
      <ConsoleContent />
    </Suspense>
  );
}
