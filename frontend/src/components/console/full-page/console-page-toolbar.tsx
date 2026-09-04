/**
 * Toolbar for the full-page console route - instance controls, console
 * helpers (clipboard / on-screen keyboard / cursor), zoom, search, BIOS keys
 * and reconnect. Pure presentational: all state lives in the page.
 */

"use client";

import * as React from "react";
import {
  Search,
  RefreshCw,
  Play,
  Square,
  RotateCcw,
  Keyboard,
  ClipboardList,
  ZoomIn,
  ZoomOut,
  Rocket,
  ChevronDown,
  MousePointer2,
} from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
  DropdownMenuLabel,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export type ConsoleOperation = "start" | "stop" | "restart";
export interface BiosKey {
  key: string;
  label: string;
  description: string;
  keyCode: number;
}

interface ConsolePageToolbarProps {
  isVM: boolean;
  activeTab: "text" | "graphics";
  showStart: boolean;
  showRestartStop: boolean;
  operationLoading: ConsoleOperation | null;
  isOperationDisabled: boolean;
  onOperation: (op: ConsoleOperation) => void;
  showClipboard: boolean;
  showKeyboard: boolean;
  showCursor: boolean;
  onToggleClipboard: () => void;
  onToggleKeyboard: () => void;
  onToggleCursor: () => void;
  zoom: number;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onZoomReset: () => void;
  showSearch: boolean;
  onToggleSearch: () => void;
  biosSpamming: boolean;
  biosKeys: BiosKey[];
  onBiosSpam: (key: string, keyCode: number) => void;
  onReconnect: () => void;
}

export function ConsolePageToolbar({
  isVM,
  activeTab,
  showStart,
  showRestartStop,
  operationLoading,
  isOperationDisabled,
  onOperation,
  showClipboard,
  showKeyboard,
  showCursor,
  onToggleClipboard,
  onToggleKeyboard,
  onToggleCursor,
  zoom,
  onZoomIn,
  onZoomOut,
  onZoomReset,
  showSearch,
  onToggleSearch,
  biosSpamming,
  biosKeys,
  onBiosSpam,
  onReconnect,
}: ConsolePageToolbarProps) {
  return (
    <>
      <div className="flex items-center gap-1 mr-2 border-r border-gray-600 pr-2">
        {showStart && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={cn("h-8 w-8 text-gray-400 hover:text-green-400 hover:bg-green-400/10", operationLoading === "start" && "text-green-400")}
                onClick={() => onOperation("start")}
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
                  onClick={() => onOperation("restart")}
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
                  onClick={() => onOperation("stop")}
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

      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon" className={cn("h-8 w-8 text-gray-400 hover:text-white", showClipboard && "bg-white/10 text-white")} onClick={onToggleClipboard}>
            <ClipboardList className="h-4 w-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Clipboard Manager</TooltipContent>
      </Tooltip>

      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon" className={cn("h-8 w-8 text-gray-400 hover:text-white", showKeyboard && "bg-white/10 text-white")} onClick={onToggleKeyboard}>
            <Keyboard className="h-4 w-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>On-Screen Keyboard</TooltipContent>
      </Tooltip>

      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon" className={cn("h-8 w-8 text-gray-400 hover:text-white", showCursor && "bg-white/10 text-white")} onClick={onToggleCursor}>
            <MousePointer2 className="h-4 w-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>On-Screen Cursor</TooltipContent>
      </Tooltip>

      <div className="flex items-center gap-0.5 border-l border-gray-600 pl-2 ml-1">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className="h-8 w-8 text-gray-400 hover:text-white" onClick={onZoomOut} disabled={zoom <= 50}>
              <ZoomOut className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Zoom Out</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="sm" className="h-8 px-2 text-gray-400 hover:text-white text-xs font-mono" onClick={onZoomReset}>
              {zoom}%
            </Button>
          </TooltipTrigger>
          <TooltipContent>Reset Zoom (100%)</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className="h-8 w-8 text-gray-400 hover:text-white" onClick={onZoomIn} disabled={zoom >= 200}>
              <ZoomIn className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Zoom In</TooltipContent>
        </Tooltip>
      </div>

      <div className="h-5 w-px bg-gray-600 mx-1" />

      {activeTab === "text" && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className={cn("h-8 w-8 text-gray-400 hover:text-white", showSearch && "bg-white/10 text-white")} onClick={onToggleSearch}>
              <Search className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Search (Ctrl+Shift+F)</TooltipContent>
        </Tooltip>
      )}

      {isVM && activeTab === "graphics" && (
        <DropdownMenu>
          <Tooltip>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="h-8 px-2 text-gray-400 hover:text-white gap-1" disabled={biosSpamming}>
                  {biosSpamming ? <Spinner className="h-4 w-4" /> : <Rocket className="h-4 w-4" />}
                  <span className="text-xs hidden sm:inline">BIOS</span>
                  <ChevronDown className="h-3 w-3" />
                </Button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent>BIOS Boot Keys</TooltipContent>
          </Tooltip>
          <DropdownMenuContent align="end" className="w-56 bg-terminal-raised border-terminal-border">
            <DropdownMenuLabel className="text-xs text-gray-400">Spam key to enter BIOS/Boot Menu</DropdownMenuLabel>
            <DropdownMenuSeparator className="bg-terminal-border" />
            {biosKeys.map((bios) => (
              <DropdownMenuItem key={bios.key} onClick={() => onBiosSpam(bios.key, bios.keyCode)} disabled={biosSpamming} className="text-gray-200 focus:bg-terminal-border focus:text-white">
                <div className="flex items-center justify-between w-full">
                  <span className="font-mono font-medium">{bios.label}</span>
                  <span className="text-xs text-gray-500">{bios.description}</span>
                </div>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon" className="h-8 w-8 text-gray-400 hover:text-white" onClick={onReconnect}>
            <RefreshCw className="h-4 w-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Reconnect</TooltipContent>
      </Tooltip>
    </>
  );
}

export default ConsolePageToolbar;
