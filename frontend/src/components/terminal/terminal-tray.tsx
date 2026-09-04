/**
 * Terminal Tray - Minimized terminals container.
 * Shows minimized terminals in a tray at the bottom-right corner.
 * Similar to Facebook chat bubbles.
 * Supports drag-to-reorder functionality.
 */

"use client";

import * as React from "react";
import { useState, useRef } from "react";
import {
  Terminal as TerminalIcon,
  ChevronUp,
  ChevronDown,
  X,
  Maximize2,
  Server,
  AlertCircle,
  CheckCircle2,
  GripVertical,
  XCircle,
  Layers,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useTerminalStore, TerminalInstance } from "@/lib/terminal-store";
import { Badge } from "@/components/ui/badge";

// Status indicator for tray items
function TrayStatusDot({ status }: { status: TerminalInstance["status"] }) {
  switch (status) {
    case "connecting":
      return (
        <div className="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-yellow-400 animate-pulse-gentle" />
      );
    case "connected":
      return (
        <div className="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-green-400" />
      );
    case "error":
      return (
        <div className="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-red-400" />
      );
    default:
      return (
        <div className="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-gray-400" />
      );
  }
}

interface TrayItemProps {
  terminal: TerminalInstance;
  onRestore: () => void;
  onClose: () => void;
  onDragStart: (e: React.DragEvent, terminalId: string) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent, terminalId: string) => void;
  isDragging: boolean;
  isDropTarget: boolean;
}

function TrayItem({ 
  terminal, 
  onRestore, 
  onClose,
  onDragStart,
  onDragOver,
  onDrop,
  isDragging,
  isDropTarget,
}: TrayItemProps) {
  const [isHovered, setIsHovered] = useState(false);
  // Always show buttons on mobile (touch devices)
  const [isMobile, setIsMobile] = useState(false);
  
  React.useEffect(() => {
    // Check if device supports touch
    setIsMobile('ontouchstart' in window || navigator.maxTouchPoints > 0);
  }, []);

  const showButtons = isHovered || isMobile;

  return (
    <div
      draggable={!isMobile} // Disable drag on mobile to prevent issues
      onDragStart={(e) => !isMobile && onDragStart(e, terminal.id)}
      onDragOver={onDragOver}
      onDrop={(e) => onDrop(e, terminal.id)}
      className={cn(
        "relative group flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer transition-all",
        "bg-terminal-raised hover:bg-terminal-border border border-terminal-border hover:border-blue-500/50",
        // Always add padding for buttons on mobile
        (showButtons || isMobile) && "pr-20",
        isDragging && "opacity-50 scale-95",
        isDropTarget && "border-blue-500 bg-blue-500/10",
        // Better touch target on mobile
        "min-h-[44px]"
      )}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      onTouchStart={() => setIsHovered(true)}
      onClick={onRestore}
    >
      {/* Drag handle - hide on mobile */}
      {!isMobile && (
        <div className="opacity-0 group-hover:opacity-100 transition-opacity cursor-grab active:cursor-grabbing">
          <GripVertical className="h-3 w-3 text-gray-500" />
        </div>
      )}

      {/* Icon with status */}
      <div className="relative">
        <TerminalIcon className="h-4 w-4 text-blue-400" />
        <TrayStatusDot status={terminal.status} />
      </div>

      {/* Name */}
      <div className="flex flex-col min-w-0">
        <span className="text-xs font-medium text-gray-200 truncate max-w-[120px]">
          {terminal.instanceName}
        </span>
        <span className="text-[10px] text-gray-500 truncate max-w-[120px]">
          {terminal.host}
        </span>
      </div>

      {/* Actions - always visible on mobile */}
      {showButtons && (
        <div className="absolute right-2 flex items-center gap-1">
          <TooltipProvider delayDuration={0}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-green-400 hover:bg-green-400/10 touch-manipulation"
                  onClick={(e) => {
                    e.stopPropagation();
                    onRestore();
                  }}
                >
                  <Maximize2 className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Restore</TooltipContent>
            </Tooltip>

            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-red-400 hover:bg-red-400/10 touch-manipulation"
                  onClick={(e) => {
                    e.stopPropagation();
                    onClose();
                  }}
                >
                  <X className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Close</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      )}
    </div>
  );
}

export function TerminalTray() {
  const { terminals, trayExpanded, toggleTray, restoreTerminal, closeTerminal, closeAllTerminals, restoreAllTerminals, reorderMinimizedTerminals } =
    useTerminalStore();

  // Drag and drop state
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);

  // Get minimized terminals
  const minimizedTerminals = terminals.filter((t) => t.minimized);
  const activeTerminals = terminals.filter((t) => !t.minimized);

  // Drag handlers
  const handleDragStart = (e: React.DragEvent, terminalId: string) => {
    setDraggedId(terminalId);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', terminalId);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  };

  const handleDrop = (e: React.DragEvent, targetId: string) => {
    e.preventDefault();
    const sourceId = e.dataTransfer.getData('text/plain');
    
    if (sourceId && sourceId !== targetId) {
      // Find indices in the minimized list
      const minimizedIds = minimizedTerminals.map(t => t.id);
      const fromIndex = minimizedIds.indexOf(sourceId);
      const toIndex = minimizedIds.indexOf(targetId);
      
      if (fromIndex !== -1 && toIndex !== -1) {
        reorderMinimizedTerminals(fromIndex, toIndex);
      }
    }
    
    setDraggedId(null);
    setDropTargetId(null);
  };

  const handleDragEnd = () => {
    setDraggedId(null);
    setDropTargetId(null);
  };

  // Don't render if no terminals
  if (terminals.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-[99999] flex flex-col items-end gap-2 pointer-events-auto">
      {/* Minimized terminals list */}
      {trayExpanded && minimizedTerminals.length > 0 && (
        <div 
          className="flex flex-col gap-2 mb-2 animate-in slide-in-from-bottom-2 fade-in duration-200"
          onDragEnd={handleDragEnd}
        >
          {minimizedTerminals.map((terminal) => (
            <TrayItem
              key={terminal.id}
              terminal={terminal}
              onRestore={() => restoreTerminal(terminal.id)}
              onClose={() => closeTerminal(terminal.id)}
              onDragStart={handleDragStart}
              onDragOver={(e) => {
                handleDragOver(e);
                setDropTargetId(terminal.id);
              }}
              onDrop={handleDrop}
              isDragging={draggedId === terminal.id}
              isDropTarget={dropTargetId === terminal.id && draggedId !== terminal.id}
            />
          ))}

          {/* Bulk actions for minimized terminals */}
          <div className="flex items-center gap-1.5 justify-end">
            <TooltipProvider delayDuration={0}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 px-2 text-[10px] gap-1 bg-terminal border-terminal-border text-gray-300 hover:bg-green-500/20 hover:border-green-500/50 hover:text-green-400"
                    onClick={() => restoreAllTerminals()}
                  >
                    <Layers className="h-3 w-3" />
                    Show All
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Restore all minimized terminals</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <TooltipProvider delayDuration={0}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 px-2 text-[10px] gap-1 bg-terminal border-terminal-border text-gray-300 hover:bg-red-500/20 hover:border-red-500/50 hover:text-red-400"
                    onClick={() => closeAllTerminals()}
                  >
                    <XCircle className="h-3 w-3" />
                    Close All
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Close all terminals</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          </div>
        </div>
      )}

      {/* Tray toggle button */}
      <div className="flex items-center gap-2">
        {/* Active count badge */}
        {activeTerminals.length > 0 && (
          <Badge variant="secondary" className="bg-blue-500/20 text-blue-400 border-blue-500/30 min-h-[32px] flex items-center">
            {activeTerminals.length} active
          </Badge>
        )}

        {/* Main tray button - ensure proper touch target */}
        <Button
          variant="outline"
          size="sm"
          className={cn(
            "gap-2 bg-terminal border-terminal-border text-gray-200",
            "hover:bg-terminal-border hover:border-blue-500/50 hover:text-white",
            "min-h-[44px] min-w-[44px] touch-manipulation",
            minimizedTerminals.length > 0 && "animate-pulse-slow"
          )}
          onClick={toggleTray}
        >
          <TerminalIcon className="h-4 w-4" />
          <span className="text-sm">
            Terminals
            {minimizedTerminals.length > 0 && (
              <span className="ml-1 text-yellow-400">({minimizedTerminals.length})</span>
            )}
          </span>
          {trayExpanded ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronUp className="h-3 w-3" />
          )}
        </Button>
      </div>
    </div>
  );
}

export default TerminalTray;
