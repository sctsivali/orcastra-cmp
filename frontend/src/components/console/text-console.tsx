/**
 * Text Console - thin xterm.js + WebSocket wrapper for shell access.
 *
 * Transport and terminal logic live in the shared useTerminalSession hook
 * (see src/lib/use-terminal-socket.ts); this component binds it to the console
 * store and exposes search/focus/paste to the parent.
 */

"use client";

import * as React from "react";
import { useRef, forwardRef, useImperativeHandle } from "react";
import { useConsoleStore } from "@/lib/console-store";
import { useTerminalSession, type TerminalStatus } from "@/lib/use-terminal-socket";

interface TextConsoleProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  onReady?: () => void;
  onError?: (error: string) => void;
  onClose?: () => void;
  // Optional direct callbacks for consumers that are not store-driven
  // (e.g. the full-page console route).
  onStatusChange?: (status: TerminalStatus, detail?: string) => void;
  onSessionIdChange?: (sessionId: string) => void;
}

export interface TextConsoleHandle {
  search: (term: string, findNext?: boolean) => void;
  focus: () => void;
  paste: (text: string) => void;
  fit: () => void;
  setFontSize: (px: number) => number;
}

export const TextConsole = forwardRef<TextConsoleHandle, TextConsoleProps>(function TextConsole({
  consoleId,
  instanceName,
  host,
  project,
  onReady,
  onError,
  onClose,
  onStatusChange,
  onSessionIdChange,
}, ref) {
  const terminalRef = useRef<HTMLDivElement>(null);
  const updateConnectionStatus = useConsoleStore((s) => s.updateConnectionStatus);
  const setConsoleSessionId = useConsoleStore((s) => s.setConsoleSessionId);

  const session = useTerminalSession({
    containerRef: terminalRef,
    instanceName,
    host,
    project,
    welcomeTitle: "Orcastra Console",
    onStatus: (status, detail) => {
      updateConnectionStatus(consoleId, status, detail);
      onStatusChange?.(status, detail);
    },
    onSessionId: (sessionId) => {
      setConsoleSessionId(consoleId, sessionId);
      onSessionIdChange?.(sessionId);
    },
    onReady,
    onError,
    onClose,
  });

  useImperativeHandle(ref, () => ({
    search: session.search,
    focus: session.focus,
    paste: session.paste,
    fit: session.fit,
    setFontSize: session.setFontSize,
  }), [session.search, session.focus, session.paste, session.fit, session.setFontSize]);

  return (
    <div
      ref={terminalRef}
      className="w-full h-full min-h-[300px] bg-terminal"
      style={{ padding: "4px" }}
    />
  );
});

export default TextConsole;
