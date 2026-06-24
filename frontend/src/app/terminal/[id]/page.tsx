/**
 * Expanded Terminal Page - Full-page terminal view.
 * Accessible via /terminal/[id] route.
 *
 * Renders the same shared <TerminalInstance> used by the windowed terminal, so the xterm
 * setup, dynamic-width banner, ticket auth, auto-reconnect and resize behaviour are identical
 * (no divergent reimplementation). This page only adds the full-page chrome + toolbar.
 */

"use client";

import * as React from "react";
import { Suspense, useEffect, useState, useRef, useCallback } from "react";
import { useParams, useSearchParams } from "next/navigation";
import {
  Terminal as TerminalIcon,
  ArrowLeft,
  Search,
  RefreshCw,
  CheckCircle2,
  AlertCircle,
  X,
  Play,
  Square,
  RotateCcw,
  Upload,
  ChevronUp,
  ChevronDown,
} from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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
import { API_URL, getAccessToken } from "@/lib/api";
import { ConsoleResourceBar } from "@/components/console/resource-bar";
import { TerminalInstance, TerminalInstanceHandle } from "@/components/terminal/terminal-instance";
import { FileUploadDialog } from "@/components/terminal/file-upload-dialog";

type TerminalStatus = "connecting" | "connected" | "disconnected" | "error";
type InstanceStatus = "Running" | "Stopped" | "Starting" | "Stopping" | "Restarting" | "Unknown" | "Error";
type OperationType = "start" | "stop" | "restart";

// Loading fallback
function TerminalLoading() {
  return (
    <div className="flex items-center justify-center h-screen bg-[#1a1b26]">
      <div className="flex flex-col items-center gap-4">
        <Spinner className="h-8 w-8 text-blue-400" />
        <span className="text-gray-400">Loading terminal...</span>
      </div>
    </div>
  );
}

function TerminalContent() {
  const params = useParams();
  const searchParams = useSearchParams();

  const terminalId = (params?.id as string) || "";
  const instanceName = searchParams.get("instance") || "";
  const host = searchParams.get("host") || "";
  const project = searchParams.get("project") || "default";

  const termRef = useRef<TerminalInstanceHandle | null>(null);

  const [status, setStatus] = useState<TerminalStatus>("connecting");
  const [sessionId, setSessionId] = useState<string>("");
  const [instanceStatus, setInstanceStatus] = useState<InstanceStatus>("Running");
  const [showSearch, setShowSearch] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [operationLoading, setOperationLoading] = useState<OperationType | null>(null);
  // Bumping this remounts TerminalInstance -> a clean reconnect with a fresh ticket
  // (used by the Reconnect button and after a restart).
  const [connKey, setConnKey] = useState(0);
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    operation: OperationType | null;
    title: string;
    description: string;
  }>({ open: false, operation: null, title: "", description: "" });

  // File upload
  const [showUploadDialog, setShowUploadDialog] = useState(false);
  const [droppedFiles, setDroppedFiles] = useState<File[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);

  const handleSearch = useCallback((direction: "next" | "prev" = "next") => {
    if (termRef.current && searchTerm) {
      termRef.current.search(searchTerm, direction === "next");
    }
  }, [searchTerm]);

  const handleReconnect = useCallback(() => {
    setStatus("connecting");
    setConnKey((k) => k + 1);
  }, []);

  // Instance start/stop/restart via the instances API.
  const executeOperation = useCallback(async (operation: OperationType) => {
    setOperationLoading(operation);
    const transitionStatus: Record<OperationType, InstanceStatus> = {
      start: "Starting",
      stop: "Stopping",
      restart: "Restarting",
    };
    setInstanceStatus(transitionStatus[operation]);

    try {
      const response = await fetch(`${API_URL}/instances/${encodeURIComponent(instanceName)}/${operation}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) },
        body: JSON.stringify({ host, force: false, timeout: 30 }),
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({ detail: "Unknown error" }));
        throw new Error(err.detail || `Failed to ${operation} instance`);
      }
      const result = await response.json();
      const statusMap: Record<string, InstanceStatus> = {
        running: "Running", stopped: "Stopped", Running: "Running", Stopped: "Stopped",
      };
      const newStatus = statusMap[result.new_status] || "Unknown";
      setInstanceStatus(newStatus);
      toast({
        title: `Instance ${operation}ed successfully`,
        description: `${instanceName} is now ${newStatus.toLowerCase()}`,
        variant: "success",
      });
      // A restart drops the shell session; remount to reconnect once it is back up.
      if (operation === "restart") {
        setTimeout(() => handleReconnect(), 2000);
      }
    } catch (error) {
      setInstanceStatus("Error");
      toast({
        title: `Failed to ${operation} instance`,
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setOperationLoading(null);
    }
  }, [instanceName, host, handleReconnect]);

  const requestOperation = useCallback((operation: OperationType) => {
    if (operation === "start") {
      executeOperation(operation);
      return;
    }
    const dialogConfig: Record<OperationType, { title: string; description: string }> = {
      start: { title: "", description: "" },
      stop: {
        title: `Stop ${instanceName}?`,
        description: "This will gracefully stop the instance. The terminal connection will be lost.",
      },
      restart: {
        title: `Restart ${instanceName}?`,
        description: "This will restart the instance. The terminal will reconnect after restart.",
      },
    };
    setConfirmDialog({ open: true, operation, ...dialogConfig[operation] });
  }, [instanceName, executeOperation]);

  const handleConfirm = useCallback(() => {
    if (confirmDialog.operation) {
      executeOperation(confirmDialog.operation);
    }
    setConfirmDialog((prev) => ({ ...prev, open: false }));
  }, [confirmDialog.operation, executeOperation]);

  const isRunning = instanceStatus === "Running";
  const isStopped = instanceStatus === "Stopped";
  const isTransitioning = ["Starting", "Stopping", "Restarting"].includes(instanceStatus);
  const showStart = isStopped || instanceStatus === "Unknown" || instanceStatus === "Error";
  const showRestartStop = isRunning || instanceStatus === "Unknown" || instanceStatus === "Error";
  const isOperationDisabled = isTransitioning || operationLoading !== null;

  // Initial instance status from the capabilities API (so Start/Stop start in the right state).
  useEffect(() => {
    if (!instanceName || !host) return;
    (async () => {
      try {
        const response = await fetch(
          `${API_URL}/console/${encodeURIComponent(instanceName)}/capabilities` +
            `?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`,
          { headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
        );
        if (response.ok) {
          const data = await response.json();
          const state = data.state ?? "Unknown";
          if (state && state !== "Unknown") setInstanceStatus(state as InstanceStatus);
        }
      } catch {
        // keep default on error
      }
    })();
  }, [instanceName, host, project]);

  // Search keyboard shortcut
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

  const handleSearchKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleSearch(e.shiftKey ? "prev" : "next");
    } else if (e.key === "Escape") {
      setShowSearch(false);
    }
  }, [handleSearch]);

  // File drag-and-drop
  const handleFileDragOver = useCallback((e: React.DragEvent) => {
    if (e.dataTransfer.types.includes("Files")) {
      e.preventDefault();
      e.stopPropagation();
      setIsDragOver(true);
    }
  }, []);
  const handleFileDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
  }, []);
  const handleFileDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
    if (e.dataTransfer.files.length > 0) {
      setDroppedFiles(Array.from(e.dataTransfer.files));
      setShowUploadDialog(true);
    }
  }, []);
  const handleUploadClick = useCallback(() => {
    setDroppedFiles([]);
    setShowUploadDialog(true);
  }, []);

  const StatusBadge = () => {
    switch (status) {
      case "connecting":
        return (
          <Badge variant="secondary" className="gap-1 bg-yellow-500/20 text-yellow-400">
            <Spinner className="h-3 w-3" />
            Connecting
          </Badge>
        );
      case "connected":
        return (
          <Badge variant="secondary" className="gap-1 bg-green-500/20 text-green-400">
            <CheckCircle2 className="h-3 w-3" />
            Connected
          </Badge>
        );
      case "error":
        return (
          <Badge variant="secondary" className="gap-1 bg-red-500/20 text-red-400">
            <AlertCircle className="h-3 w-3" />
            Error
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
        <div className="flex items-center gap-3 min-w-0">
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 text-gray-400 hover:text-white shrink-0"
            onClick={() => window.close()}
            title="Close tab"
          >
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <TerminalIcon className="h-5 w-5 text-blue-400 shrink-0" />
          <div className="flex items-center gap-2 min-w-0">
            <span className="font-medium text-gray-200 truncate">{instanceName}</span>
            <span className="text-gray-500">@</span>
            <span className="text-gray-400 truncate">{host}</span>
            <span className="text-gray-600 text-sm">({project})</span>
          </div>
          <StatusBadge />
        </div>

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
                      className={cn("h-8 w-8 text-gray-400 hover:text-green-400 hover:bg-green-400/10", operationLoading === "start" && "text-green-400")}
                      onClick={() => requestOperation("start")}
                      disabled={isOperationDisabled}
                    >
                      {operationLoading === "start" ? <Spinner className="h-4 w-4" /> : <Play className="h-4 w-4" />}
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
                        className={cn("h-8 w-8 text-gray-400 hover:text-blue-400 hover:bg-blue-400/10", operationLoading === "restart" && "text-blue-400")}
                        onClick={() => requestOperation("restart")}
                        disabled={isOperationDisabled}
                      >
                        {operationLoading === "restart" ? <Spinner className="h-4 w-4" /> : <RotateCcw className="h-4 w-4" />}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Restart Instance</TooltipContent>
                  </Tooltip>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className={cn("h-8 w-8 text-gray-400 hover:text-red-400 hover:bg-red-400/10", operationLoading === "stop" && "text-red-400")}
                        onClick={() => requestOperation("stop")}
                        disabled={isOperationDisabled}
                      >
                        {operationLoading === "stop" ? <Spinner className="h-4 w-4" /> : <Square className="h-4 w-4" />}
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Stop Instance</TooltipContent>
                  </Tooltip>
                </>
              )}
            </div>

            {/* Search */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className={cn("h-8 w-8 text-gray-400 hover:text-white", showSearch && "bg-white/10 text-white")}
                  onClick={() => setShowSearch((s) => !s)}
                >
                  <Search className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Search (Ctrl+Shift+F)</TooltipContent>
            </Tooltip>

            {/* Upload */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-gray-400 hover:text-white"
                  onClick={handleUploadClick}
                >
                  <Upload className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Upload File to Instance</TooltipContent>
            </Tooltip>

            {/* Reconnect */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-gray-400 hover:text-white"
                  onClick={handleReconnect}
                >
                  <RefreshCw className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reconnect</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      {/* Confirmation Dialog */}
      <AlertDialog open={confirmDialog.open} onOpenChange={(open) => setConfirmDialog((prev) => ({ ...prev, open }))}>
        <AlertDialogContent className="bg-[#1a1b26] border-[#292e42]">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-gray-200">{confirmDialog.title}</AlertDialogTitle>
            <AlertDialogDescription className="text-gray-400">{confirmDialog.description}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="bg-transparent border-gray-600 text-gray-300 hover:bg-gray-800">Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirm}
              className={cn(
                confirmDialog.operation === "stop" && "bg-red-600 hover:bg-red-700",
                confirmDialog.operation === "restart" && "bg-blue-600 hover:bg-blue-700"
              )}
            >
              {confirmDialog.operation === "stop" ? "Stop" : "Restart"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Search bar */}
      {showSearch && (
        <div className="flex items-center gap-2 px-4 py-2 bg-[#1f2335] border-b border-[#292e42]">
          <Search className="h-4 w-4 text-gray-400" />
          <input
            type="text"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            onKeyDown={handleSearchKeyDown}
            placeholder="Search... (Enter to find, Shift+Enter for previous)"
            className="flex-1 bg-transparent text-sm text-gray-200 outline-none placeholder:text-gray-500"
            autoFocus
          />
          <Button variant="ghost" size="icon" className="h-7 w-7 text-gray-400 hover:text-white" onClick={() => handleSearch("prev")} title="Previous (Shift+Enter)">
            <ChevronUp className="h-3.5 w-3.5" />
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7 text-gray-400 hover:text-white" onClick={() => handleSearch("next")} title="Next (Enter)">
            <ChevronDown className="h-3.5 w-3.5" />
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7 text-gray-400 hover:text-white" onClick={() => setShowSearch(false)}>
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}

      {/* Terminal (shared component) with drag-and-drop upload overlay */}
      <div
        className="flex-1 p-2 overflow-hidden relative"
        onDragOver={handleFileDragOver}
        onDragLeave={handleFileDragLeave}
        onDrop={handleFileDrop}
      >
        <TerminalInstance
          key={connKey}
          ref={termRef}
          terminalId={terminalId}
          instanceName={instanceName}
          host={host}
          project={project}
          onReady={() => setStatus("connected")}
          onError={() => setStatus("error")}
          onClose={() => setStatus("disconnected")}
          onSessionId={setSessionId}
        />
        {isDragOver && (
          <div className="absolute inset-0 z-50 flex items-center justify-center bg-blue-500/20 border-2 border-dashed border-blue-400 rounded pointer-events-none">
            <div className="flex flex-col items-center gap-2 text-blue-300">
              <Upload className="h-10 w-10" />
              <span className="text-sm font-medium">Drop files to upload</span>
            </div>
          </div>
        )}
      </div>

      {/* File Upload Dialog */}
      <FileUploadDialog
        open={showUploadDialog}
        onOpenChange={setShowUploadDialog}
        instanceName={instanceName}
        host={host}
        project={project}
        initialFiles={droppedFiles}
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

export default function ExpandedTerminalPage() {
  return (
    <Suspense fallback={<TerminalLoading />}>
      <TerminalContent />
    </Suspense>
  );
}
