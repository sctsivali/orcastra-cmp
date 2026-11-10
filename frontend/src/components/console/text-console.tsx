/**
 * The console window's shell on the instance (an exec, which needs the VM agent).
 * The Serial tab is the instance's serial console instead (serial-console.tsx).
 */

"use client";

import * as React from "react";
import { forwardRef, useImperativeHandle, useRef } from "react";
import { useConsoleStore } from "@/lib/console-store";
import { TerminalView, asConnectionStatus, type TerminalViewHandle } from "@/components/terminal/terminal-view";

interface TextConsoleProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  fontSize?: number;
  /** Report status into the console store (floating windows); the full page passes false. */
  reportToStore?: boolean;
  onStatus?: (status: ReturnType<typeof asConnectionStatus>, detail?: string) => void;
  onSessionId?: (sessionId: string) => void;
  onReady?: () => void;
}

export interface TextConsoleHandle {
  search: (term: string, findNext?: boolean) => boolean;
  focus: () => void;
  paste: (text: string) => void;
  sendRaw: (data: string) => void;
  reconnect: () => void;
  setFontSize: (px: number) => number;
}

export const TextConsole = forwardRef<TextConsoleHandle, TextConsoleProps>(function TextConsole(
  { consoleId, instanceName, host, project, fontSize, reportToStore = true, onStatus, onSessionId, onReady },
  ref,
) {
  const viewRef = useRef<TerminalViewHandle | null>(null);
  const updateConnectionStatus = useConsoleStore((s) => s.updateConnectionStatus);
  const setConsoleSessionId = useConsoleStore((s) => s.setConsoleSessionId);

  useImperativeHandle(ref, () => ({
    search: (term, findNext = true) => viewRef.current?.search(term, findNext) ?? false,
    focus: () => viewRef.current?.focus(),
    paste: (text) => viewRef.current?.paste(text),
    sendRaw: (data) => viewRef.current?.sendRaw(data),
    reconnect: () => viewRef.current?.reconnect(),
    setFontSize: (px) => viewRef.current?.setFontSize(px) ?? px,
  }), []);

  return (
    <TerminalView
      ref={viewRef}
      kind="shell"
      instanceName={instanceName}
      host={host}
      project={project}
      fontSize={fontSize}
      onStatus={(status, detail) => {
        const mapped = asConnectionStatus(status);
        if (reportToStore) updateConnectionStatus(consoleId, mapped, status === "error" ? detail : undefined);
        onStatus?.(mapped, detail);
      }}
      onSessionId={(sessionId) => {
        if (reportToStore) setConsoleSessionId(consoleId, sessionId);
        onSessionId?.(sessionId);
      }}
      onReady={onReady}
    />
  );
});
