/** The floating terminal window's shell: a TerminalView that reports to the terminal store. */

"use client";

import * as React from "react";
import { forwardRef, useImperativeHandle, useRef } from "react";
import { useTerminalStore } from "@/lib/terminal-store";
import { TerminalView, asConnectionStatus, type TerminalViewHandle } from "./terminal-view";

interface TerminalInstanceProps {
  terminalId: string;
  instanceName: string;
  host: string;
  project: string;
  onReady?: () => void;
}

export interface TerminalInstanceHandle {
  search: (term: string, findNext?: boolean) => void;
  focus: () => void;
  paste: (text: string) => void;
}

export const TerminalInstance = forwardRef<TerminalInstanceHandle, TerminalInstanceProps>(function TerminalInstance(
  { terminalId, instanceName, host, project, onReady },
  ref,
) {
  const viewRef = useRef<TerminalViewHandle | null>(null);
  const updateTerminalStatus = useTerminalStore((s) => s.updateTerminalStatus);
  const setTerminalSessionId = useTerminalStore((s) => s.setTerminalSessionId);

  useImperativeHandle(ref, () => ({
    search: (term, findNext = true) => {
      viewRef.current?.search(term, findNext);
    },
    focus: () => viewRef.current?.focus(),
    paste: (text) => viewRef.current?.paste(text),
  }), []);

  return (
    <TerminalView
      ref={viewRef}
      kind="shell"
      instanceName={instanceName}
      host={host}
      project={project}
      onStatus={(status, detail) => updateTerminalStatus(terminalId, asConnectionStatus(status), status === "error" ? detail : undefined)}
      onSessionId={(sessionId) => setTerminalSessionId(terminalId, sessionId)}
      onReady={() => {
        viewRef.current?.focus();
        onReady?.();
      }}
    />
  );
});
