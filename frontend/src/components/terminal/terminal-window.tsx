/** A draggable, resizable floating terminal window, built on react-rnd. */

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
  ChevronUp,
  ChevronDown,
  Pin,
  PinOff,
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
import { GuestPanelOverlay, GuestPanelToggles, useGuestPanelAccess, type GuestPanel } from "./guest-panels";
import { SessionStatusIcon } from "./session-status";

interface TerminalWindowProps {
  terminal: TerminalInstanceType;
}

export function TerminalWindow({ terminal }: TerminalWindowProps) {
  // Per-field selectors, so another terminal's status or position does not re-render this
  // window (and its xterm).
  const closeTerminal = useTerminalStore((s) => s.closeTerminal);
  const minimizeTerminal = useTerminalStore((s) => s.minimizeTerminal);
  const focusTerminal = useTerminalStore((s) => s.focusTerminal);
  const updateTerminalPosition = useTerminalStore((s) => s.updateTerminalPosition);
  const bringToFront = useTerminalStore((s) => s.bringToFront);
  const maximizeTerminal = useTerminalStore((s) => s.maximizeTerminal);
  const restoreFromMaximize = useTerminalStore((s) => s.restoreFromMaximize);
  const arrangeAllTerminals = useTerminalStore((s) => s.arrangeAllTerminals);
  const toggleStickyTerminal = useTerminalStore((s) => s.toggleStickyTerminal);
  const visibleTerminalCount = useTerminalStore((s) => s.terminals.filter((t) => !t.minimized).length);

  const zIndex = useTerminalZIndex(terminal.id);
  const [showSearch, setShowSearch] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const terminalInstanceRef = useRef<TerminalInstanceHandle | null>(null);
  
  // The file explorer or the Credentials panel, drawn over the terminal; one at a time.
  const [panel, setPanel] = useState<GuestPanel>(null);
  const panelAccess = useGuestPanelAccess(terminal);
  const canUseFiles = panelAccess.files;
  
  // Track scroll offset for unpinned terminals
  const [scrollOffset, setScrollOffset] = useState({ x: 0, y: 0 });
  const basePositionRef = useRef({ x: terminal.position.x, y: terminal.position.y });
  
  useEffect(() => {
    if (terminal.sticky) {
      // A pinned window records its position as the base.
      basePositionRef.current = { x: terminal.position.x, y: terminal.position.y };
    }
  }, [terminal.position.x, terminal.position.y, terminal.sticky]);
  
  // An unpinned window follows page scroll; pinning or minimizing resets the offset.
  useEffect(() => {
    if (terminal.sticky || terminal.minimized) {
      setScrollOffset({ x: 0, y: 0 });
      return;
    }
    
    // Capture initial scroll position when unpinning
    const initialScrollX = window.scrollX;
    const initialScrollY = window.scrollY;
    
    const handleScroll = () => {
      const deltaX = window.scrollX - initialScrollX;
      const deltaY = window.scrollY - initialScrollY;
      setScrollOffset({ x: -deltaX, y: -deltaY });
    };
    
    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, [terminal.sticky, terminal.minimized]);

  const handleFocus = useCallback(() => {
    bringToFront(terminal.id);
  }, [terminal.id, bringToFront]);

  const handleClose = useCallback(() => {
    closeTerminal(terminal.id);
  }, [terminal.id, closeTerminal]);

  const handleToggleSticky = useCallback(() => {
    toggleStickyTerminal(terminal.id);
  }, [terminal.id, toggleStickyTerminal]);

  const handleMinimize = useCallback(() => {
    minimizeTerminal(terminal.id);
  }, [terminal.id, minimizeTerminal]);

  const handleMaximize = useCallback(() => {
    if (terminal.maximized) {
      restoreFromMaximize(terminal.id);
    } else {
      maximizeTerminal(terminal.id);
    }
  }, [terminal.id, terminal.maximized, maximizeTerminal, restoreFromMaximize]);

  const handleExpand = useCallback(() => {
    const url = `/terminal/${terminal.id}?instance=${encodeURIComponent(terminal.instanceName)}&host=${encodeURIComponent(terminal.host)}&project=${encodeURIComponent(terminal.project)}`;
    window.open(url, "_blank");
    // Close terminal in current tab to prevent duplicate connections
    closeTerminal(terminal.id);
  }, [terminal, closeTerminal]);

  const handleArrangeAll = useCallback(() => {
    arrangeAllTerminals();
  }, [arrangeAllTerminals]);

  const handleDragStop = useCallback(
    (_e: any, d: { x: number; y: number }) => {
      updateTerminalPosition(terminal.id, { x: d.x, y: d.y });
      // Dragging a maximized window restores it.
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

  const handleSearch = useCallback((direction: 'next' | 'prev' = 'next') => {
    if (terminalInstanceRef.current && searchTerm) {
      terminalInstanceRef.current.search(searchTerm, direction === 'next');
    }
  }, [searchTerm]);

  const handleSearchKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSearch(e.shiftKey ? 'prev' : 'next');
    } else if (e.key === 'Escape') {
      setShowSearch(false);
    }
  }, [handleSearch]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!terminal.focused) return;

      if (e.ctrlKey && e.shiftKey && e.key === "F") {
        e.preventDefault();
        setShowSearch((s) => !s);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [terminal.focused]);

  // A file dragged over the terminal opens the explorer, whose own drop handling takes it from here:
  // the person sees which folder it lands in, which a bare drop on a terminal could not say.
  const handleFileDragOver = useCallback((e: React.DragEvent) => {
    if (canUseFiles && e.dataTransfer.types.includes("Files")) {
      e.preventDefault();
      setPanel("files");
    }
  }, [canUseFiles]);

  // An unpinned window moves with the page: its stored position plus the scroll offset.
  const displayPosition = {
    x: terminal.position.x + scrollOffset.x,
    y: terminal.position.y + scrollOffset.y,
  };
  
  // A minimized window stays mounted and is only hidden, so its WebSocket and session stay alive.
  const isMinimized = terminal.minimized;
  
  return (
    <Rnd
      // Controlled by the store; an unpinned window adds the scroll offset.
      position={displayPosition}
      size={{ width: terminal.position.width, height: terminal.position.height }}
      // A phone is narrower than 400 px; the store clamps the window to the viewport, so must this.
      minWidth={typeof window !== "undefined" ? Math.min(400, window.innerWidth - 20) : 400}
      minHeight={250}
      dragHandleClassName="terminal-drag-handle"
      onDragStart={handleFocus}
      onDragStop={handleDragStop}
      onResizeStart={handleFocus}
      onResizeStop={handleResizeStop}
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
        // Always fixed; `sticky` only decides whether the scroll offset applies.
        position: 'fixed',
        // Hidden while minimized but kept mounted, so the connection stays open.
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
        <div className="terminal-drag-handle flex items-center justify-between h-10 px-3 bg-terminal-deep border-b border-terminal-border select-none cursor-move">
          <div className="flex items-center gap-2 min-w-0 flex-1">
            <TerminalIcon className="h-4 w-4 text-blue-400 shrink-0" />
            <SessionStatusIcon status={terminal.status} />
            <span className="text-sm font-medium text-gray-200 truncate">
              {terminal.title}
            </span>
            <span className="text-xs text-gray-500 truncate">
              ({terminal.project})
            </span>
          </div>

          {/* A click or touch on the controls must not focus or drag the window. */}
          <div 
            className="flex items-center gap-1 ml-2 touch-manipulation"
            onClick={(e) => e.stopPropagation()}
            onMouseDown={(e) => e.stopPropagation()}
            onTouchStart={(e) => e.stopPropagation()}
            onTouchEnd={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <InstanceOperations
              terminalId={terminal.id}
              instanceName={terminal.instanceName}
              host={terminal.host}
              project={terminal.project}
              instanceStatus={terminal.instanceStatus}
            />

            <TooltipProvider delayDuration={100}>
              {visibleTerminalCount > 1 && (
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

              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className={cn(
                      "h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-white hover:bg-white/10 touch-manipulation active:scale-95",
                      terminal.sticky && "bg-terminal-border text-gray-100"
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

              <GuestPanelToggles panel={panel} onPanel={setPanel} access={panelAccess} />

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

              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 min-h-[32px] min-w-[32px] terminal-status text-gray-400 hover:text-status-pending hover:bg-terminal-raised touch-manipulation active:scale-95"
                    onClick={handleMinimize}
                    onTouchEnd={(e) => { e.preventDefault(); handleMinimize(); }}
                  >
                    <Minus className="h-4 w-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Minimize</TooltipContent>
              </Tooltip>

              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 min-h-[32px] min-w-[32px] terminal-status text-gray-400 hover:text-status-online hover:bg-terminal-raised touch-manipulation active:scale-95"
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

              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 min-h-[32px] min-w-[32px] terminal-status text-gray-400 hover:text-status-offline hover:bg-terminal-raised touch-manipulation active:scale-95"
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

        {showSearch && (
          <div className="flex items-center gap-2 px-3 py-2 bg-terminal-raised border-b border-terminal-border">
            <Search className="h-4 w-4 text-gray-400" />
            <input
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              onKeyDown={handleSearchKeyDown}
              placeholder="Search… (Enter to find, Shift+Enter for previous)"
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

        {/* A guest panel draws over the terminal, so xterm keeps its size. */}
        <div className="flex-1 overflow-hidden relative" onDragOver={handleFileDragOver}>
          <TerminalInstance
            ref={terminalInstanceRef}
            terminalId={terminal.id}
            instanceName={terminal.instanceName}
            host={terminal.host}
            project={terminal.project}
          />
          <GuestPanelOverlay panel={panel} access={panelAccess} target={terminal} />
        </div>
      </div>
    </Rnd>
  );
}

export default TerminalWindow;
