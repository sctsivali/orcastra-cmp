/** The bottom-right tray of minimized terminals, reordered by drag on pointer devices. */

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
  Loader2,
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
import { SessionStatusIcon } from "./session-status";

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
  // Touch devices have no hover, so the buttons always show there.
  const [isMobile, setIsMobile] = useState(false);
  
  React.useEffect(() => {
    setIsMobile('ontouchstart' in window || navigator.maxTouchPoints > 0);
  }, []);

  const showButtons = isHovered || isMobile;

  return (
    <div
      draggable={!isMobile}
      onDragStart={(e) => !isMobile && onDragStart(e, terminal.id)}
      onDragOver={onDragOver}
      onDrop={(e) => onDrop(e, terminal.id)}
      className={cn(
        "relative group flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer transition-all",
        "bg-terminal-raised hover:bg-terminal-border border border-terminal-border hover:border-blue-500/50",
        // Room for the absolutely positioned action buttons.
        (showButtons || isMobile) && "pr-20",
        isDragging && "opacity-50 scale-95",
        isDropTarget && "border-primary bg-primary/10",
        // 44 px touch target.
        "min-h-[44px]"
      )}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      onTouchStart={() => setIsHovered(true)}
      onClick={onRestore}
    >
      {!isMobile && (
        <div className="opacity-0 group-hover:opacity-100 transition-opacity cursor-grab active:cursor-grabbing">
          <GripVertical className="h-3 w-3 text-gray-500" />
        </div>
      )}

      <TerminalIcon className="h-4 w-4 shrink-0 text-blue-400" />
      <SessionStatusIcon status={terminal.status} />

      <div className="flex flex-col min-w-0">
        <span className="text-xs font-medium text-gray-200 truncate max-w-[120px]">
          {terminal.instanceName}
        </span>
        <span className="max-w-[120px] truncate text-xs text-gray-500">
          {terminal.host}
        </span>
      </div>

      {showButtons && (
        <div className="absolute right-2 flex items-center gap-1">
          <TooltipProvider delayDuration={0}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 min-h-[32px] min-w-[32px] terminal-status text-gray-400 hover:text-status-online hover:bg-terminal-raised touch-manipulation"
                  aria-label={`Restore terminal ${terminal.instanceName} on ${terminal.host}`}
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
                  className="h-8 w-8 min-h-[32px] min-w-[32px] terminal-status text-gray-400 hover:text-status-offline hover:bg-terminal-raised touch-manipulation"
                  aria-label={`Close terminal ${terminal.instanceName} on ${terminal.host}`}
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

  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);

  const minimizedTerminals = terminals.filter((t) => t.minimized);
  const activeTerminals = terminals.filter((t) => !t.minimized);

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
      // The store reorders by position in the minimized list.
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

  if (terminals.length === 0) return null;

  return (
    <div className="fixed bottom-[calc(1rem+var(--dock-height,0px))] right-4 z-[99999] flex flex-col items-end gap-2 pointer-events-auto">
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

          <div className="flex items-center gap-1.5 justify-end">
            <TooltipProvider delayDuration={0}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 px-2 text-[10px] gap-1 bg-terminal border-terminal-border text-gray-300 terminal-status hover:bg-terminal-raised hover:border-status-online/50 hover:text-status-online"
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
                    className="h-7 px-2 text-[10px] gap-1 bg-terminal border-terminal-border text-gray-300 terminal-status hover:bg-terminal-raised hover:border-status-offline/50 hover:text-status-offline"
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

      <div className="flex items-center gap-2">
        {activeTerminals.length > 0 && (
          <Badge variant="outline" className="min-h-[32px] border-terminal-border bg-terminal text-gray-200">
            {activeTerminals.length} active
          </Badge>
        )}

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
              <span className="ml-1 text-gray-400 tabular-nums">({minimizedTerminals.length})</span>
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
