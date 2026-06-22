"use client";

import { Terminal, Monitor, Box, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";

// Terminal menu item with active terminals count
export function TerminalMenuItem() {
  const terminals = useTerminalStore((s) => s.terminals);
  const totalCount = terminals.length;
  const store = useTerminalStore.getState();

  if (totalCount === 0) {
    return (
      <Button
        variant="ghost"
        className="w-full justify-start text-sm relative text-muted-foreground"
        size="sm"
        disabled
      >
        <Terminal className="h-4 w-4 mr-2" />
        Terminals
      </Button>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          className="w-full justify-start text-sm relative"
          size="sm"
        >
          <Terminal className="h-4 w-4 mr-2" />
          Terminals
          <Badge
            variant="secondary"
            className="ml-auto h-5 min-w-5 px-1.5 text-xs bg-blue-500/20 text-blue-500"
          >
            {totalCount}
          </Badge>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {terminals.map((terminal) => (
          <DropdownMenuItem
            key={terminal.id}
            className="flex items-center justify-between gap-2"
            onClick={() => {
              if (terminal.minimized) {
                store.restoreTerminal(terminal.id);
              } else {
                store.focusTerminal(terminal.id);
              }
            }}
          >
            <div className="flex items-center gap-2 flex-1 truncate">
              <Terminal className="h-4 w-4 shrink-0" />
              <span className="truncate">{terminal.instanceName}</span>
            </div>
            <div className="flex items-center gap-1">
              {terminal.minimized ? (
                <Badge variant="outline" className="text-xs">minimized</Badge>
              ) : (
                <Badge variant="secondary" className="text-xs">open</Badge>
              )}
              <Button
                variant="ghost"
                size="icon"
                className="h-5 w-5"
                onClick={(e) => {
                  e.stopPropagation();
                  store.closeTerminal(terminal.id);
                }}
              >
                <X className="h-3 w-3" />
              </Button>
            </div>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// Console menu item with active consoles count
export function ConsoleMenuItem() {
  const consoles = useConsoleStore((s) => s.consoles);
  const totalCount = consoles.length;
  const store = useConsoleStore.getState();

  if (totalCount === 0) {
    return (
      <Button
        variant="ghost"
        className="w-full justify-start text-sm relative text-muted-foreground"
        size="sm"
        disabled
      >
        <Monitor className="h-4 w-4 mr-2" />
        Consoles
      </Button>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          className="w-full justify-start text-sm relative"
          size="sm"
        >
          <Monitor className="h-4 w-4 mr-2" />
          Consoles
          <Badge
            variant="secondary"
            className="ml-auto h-5 min-w-5 px-1.5 text-xs bg-green-500/20 text-green-500"
          >
            {totalCount}
          </Badge>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {consoles.map((console) => (
          <DropdownMenuItem
            key={console.id}
            className="flex items-center justify-between gap-2"
            onClick={() => {
              if (console.minimized) {
                store.restoreConsole(console.id);
              } else {
                store.focusConsole(console.id);
              }
            }}
          >
            <div className="flex items-center gap-2 flex-1 truncate">
              {console.instanceType === "virtual-machine" ? (
                <Monitor className="h-4 w-4 shrink-0" />
              ) : (
                <Box className="h-4 w-4 shrink-0" />
              )}
              <span className="truncate">{console.instanceName}</span>
            </div>
            <div className="flex items-center gap-1">
              {console.minimized ? (
                <Badge variant="outline" className="text-xs">minimized</Badge>
              ) : (
                <Badge variant="secondary" className="text-xs">open</Badge>
              )}
              <Button
                variant="ghost"
                size="icon"
                className="h-5 w-5"
                onClick={(e) => {
                  e.stopPropagation();
                  store.closeConsole(console.id);
                }}
              >
                <X className="h-3 w-3" />
              </Button>
            </div>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
