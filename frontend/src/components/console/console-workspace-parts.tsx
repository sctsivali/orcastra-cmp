/**
 * Small pieces of the console workspace: the terminal key map of the on-screen keyboard, the
 * output search bar, the "another console is open" notice, the full-screen chrome and the two
 * guest panes (Files and Credentials).
 */

"use client";

import * as React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp, MonitorX, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CredentialsPanel } from "@/components/guest-credentials/credentials-panel";
import { InstanceFileExplorer } from "@/components/instance-files/instance-file-explorer";
import { cn, formatClockTime } from "@/lib/utils";
import type { ActiveConsoleSession } from "@/lib/console-api";
import type { ConsoleConnectionStatus, ConsoleTab } from "@/lib/console-store";

const TERMINAL_KEYS: Record<string, string> = {
  Enter: "\r", Tab: "\t", Escape: "\x1b", Backspace: "\x7f", Delete: "\x1b[3~", Insert: "\x1b[2~",
  ArrowUp: "\x1b[A", ArrowDown: "\x1b[B", ArrowRight: "\x1b[C", ArrowLeft: "\x1b[D",
  Home: "\x1b[H", End: "\x1b[F", PageUp: "\x1b[5~", PageDown: "\x1b[6~",
  F1: "\x1bOP", F2: "\x1bOQ", F3: "\x1bOR", F4: "\x1bOS", F5: "\x1b[15~", F6: "\x1b[17~",
  F7: "\x1b[18~", F8: "\x1b[19~", F9: "\x1b[20~", F10: "\x1b[21~", F11: "\x1b[23~", F12: "\x1b[24~",
};

/** What an on-screen keyboard key types into a terminal, with Ctrl and Alt applied. */
export function terminalSequence(key: string, modifiers: { ctrl?: boolean; alt?: boolean; shift?: boolean } = {}): string {
  let out = TERMINAL_KEYS[key] ?? (key === "Space" ? " " : key);
  if (key.length === 1) {
    if (modifiers.shift) out = key.toUpperCase();
    if (modifiers.ctrl) {
      const code = key.toLowerCase().charCodeAt(0);
      if (code >= 97 && code <= 122) out = String.fromCharCode(code - 96);
    }
  }
  // Alt sends ESC first (meta), as xterm does with macOptionIsMeta.
  return modifiers.alt ? `\x1b${out}` : out;
}

export function SearchBar({
  onSearch,
  onClose,
}: {
  onSearch: (term: string, forward: boolean) => boolean;
  onClose: () => void;
}) {
  const [term, setTerm] = useState("");
  const [missed, setMissed] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => inputRef.current?.focus(), []);
  const run = (forward: boolean) => setMissed(term.length > 0 && !onSearch(term, forward));
  return (
    <div className="absolute right-3 top-2 z-20 flex items-center gap-1 rounded-md border border-terminal-border bg-terminal-raised p-1 shadow-lg" role="search">
      <Search className="ml-1 h-4 w-4 text-gray-400" aria-hidden="true" />
      <Input
        ref={inputRef}
        value={term}
        onChange={(e) => setTerm(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") run(!e.shiftKey);
          if (e.key === "Escape") onClose();
        }}
        placeholder="Search the output…"
        aria-label="Search the output"
        aria-invalid={missed}
        autoComplete="off"
        spellCheck={false}
        className={cn("h-7 w-48 border-0 bg-transparent text-sm text-gray-100 focus-visible:ring-1", missed && "text-status-offline")}
      />
      <Button type="button" variant="ghost" size="icon" className="h-7 w-7 text-gray-300" onClick={() => run(false)} aria-label="Previous match">
        <ChevronUp className="h-4 w-4" aria-hidden="true" />
      </Button>
      <Button type="button" variant="ghost" size="icon" className="h-7 w-7 text-gray-300" onClick={() => run(true)} aria-label="Next match">
        <ChevronDown className="h-4 w-4" aria-hidden="true" />
      </Button>
      <Button type="button" variant="ghost" size="icon" className="h-7 w-7 text-gray-300" onClick={onClose} aria-label="Close search">
        <X className="h-4 w-4" aria-hidden="true" />
      </Button>
    </div>
  );
}

function clock(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : formatClockTime(date.getTime());
}

/**
 * QEMU's display serves one viewer at a time: opening it here disconnects whoever is watching.
 * Shown before connecting, so the operator knows before cutting the other viewer off.
 */
export function ConflictNotice({
  sessions,
  onOpen,
  onCancel,
}: {
  sessions: ActiveConsoleSession[];
  onOpen: () => void;
  onCancel: () => void;
}) {
  const since = sessions.map((s) => clock(s.created_at)).filter(Boolean).sort()[0];
  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center bg-black px-6" role="alertdialog" aria-labelledby="console-conflict-title">
      <div className="max-w-md space-y-3 text-center text-white/85">
        <MonitorX className="mx-auto h-9 w-9 text-white/60" aria-hidden="true" />
        <p id="console-conflict-title" className="text-sm font-medium text-white">Someone may be using this console</p>
        <p className="text-sm text-white/70">
          {sessions.length === 1 ? "Another console session is" : `${sessions.length} console sessions are`} open on this
          instance{since ? ` (since ${since})` : ""}. The display serves one viewer at a time, so opening it here disconnects the
          other one.
        </p>
        <div className="flex justify-center gap-2 pt-1">
          <Button variant="outline" size="sm" className="border-white/30 bg-transparent text-white hover:bg-white/10 hover:text-white" onClick={onCancel}>
            Use the Text Tab
          </Button>
          <Button size="sm" onClick={onOpen}>Open Here</Button>
        </div>
      </div>
    </div>
  );
}

/**
 * In full screen the tabs and toolbar step aside and come back when the pointer touches the top
 * edge or the keyboard moves into them, as a remote desktop's drawer does.
 */
export function useFullscreenChrome(getTarget: () => HTMLElement | null) {
  const [fullscreen, setFullscreen] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const onChange = () => {
      const on = Boolean(document.fullscreenElement) && document.fullscreenElement === getTarget();
      setFullscreen(on);
      setRevealed(false);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [getTarget]);

  const reveal = useCallback(() => {
    if (hideTimer.current) clearTimeout(hideTimer.current);
    setRevealed(true);
  }, []);
  const conceal = useCallback(() => {
    if (hideTimer.current) clearTimeout(hideTimer.current);
    hideTimer.current = setTimeout(() => setRevealed(false), 700);
  }, []);

  return { fullscreen, revealed, reveal, conceal };
}

export type GuestPaneTab = Extract<ConsoleTab, "files" | "credentials">;

export function isGuestPaneTab(tab: ConsoleTab): tab is GuestPaneTab {
  return tab === "files" || tab === "credentials";
}

/**
 * The Files and Credentials tabs: each mounted once like Text, so a half-typed path, a transfer
 * list or a chosen account survives a switch. Neither reads anything while it is not in front.
 */
export function GuestPane({
  tab,
  hidden,
  active,
  host,
  project,
  instanceName,
  onTabChange,
  onStatus,
}: {
  tab: GuestPaneTab;
  hidden: boolean;
  active: boolean;
  host: string;
  project: string;
  instanceName: string;
  onTabChange: (tab: ConsoleTab) => void;
  onStatus: (tab: GuestPaneTab, status: ConsoleConnectionStatus) => void;
}) {
  const props = { host, project, instanceName, active, onOpenConsoleTab: onTabChange, onHealth: (status: ConsoleConnectionStatus) => onStatus(tab, status) };
  return (
    <div className={cn("absolute inset-0", hidden && "hidden")}>
      {tab === "files" ? <InstanceFileExplorer {...props} /> : <CredentialsPanel {...props} />}
    </div>
  );
}
