/**
 * Terminal Instance - thin xterm.js + WebSocket wrapper.
 *
 * All terminal/transport logic lives in the shared useTerminalSession hook
 * (see src/lib/use-terminal-socket.ts); this component only binds it to the
 * terminal store and exposes search/focus to the parent.
 */

"use client";

import * as React from "react";
import { useRef, useCallback, forwardRef, useImperativeHandle } from "react";
import { useTerminalStore } from "@/lib/terminal-store";
import { useTerminalSession } from "@/lib/use-terminal-socket";

interface TerminalInstanceProps {
  terminalId: string;
  instanceName: string;
  host: string;
  project: string;
  onReady?: () => void;
  onError?: (error: string) => void;
  onClose?: () => void;
  // Fired when the session id arrives - lets a host that does not use the terminal store
  // (ex; the full-page view) feed the resource bar.
  onSessionId?: (sessionId: string) => void;
}

export interface TerminalInstanceHandle {
  search: (term: string, findNext?: boolean) => void;
  focus: () => void;
  setFontSize: (px: number) => number;
}

export const TerminalInstance = forwardRef<TerminalInstanceHandle, TerminalInstanceProps>(function TerminalInstance({
  terminalId,
  instanceName,
  host,
  project,
  onReady,
  onError,
  onClose,
  onSessionId,
}, ref) {
  const terminalRef = useRef<HTMLDivElement>(null);
  const { updateTerminalStatus, setTerminalSessionId } = useTerminalStore();

  const handleSessionId = useCallback((sessionId: string) => {
    setTerminalSessionId(terminalId, sessionId);
    onSessionId?.(sessionId);
  }, [terminalId, setTerminalSessionId, onSessionId]);

  const session = useTerminalSession({
    containerRef: terminalRef,
    instanceName,
    host,
    project,
    welcomeTitle: "Orcastra Terminal",
    onStatus: (status, detail) => updateTerminalStatus(terminalId, status, detail),
    onSessionId: handleSessionId,
    onReady,
    onError,
    onClose,
  });

  useImperativeHandle(ref, () => ({
    search: session.search,
    focus: session.focus,
    setFontSize: session.setFontSize,
  }), [session.search, session.focus, session.setFontSize]);

  return (
    <div
      ref={terminalRef}
      className="w-full h-full bg-[#1a1b26] rounded-b-lg overflow-hidden"
      onClick={session.focus}
    />
  );
});

export default TerminalInstance;
