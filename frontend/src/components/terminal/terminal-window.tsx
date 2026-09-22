/**
 * Terminal Window - Draggable, resizable terminal popup window.
 * Uses react-rnd for drag and resize functionality.
 */

"use client";

import * as React from "react";
import { useState, useCallback, useEffect, useRef } from "react";
import { Rnd } from "react-rnd";
import {
  X,
  Minus,
  Square,
  Maximize2,
  ExternalLink,
  Copy,
  Search,
  LayoutGrid,
  Terminal as TerminalIcon,
  Server,
  Loader2,
  AlertCircle,
  CheckCircle2,
  ChevronUp,
  ChevronDown,
  Pin,
  PinOff,
  Upload,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import {
  useTerminalStore,
  useTerminalZIndex,
  TerminalInstance as TerminalInstanceType,
} from "@/lib/terminal-store";
import { TerminalInstance, TerminalInstanceHandle } from "./terminal-instance";
import { InstanceOperations } from "./instance-operations";
import { FileUploadDialog } from "./file-upload-dialog";

interface TerminalWindowProps {
  terminal: TerminalInstanceType;
}

// Status indicator component
function StatusIndicator({ status }: { status: TerminalInstanceType["status"] }) {
  switch (status) {
    case "connecting":
      return <Loader2 className="h-3 w-3 animate-spin text-yellow-400" />;
    case "connected":
      return <CheckCircle2 className="h-3 w-3 text-green-400" />;
    case "error":
      return <AlertCircle className="h-3 w-3 text-red-400" />;
    default:
      return <div className="h-2 w-2 rounded-full bg-gray-400" />;
  }
}

export function TerminalWindow({ terminal }: TerminalWindowProps) {
  const {
    closeTerminal,
    minimizeTerminal,
    focusTerminal,
    updateTerminalPosition,
    bringToFront,
    maximizeTerminal,
    restoreFromMaximize,
    arrangeAllTerminals,
    terminals,
    toggleStickyTerminal,
  } = useTerminalStore();

  const zIndex = useTerminalZIndex(terminal.id);
  const [showSearch, setShowSearch] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const terminalInstanceRef = useRef<TerminalInstanceHandle | null>(null);
  
  // File upload state
  const [showUploadDialog, setShowUploadDialog] = useState(false);
  const [droppedFiles, setDroppedFiles] = useState<File[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  
  // Track scroll offset for unpinned terminals
  const [scrollOffset, setScrollOffset] = useState({ x: 0, y: 0 });
  const basePositionRef = useRef({ x: terminal.position.x, y: terminal.position.y });
  
  // Update base position when terminal position changes (from drag/resize/maximize)
  useEffect(() => {
    if (terminal.sticky) {
      // When pinned, store current position as base
      basePositionRef.current = { x: terminal.position.x, y: terminal.position.y };
    }
  }, [terminal.position.x, terminal.position.y, terminal.sticky]);
  
  // Handle scroll for unpinned terminals
  useEffect(() => {
    if (terminal.sticky || terminal.minimized) {
      // Reset scroll offset when pinned or minimized
      setScrollOffset({ x: 0, y: 0 });
      return;
    }
    
    // Capture initial scroll position when unpinning
    const initialScrollX = window.scrollX;
    const initialScrollY = window.scrollY;
    
    const handleScroll = () => {
      // Calculate how much we've scrolled since unpinning
      const deltaX = window.scrollX - initialScrollX;
      const deltaY = window.scrollY - initialScrollY;
      setScrollOffset({ x: -deltaX, y: -deltaY });
    };
    
    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, [terminal.sticky, terminal.minimized]);

  // Handle window focus
  const handleFocus = useCallback(() => {
    bringToFront(terminal.id);
  }, [terminal.id, bringToFront]);

  // Handle close
  const handleClose = useCallback(() => {
    closeTerminal(terminal.id);
  }, [terminal.id, closeTerminal]);

  // Handle sticky toggle
  const handleToggleSticky = useCallback(() => {
    toggleStickyTerminal(terminal.id);
  }, [terminal.id, toggleStickyTerminal]);

  // Handle minimize
  const handleMinimize = useCallback(() => {
    minimizeTerminal(terminal.id);
  }, [terminal.id, minimizeTerminal]);

  // Handle maximize toggle
  const handleMaximize = useCallback(() => {
    if (terminal.maximized) {
      restoreFromMaximize(terminal.id);
    } else {
      maximizeTerminal(terminal.id);
    }
  }, [terminal.id, terminal.maximized, maximizeTerminal, restoreFromMaximize]);

  // Handle expand to new tab
  const handleExpand = useCallback(() => {
    const url = `/terminal/${terminal.id}?instance=${encodeURIComponent(terminal.instanceName)}&host=${encodeURIComponent(terminal.host)}&project=${encodeURIComponent(terminal.project)}`;
    window.open(url, "_blank");
    // Close terminal in current tab to prevent duplicate connections
    closeTerminal(terminal.id);
  }, [terminal, closeTerminal]);

  // Handle arrange all terminals in a grid layout
  const handleArrangeAll = useCallback(() => {
    arrangeAllTerminals();
  }, [arrangeAllTerminals]);

  // Handle drag/resize - also clears maximized state
  const handleDragStop = useCallback(
    (_e: any, d: { x: number; y: number }) => {
      updateTerminalPosition(terminal.id, { x: d.x, y: d.y });
      // Clear maximized state if user drags
      if (terminal.maximized) {
        restoreFromMaximize(terminal.id);
      }
    },
    [terminal.id, terminal.maximized, updateTerminalPosition, restoreFromMaximize]
  );

  const handleResizeStop = useCallback(
    (_e: any, _direction: any, ref: HTMLElement, _delta: any, position: { x: number; y: number }) => {
      updateTerminalPosition(terminal.id, {
        x: position.x,
        y: position.y,
        width: parseInt(ref.style.width),
        height: parseInt(ref.style.height),
      });
    },
    [terminal.id, updateTerminalPosition]
  );

  // Handle search
  const handleSearch = useCallback((direction: 'next' | 'prev' = 'next') => {
    if (terminalInstanceRef.current && searchTerm) {
      terminalInstanceRef.current.search(searchTerm, direction === 'next');
    }
  }, [searchTerm]);

  // Search on Enter key
  const handleSearchKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSearch(e.shiftKey ? 'prev' : 'next');
    } else if (e.key === 'Escape') {
      setShowSearch(false);
    }
  }, [handleSearch]);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!terminal.focused) return;

      // Ctrl+Shift+F for search
      if (e.ctrlKey && e.shiftKey && e.key === "F") {
        e.preventDefault();
        setShowSearch((s) => !s);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [terminal.focused]);

  // File drag-and-drop handlers for the terminal content area
  const handleFileDragOver = useCallback((e: React.DragEvent) => {
    // Only handle file drags, not text/terminal content drags
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

  // Handle upload button click
  const handleUploadClick = useCallback(() => {
    setDroppedFiles([]);
    setShowUploadDialog(true);
  }, []);

  // Render terminal window with react-rnd for drag/resize
  // Calculate actual position (base position + scroll offset for unpinned)
  const displayPosition = {
    x: terminal.position.x + scrollOffset.x,
    y: terminal.position.y + scrollOffset.y,
  };
  
  // When minimized, we still render the component but hide it visually
  // This keeps the WebSocket connection alive and terminal session running
  const isMinimized = terminal.minimized;
  
  return (
    <Rnd
      // Controlled mode - position/size from props, synced with store
      // For unpinned terminals, apply scroll offset to simulate scrolling with page
      position={displayPosition}
      size={{ width: terminal.position.width, height: terminal.position.height }}
      minWidth={400}
      minHeight={250}
      dragHandleClassName="terminal-drag-handle"
      onDragStart={handleFocus}
      onDragStop={handleDragStop}
      onResizeStart={handleFocus}
      onResizeStop={handleResizeStop}
      // Disable interaction when minimized
      disableDragging={isMinimized}
      enableResizing={isMinimized ? false : {
        top: true,
        right: true,
        bottom: true,
        left: true,
        topRight: true,
        bottomRight: true,
        bottomLeft: true,
        topLeft: true,
      }}
      style={{ 
        zIndex: isMinimized ? -1 : 99999 + zIndex,
        // Always use fixed positioning - sticky controls scroll behavior
        position: 'fixed',
        // Hide visually when minimized but keep mounted for active connection
        visibility: isMinimized ? 'hidden' : 'visible',
        pointerEvents: isMinimized ? 'none' : 'auto',
      }}
    >
      <div
        className={cn(
          "flex flex-col h-full rounded-lg overflow-hidden shadow-2xl border",
          "bg-terminal border-terminal-border",
          terminal.focused && "ring-2 ring-blue-500/50",
          terminal.sticky && "ring-2 ring-amber-500/40 border-amber-500/30"
        )}
        onClick={handleFocus}
      >
        {/* Title Bar */}
        <div className="terminal-drag-handle flex items-center justify-between h-10 px-3 bg-terminal-deep border-b border-terminal-border select-none cursor-move">
          {/* Left: Icon and Title */}
          <div className="flex items-center gap-2 min-w-0 flex-1">
            <TerminalIcon className="h-4 w-4 text-blue-400 shrink-0" />
            <StatusIndicator status={terminal.status} />
            <span className="text-sm font-medium text-gray-200 truncate">
              {terminal.title}
            </span>
            <span className="text-xs text-gray-500 truncate">
              ({terminal.project})
            </span>
          </div>

          {/* Right: Controls - stopPropagation to prevent triggering parent focus and drag on button click/touch */}
          <div 
            className="flex items-center gap-1 ml-2 touch-manipulation"
            onClick={(e) => e.stopPropagation()}
            onMouseDown={(e) => e.stopPropagation()}
            onTouchStart={(e) => e.stopPropagation()}
            onTouchEnd={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            {/* Instance Operations (Start/Restart/Stop) */}
            <InstanceOperations
              terminalId={terminal.id}
              instanceName={terminal.instanceName}
              host={terminal.host}
              project={terminal.project}
              instanceStatus={terminal.instanceStatus}
            />

            <TooltipProvider delayDuration={100}>
              {/* Arrange All Terminals - only show if more than 1 active terminal */}
              {terminals.filter(t => !t.minimized).length > 1 && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-white hover:bg-white/10 touch-manipulation active:scale-95"
                      onClick={handleArrangeAll}
                      onTouchEnd={(e) => { e.preventDefault(); handleArrangeAll(); }}
                    >
                      <LayoutGrid className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Arrange All Terminals</TooltipContent>
                </Tooltip>
              )}

              {/* Sticky Toggle */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className={cn(
                      "h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-white hover:bg-white/10 touch-manipulation active:scale-95",
                      terminal.sticky && "bg-blue-500/20 text-blue-400"
                    )}
                    onClick={handleToggleSticky}
                    onTouchEnd={(e) => { e.preventDefault(); handleToggleSticky(); }}
                  >
                    {terminal.sticky ? (
                      <PinOff className="h-4 w-4" />
                    ) : (
                      <Pin className="h-4 w-4" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  {terminal.sticky ? "Unpin Terminal" : "Pin Terminal (Stay Fixed)"}
                </TooltipContent>
              </Tooltip>

              {/* Search */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className={cn(
                      "h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-white hover:bg-white/10 touch-manipulation active:scale-95",
                      showSearch && "bg-white/10 text-white"
                    )}
                    onClick={() => setShowSearch((s) => !s)}
                    onTouchEnd={(e) => { e.preventDefault(); setShowSearch((s) => !s); }}
                  >
                    <Search className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Search (Ctrl+Shift+F)</TooltipContent>
              </Tooltip>

              {/* Upload File */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-white hover:bg-white/10 touch-manipulation active:scale-95"
                    onClick={handleUploadClick}
                    onTouchEnd={(e) => { e.preventDefault(); handleUploadClick(); }}
                  >
                    <Upload className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Upload File to Instance</TooltipContent>
              </Tooltip>

              {/* Expand to new tab */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-white hover:bg-white/10 touch-manipulation active:scale-95"
                    onClick={handleExpand}
                    onTouchEnd={(e) => { e.preventDefault(); handleExpand(); }}
                  >
                    <ExternalLink className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Open in New Tab</TooltipContent>
              </Tooltip>

              {/* Minimize */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-yellow-400 hover:bg-yellow-400/10 touch-manipulation active:scale-95"
                    onClick={handleMinimize}
                    onTouchEnd={(e) => { e.preventDefault(); handleMinimize(); }}
                  >
                    <Minus className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Minimize</TooltipContent>
              </Tooltip>

              {/* Maximize */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-green-400 hover:bg-green-400/10 touch-manipulation active:scale-95"
                    onClick={handleMaximize}
                    onTouchEnd={(e) => { e.preventDefault(); handleMaximize(); }}
                  >
                    {terminal.maximized ? (
                      <Square className="h-4 w-4" />
                    ) : (
                      <Maximize2 className="h-4 w-4" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{terminal.maximized ? "Restore" : "Maximize"}</TooltipContent>
              </Tooltip>

              {/* Close */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-red-400 hover:bg-red-400/10 touch-manipulation active:scale-95"
                    onClick={handleClose}
                    onTouchEnd={(e) => { e.preventDefault(); handleClose(); }}
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Close</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        </div>

        {/* Search Bar (conditional) */}
        {showSearch && (
          <div className="flex items-center gap-2 px-3 py-2 bg-terminal-raised border-b border-terminal-border">
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
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-gray-400 hover:text-white"
              onClick={() => handleSearch('prev')}
              title="Previous (Shift+Enter)"
            >
              <ChevronUp className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-gray-400 hover:text-white"
              onClick={() => handleSearch('next')}
              title="Next (Enter)"
            >
              <ChevronDown className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 text-gray-400 hover:text-white"
              onClick={() => setShowSearch(false)}
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}

        {/* Terminal Content with drag-and-drop overlay */}
        <div
          className="flex-1 overflow-hidden relative"
          onDragOver={handleFileDragOver}
          onDragLeave={handleFileDragLeave}
          onDrop={handleFileDrop}
        >
          <TerminalInstance
            ref={terminalInstanceRef}
            terminalId={terminal.id}
            instanceName={terminal.instanceName}
            host={terminal.host}
            project={terminal.project}
          />

          {/* Drag overlay */}
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
          instanceName={terminal.instanceName}
          host={terminal.host}
          project={terminal.project}
          initialFiles={droppedFiles}
        />
      </div>
    </Rnd>
  );
}

export default TerminalWindow;
