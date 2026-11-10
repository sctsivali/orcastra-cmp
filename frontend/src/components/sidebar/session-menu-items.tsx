"use client";

import { Terminal, Monitor, Box, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

// The two count chips carry `sidebar-count` rather than a colour of their own; a tint under
// same-hue text is the shape scripts/checks/check-semantic-colour.mjs bans. The rule in
// app/globals.css mixes currentColor, so a chip stays legible at whatever sidebar colour an
// operator picks, and the labels beside them already say which is which.
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";

// Live Terminal/Console rows. They are store-driven: the count and the menu contents come
// from the open session windows, so they cannot live in the nav config. The counter Badge
// stays a pill because it is sidebar chrome, not table data.

interface SessionMenuProps {
  indent?: boolean;
}

type SessionWindowStatus = "open" | "minimized";

const SESSION_WINDOW_STATUS: Record<
  SessionWindowStatus,
  { label: string; tone: "secondary" | "outline" }
> = {
  open: { label: "Open", tone: "secondary" },
  minimized: { label: "Minimized", tone: "outline" },
};

function sessionWindowStatus(minimized: boolean) {
  return SESSION_WINDOW_STATUS[minimized ? "minimized" : "open"];
}

function rowClass(indent: boolean | undefined, muted: boolean) {
  return cn(
    "w-full justify-start text-sm relative",
    indent && "pl-6",
    muted && "text-muted-foreground"
  );
}

export function TerminalMenuItem({ indent }: SessionMenuProps) {
  const terminals = useTerminalStore((s) => s.terminals);
  const totalCount = terminals.length;
  const store = useTerminalStore.getState();

  if (totalCount === 0) {
    return (
      <Button variant="ghost" className={rowClass(indent, true)} size="sm" disabled>
        <Terminal className="h-4 w-4 mr-2" />
        Terminals
      </Button>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className={rowClass(indent, false)} size="sm">
          <Terminal className="h-4 w-4 mr-2" />
          Terminals
          <Badge
            variant="secondary"
            className="ml-auto h-5 min-w-5 px-1.5 text-xs sidebar-count"
          >
            {totalCount}
          </Badge>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {terminals.map((terminal) => {
          const status = sessionWindowStatus(terminal.minimized);
          return (
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
                <Badge variant={status.tone} className="text-xs">{status.label}</Badge>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-5 w-5"
                  aria-label={`Close terminal for ${terminal.instanceName}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    store.closeTerminal(terminal.id);
                  }}
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function ConsoleMenuItem({ indent }: SessionMenuProps) {
  const consoles = useConsoleStore((s) => s.consoles);
  const totalCount = consoles.length;
  const store = useConsoleStore.getState();

  if (totalCount === 0) {
    return (
      <Button variant="ghost" className={rowClass(indent, true)} size="sm" disabled>
        <Monitor className="h-4 w-4 mr-2" />
        Consoles
      </Button>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className={rowClass(indent, false)} size="sm">
          <Monitor className="h-4 w-4 mr-2" />
          Consoles
          <Badge
            variant="secondary"
            className="ml-auto h-5 min-w-5 px-1.5 text-xs sidebar-count"
          >
            {totalCount}
          </Badge>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {consoles.map((console) => {
          const status = sessionWindowStatus(console.minimized);
          return (
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
                <Badge variant={status.tone} className="text-xs">{status.label}</Badge>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-5 w-5"
                  aria-label={`Close console for ${console.instanceName}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    store.closeConsole(console.id);
                  }}
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
