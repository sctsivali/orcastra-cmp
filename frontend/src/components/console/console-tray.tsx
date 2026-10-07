/**
 * Console Tray - Shows minimized console windows.
 * Fixed position at bottom of screen.
 */

"use client";

import * as React from "react";
import { X, Maximize2, Monitor, Terminal, Server } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { ConsoleInstance } from "@/lib/console-store";
import { SessionStatusBadge } from "@/components/terminal/session-status";

interface ConsoleTrayProps {
  consoles: ConsoleInstance[];
  onRestore: (id: string) => void;
  onClose: (id: string) => void;
  className?: string;
}

export function ConsoleTray({
  consoles,
  onRestore,
  onClose,
  className,
}: ConsoleTrayProps) {
  if (consoles.length === 0) {
    return null;
  }

  return (
    <TooltipProvider>
      <div
        className={cn(
          "fixed bottom-[calc(1rem+var(--dock-height,0px))] left-1/2 -translate-x-1/2 z-[99999]",
          "flex items-center gap-2 p-2 rounded-lg",
          "bg-background/95 backdrop-blur-sm border shadow-lg",
          "pointer-events-auto",
          className
        )}
      >
        {consoles.map((console) => (
          <div
            key={console.id}
            className="flex items-center gap-1 px-2 py-1 rounded-md bg-muted/50 hover:bg-muted transition-colors min-h-[44px]"
          >
            {/* Icon based on type */}
            {console.instanceType === "virtual-machine" ? (
              <Monitor className="h-4 w-4 text-blue-500" />
            ) : (
              <Terminal className="h-4 w-4 text-green-500" />
            )}

            {/* Instance name */}
            <span className="text-sm font-medium max-w-[120px] truncate">
              {console.instanceName}
            </span>

            <SessionStatusBadge status={console.connectionStatus} chrome={false} className="shrink-0" />

            {/* Restore button - larger touch target */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 min-h-[32px] min-w-[32px] hover:bg-primary/20 touch-manipulation"
                  aria-label={`Restore the console for ${console.instanceName}`}
                  onClick={() => onRestore(console.id)}
                >
                  <Maximize2 className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Restore</TooltipContent>
            </Tooltip>

            {/* Close button - larger touch target */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 min-h-[32px] min-w-[32px] hover:bg-destructive/20 touch-manipulation"
                  aria-label={`Close the console for ${console.instanceName}`}
                  onClick={() => onClose(console.id)}
                >
                  <X className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Close</TooltipContent>
            </Tooltip>
          </div>
        ))}
      </div>
    </TooltipProvider>
  );
}

export default ConsoleTray;
