/**
 * Expanded Terminal Page - Full-page terminal view.
 * Accessible via /terminal/[id] route.
 */

"use client";

import * as React from "react";
import { Suspense, useEffect, useState, useRef, useCallback } from "react";
import { useParams, useSearchParams } from "next/navigation";
import {
  Terminal as TerminalIcon,
  ArrowLeft,
  Copy,
  Search,
  Settings,
  RefreshCw,
  Loader2,
  X,
  Play,
  Square,
  RotateCcw,
  AlertTriangle,
} from "lucide-react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { SearchAddon } from "@xterm/addon-search";
import { Button } from "@/components/ui/button";
import { SessionStatusBadge } from "@/components/terminal/session-status";
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
} from "@/components/ui/alert-dialog";
import { toast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";
import { useManualRefresh } from "@/hooks/use-manual-refresh";
import {
  isActorScopeChangedError,
  useInstancePowerAction,
} from "@/hooks/use-instance-power-action";
import { API_URL, getAccessToken } from "@/lib/api";
import { ConsoleResourceBar } from "@/components/console/resource-bar";
import "@xterm/xterm/css/xterm.css";
import { TERMINAL_THEME } from "@/lib/terminal-theme";

type TerminalStatus = "connecting" | "connected" | "disconnected" | "error";
type InstanceStatus = "Running" | "Stopped" | "Starting" | "Stopping" | "Restarting" | "Unknown" | "Error";
type OperationType = 'start' | 'stop' | 'restart';

// WebSocket URL builder - shared utility for proxy-safe, ticketed URLs
import { ACCESS_REVOKED_MESSAGE, WS_CLOSE, mintFailureMessage, ticketedSocketUrl } from "@/lib/ws-url";

// Loading fallback
function TerminalLoading() {
  return (
    <div className="flex items-center justify-center h-screen bg-terminal">
      <div className="flex flex-col items-center gap-4">
        <Loader2 className="h-8 w-8 animate-spin text-blue-400" />
        <span className="text-gray-400">Loading terminal...</span>
      </div>
    </div>
  );
}

// Main terminal content component
function TerminalContent() {
  const params = useParams();
  const searchParams = useSearchParams();
  
  const instanceName = searchParams.get("instance") || "";
  const host = searchParams.get("host") || "";
  const project = searchParams.get("project") || "default";
  
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const searchAddonRef = useRef<SearchAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // Bumped on every connect and on unmount, so a ticket that arrives late opens nothing.
  const connectGenerationRef = useRef(0);
  
  const [status, setStatus] = useState<TerminalStatus>("connecting");
  const [instanceStatus, setInstanceStatus] = useState<InstanceStatus>("Running");
  const [sessionId, setSessionId] = useState<string>("");
  const [showSearch, setShowSearch] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [error, setError] = useState<string>("");
  const [operationLoading, setOperationLoading] = useState<OperationType | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    operation: OperationType | null;
    title: string;
    description: string;
  }>({ open: false, operation: null, title: "", description: "" });
  const executePowerAction = useInstancePowerAction();

  // Initialize terminal
  const initTerminal = useCallback(() => {
    if (!terminalRef.current || xtermRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "block",
      fontSize: 15,
      fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", Consolas, monospace',
      theme: TERMINAL_THEME,
      allowProposedApi: true,
      scrollback: 50000,
      convertEol: true,
    });

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon();
    const searchAddon = new SearchAddon();

    term.loadAddon(fitAddon);
    term.loadAddon(webLinksAddon);
    term.loadAddon(searchAddon);

    term.open(terminalRef.current);
    fitAddon.fit();

    xtermRef.current = term;
    fitAddonRef.current = fitAddon;
    searchAddonRef.current = searchAddon;

    // Handle user input
    term.onData((data) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "input", data }));
      }
    });

    // Handle resize
    term.onResize(({ cols, rows }) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "resize", width: cols, height: rows }));
      }
    });

    term.writeln("\x1b[1;34m╔═══════════════════════════════════════════════════════════════╗\x1b[0m");
    term.writeln("\x1b[1;34m║\x1b[0m  \x1b[1;36mOrcastra Terminal - Full View\x1b[0m                               \x1b[1;34m║\x1b[0m");
    term.writeln("\x1b[1;34m║\x1b[0m  Connecting to \x1b[1;33m" + instanceName.padEnd(20) + "\x1b[0m                         \x1b[1;34m║\x1b[0m");
    term.writeln("\x1b[1;34m╚═══════════════════════════════════════════════════════════════╝\x1b[0m");
    term.writeln("");

    return term;
  }, [instanceName]);

  // Connect WebSocket, after minting its single-use ticket
  const connect = useCallback(async (): Promise<void> => {
    if (!instanceName || !host) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const generation = ++connectGenerationRef.current;
    setStatus("connecting");
    let wsUrl: string;
    try {
      wsUrl = await ticketedSocketUrl({ kind: "terminal", host, project, instance: instanceName });
    } catch (mintError) {
      if (generation !== connectGenerationRef.current) return;
      const message = mintFailureMessage(mintError);
      xtermRef.current?.writeln(`\x1b[1;31m✗ ${message}\x1b[0m`);
      setStatus("error");
      setError(message);
      return;
    }
    if (generation !== connectGenerationRef.current) return;

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      if (xtermRef.current) {
        const { cols, rows } = xtermRef.current;
        ws.send(JSON.stringify({ width: cols, height: rows }));
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);

        switch (msg.type) {
          case "session":
            setSessionId(msg.session_id);
            xtermRef.current?.writeln(`\x1b[1;32m✓ Session: ${msg.session_id}\x1b[0m`);
            break;

          case "ready":
            setStatus("connected");
            xtermRef.current?.writeln("\x1b[1;32m✓ Terminal ready\x1b[0m\n");
            break;

          case "output":
            if (msg.encoding === "base64") {
              const decoded = atob(msg.data);
              xtermRef.current?.write(decoded);
            } else {
              xtermRef.current?.write(msg.data);
            }
            break;

          case "error":
            xtermRef.current?.writeln(`\x1b[1;31m✗ Error: ${msg.message}\x1b[0m`);
            setStatus("error");
            setError(msg.message);
            break;

          case "closed":
            xtermRef.current?.writeln("\n\x1b[1;33m⚠ Connection closed\x1b[0m");
            setStatus("disconnected");
            break;
        }
      } catch (e) {
        xtermRef.current?.write(event.data);
      }
    };

    ws.onerror = () => {
      xtermRef.current?.writeln("\x1b[1;31m✗ Connection error\x1b[0m");
      setStatus("error");
    };

    ws.onclose = (event) => {
      if (event.code === WS_CLOSE.ACCESS_REVOKED) {
        xtermRef.current?.writeln(`\n\x1b[1;31m✗ ${ACCESS_REVOKED_MESSAGE}\x1b[0m`);
        setStatus("error");
        setError(ACCESS_REVOKED_MESSAGE);
        return;
      }
      setStatus("disconnected");
    };
  }, [instanceName, host, project]);

  // Handle resize
  const handleResize = useCallback(() => {
    if (fitAddonRef.current) {
      try {
        fitAddonRef.current.fit();
      } catch (e) {
        console.warn("[Terminal] Fit error:", e);
      }
    }
  }, []);

  // Search
  const handleSearch = useCallback(() => {
    if (searchAddonRef.current && searchTerm) {
      searchAddonRef.current.findNext(searchTerm);
    }
  }, [searchTerm]);

  // Reconnect
  const handleReconnect = useCallback(async (): Promise<void> => {
    if (wsRef.current) {
      wsRef.current.close();
    }
    if (xtermRef.current) {
      xtermRef.current.clear();
    }
    await connect();
  }, [connect]);

  // important: guard the reconnect. It closes the socket and opens a new one, so
  // a rapid double-click tore the session down twice and raced the rebuild. The
  // guard also gives the icon-only button the pending state it never had.
  const { pending: reconnectPending, run: runReconnect } = useManualRefresh(
    handleReconnect,
    { errorTitle: "Reconnect failed" },
  );

  // Execute instance operation
  const executeOperation = useCallback(async (operation: OperationType) => {
    setOperationLoading(operation);
    
    const transitionStatus: Record<OperationType, InstanceStatus> = {
      start: 'Starting',
      stop: 'Stopping',
      restart: 'Restarting',
    };
    setInstanceStatus(transitionStatus[operation]);

    try {
      const result = await executePowerAction(operation, {
        name: instanceName,
        host,
        project,
        force: false,
        timeout: 30,
      });
      
      const statusMap: Record<string, InstanceStatus> = {
        'running': 'Running',
        'stopped': 'Stopped',
        'Running': 'Running',
        'Stopped': 'Stopped',
      };
      const newStatus = result.new_status ? statusMap[result.new_status] || 'Unknown' : 'Unknown';
      setInstanceStatus(newStatus);
      
      toast({
        title: `Instance ${operation}ed successfully`,
        description: `${instanceName} is now ${newStatus.toLowerCase()}`,
      });
      
      // Reconnect if restarted
      if (operation === 'restart') {
        setTimeout(() => void handleReconnect(), 2000);
      }
    } catch (error) {
      if (isActorScopeChangedError(error)) return;
      setInstanceStatus('Error');
      toast({
        title: `Failed to ${operation} instance`,
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setOperationLoading(null);
    }
  }, [executePowerAction, handleReconnect, host, instanceName, project]);

  // Request operation with confirmation
  const requestOperation = useCallback((operation: OperationType) => {
    if (operation === 'start') {
      executeOperation(operation);
      return;
    }

    const dialogConfig: Record<OperationType, { title: string; description: string }> = {
      start: { title: '', description: '' },
      stop: {
        title: `Stop ${instanceName}?`,
        description: `This will gracefully stop the instance. The terminal connection will be lost.`,
      },
      restart: {
        title: `Restart ${instanceName}?`,
        description: `This will restart the instance. The terminal will attempt to reconnect after restart.`,
      },
    };

    setConfirmDialog({
      open: true,
      operation,
      ...dialogConfig[operation],
    });
  }, [instanceName, executeOperation]);

  const handleConfirm = useCallback(() => {
    if (confirmDialog.operation) {
      executeOperation(confirmDialog.operation);
    }
    setConfirmDialog(prev => ({ ...prev, open: false }));
  }, [confirmDialog.operation, executeOperation]);

  const isRunning = instanceStatus === 'Running';
  const isStopped = instanceStatus === 'Stopped';
  const isTransitioning = ['Starting', 'Stopping', 'Restarting'].includes(instanceStatus);
  const showStart = isStopped || instanceStatus === 'Unknown' || instanceStatus === 'Error';
  const showRestartStop = isRunning || instanceStatus === 'Unknown' || instanceStatus === 'Error';
  const isOperationDisabled = isTransitioning || operationLoading !== null;

  // Fetch initial instance status from capabilities API
  // This ensures Start/Stop buttons have correct initial state
  useEffect(() => {
    if (!instanceName || !host) return;
    
    const fetchInstanceStatus = async () => {
      try {
        const response = await fetch(
          `${API_URL}/console/${encodeURIComponent(instanceName)}/capabilities` +
            `?host=${encodeURIComponent(host)}` +
            `&project=${encodeURIComponent(project)}`,
          { headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
        );
        
        if (response.ok) {
          const data = await response.json();
          const state = data.state ?? 'Unknown';
          if (state && state !== 'Unknown') {
            setInstanceStatus(state as InstanceStatus);
          }
        }
      } catch (err) {
        console.warn("[Terminal] Failed to fetch instance status:", err);
        // Keep default status on error
      }
    };
    
    fetchInstanceStatus();
  }, [instanceName, host, project]);

  // Initialize
  useEffect(() => {
    initTerminal();
    void connect();

    window.addEventListener("resize", handleResize);

    const heartbeat = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "ping" }));
      }
    }, 30000);

    return () => {
      window.removeEventListener("resize", handleResize);
      clearInterval(heartbeat);
      connectGenerationRef.current += 1;
      if (wsRef.current) {
        wsRef.current.close();
      }
      if (xtermRef.current) {
        xtermRef.current.dispose();
      }
    };
  }, [initTerminal, connect, handleResize]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key === "F") {
        e.preventDefault();
        setShowSearch((s) => !s);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  return (
    <div className="flex flex-col h-screen bg-terminal">
      {/* Header */}
      <div className="flex items-center justify-between h-12 px-4 bg-terminal-deep border-b border-terminal-border">
        {/* Left */}
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 text-gray-400 hover:text-white"
            aria-label="Close this terminal window"
            title="Close this terminal window"
            onClick={() => window.close()}
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </Button>
          <TerminalIcon className="h-5 w-5 text-blue-400" />
          <div className="flex items-center gap-2">
            <span className="font-medium text-gray-200">{instanceName}</span>
            <span className="text-gray-500">@</span>
            <span className="text-gray-400">{host}</span>
            <span className="text-gray-600 text-sm">({project})</span>
          </div>
          <SessionStatusBadge status={status} />
        </div>

        {/* Right */}
        <div className="flex items-center gap-2">
          <TooltipProvider delayDuration={100}>
            {/* Instance Operations */}
            <div className="flex items-center gap-1 mr-2 border-r border-gray-600 pr-2">
              {showStart && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className={cn(
                        "h-8 w-8 terminal-status text-gray-400 hover:text-status-online hover:bg-terminal-raised",
                        operationLoading === 'start' && "text-status-online"
                      )}
                      onClick={() => requestOperation('start')}
                      disabled={isOperationDisabled}
                    >
                      {operationLoading === 'start' ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Play className="h-4 w-4" />
                      )}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Start Instance</TooltipContent>
                </Tooltip>
              )}

              {showRestartStop && (
                <>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className={cn(
                          "h-8 w-8 text-gray-400 hover:text-gray-100 hover:bg-terminal-raised",
                          operationLoading === 'restart' && "text-gray-100"
                        )}
                        onClick={() => requestOperation('restart')}
                        disabled={isOperationDisabled}
                      >
                        {operationLoading === 'restart' ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <RotateCcw className="h-4 w-4" />
                        )}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Restart Instance</TooltipContent>
                  </Tooltip>

                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className={cn(
                          "h-8 w-8 terminal-status text-gray-400 hover:text-status-offline hover:bg-terminal-raised",
                          operationLoading === 'stop' && "text-status-offline"
                        )}
                        onClick={() => requestOperation('stop')}
                        disabled={isOperationDisabled}
                      >
                        {operationLoading === 'stop' ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Square className="h-4 w-4" />
                        )}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Stop Instance</TooltipContent>
                  </Tooltip>
                </>
              )}
            </div>

            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn(
                    "h-8 w-8 text-gray-400 hover:text-white",
                    showSearch && "bg-white/10 text-white"
                  )}
                  onClick={() => setShowSearch((s) => !s)}
                >
                  <Search className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Search (Ctrl+Shift+F)</TooltipContent>
            </Tooltip>

            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  aria-busy={reconnectPending}
                  aria-label="Reconnect"
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-gray-400 hover:text-white"
                  disabled={reconnectPending}
                  onClick={() => void runReconnect()}
                >
                  <RefreshCw
                    className={cn(
                      "h-4 w-4",
                      reconnectPending && "animate-spin motion-reduce:animate-none",
                    )}
                  />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reconnect</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      {/* Confirmation Dialog */}
      <AlertDialog open={confirmDialog.open} onOpenChange={(open) => setConfirmDialog(prev => ({ ...prev, open }))}>
        <AlertDialogContent className="bg-terminal border-terminal-border">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-gray-200">
              {confirmDialog.title}
            </AlertDialogTitle>
            <AlertDialogDescription className="text-gray-400">
              {confirmDialog.description}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="bg-transparent border-gray-600 text-gray-300 hover:bg-gray-800">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirm}
              className={cn(
                confirmDialog.operation === 'stop' && "bg-destructive text-destructive-foreground hover:bg-destructive/90"
              )}
            >
              {confirmDialog.operation === 'stop' ? 'Stop' : 'Restart'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Search bar */}
      {showSearch && (
        <div className="flex items-center gap-2 px-4 py-2 bg-terminal-raised border-b border-terminal-border">
          <Search className="h-4 w-4 text-gray-400" />
          <input
            type="text"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="Search terminal output..."
            className="flex-1 bg-transparent text-sm text-gray-200 outline-none placeholder:text-gray-500"
            autoFocus
          />
          <Button
            variant="ghost"
            size="sm"
            className="h-7 text-xs text-gray-400 hover:text-white"
            onClick={handleSearch}
          >
            Find Next
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-gray-400 hover:text-white"
            onClick={() => setShowSearch(false)}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}

      {/* Terminal */}
      <div
        ref={terminalRef}
        className="flex-1 p-2 overflow-hidden"
        onClick={() => xtermRef.current?.focus()}
      />

      {/* Session Intelligence & Resource Display Bar */}
      <ConsoleResourceBar
        sessionId={sessionId || null}
        instanceName={instanceName}
        host={host}
        project={project}
        connectionStatus={status}
        protocol="terminal"
      />
    </div>
  );
}

// Export with Suspense wrapper for useSearchParams
export default function ExpandedTerminalPage() {
  return (
    <Suspense fallback={<TerminalLoading />}>
      <TerminalContent />
    </Suspense>
  );
}
