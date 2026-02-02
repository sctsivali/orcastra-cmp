/**
 * Console Tray - Shows minimized console windows.
 * Fixed position at bottom of screen.
 */

"use client";

import * as React from "react";
import { X, Maximize2, Monitor, Terminal, Server } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { ConsoleInstance } from "@/lib/console-store";

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
          "fixed bottom-4 left-1/2 -translate-x-1/2 z-[99999]",
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

            {/* Status indicator */}
            <div
              className={cn(
                "h-2 w-2 rounded-full",
                console.connectionStatus === "connected" && "bg-green-500",
                console.connectionStatus === "connecting" && "bg-yellow-500 animate-pulse",
                console.connectionStatus === "disconnected" && "bg-gray-400",
                console.connectionStatus === "error" && "bg-red-500"
              )}
            />

            {/* Restore button - larger touch target */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 min-h-[32px] min-w-[32px] hover:bg-primary/20 touch-manipulation"
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
