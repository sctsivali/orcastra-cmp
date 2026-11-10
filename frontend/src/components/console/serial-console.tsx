/**
 * The instance's serial console (LXD console type "console").
 *
 * It needs neither the VM agent nor networking, so it is what an operator uses to watch a boot,
 * reach a rescue prompt, or log in to a guest whose agent is not running. Output from before the
 * connection is not replayed; press Enter for a fresh prompt.
 */

"use client";

import * as React from "react";
import { forwardRef, useImperativeHandle, useRef } from "react";
import { TerminalView, asConnectionStatus, type TerminalViewHandle } from "@/components/terminal/terminal-view";

interface SerialConsoleProps {
  instanceName: string;
  host: string;
  project: string;
  fontSize?: number;
  onStatus?: (status: ReturnType<typeof asConnectionStatus>, detail?: string) => void;
}

export interface SerialConsoleHandle {
  focus: () => void;
  paste: (text: string) => void;
  sendRaw: (data: string) => void;
  search: (term: string, findNext?: boolean) => boolean;
  reconnect: () => void;
  setFontSize: (px: number) => number;
}

export const SerialConsole = forwardRef<SerialConsoleHandle, SerialConsoleProps>(function SerialConsole(
  { instanceName, host, project, fontSize, onStatus },
  ref,
) {
  const viewRef = useRef<TerminalViewHandle | null>(null);
  useImperativeHandle(ref, () => ({
    focus: () => viewRef.current?.focus(),
    paste: (text) => viewRef.current?.paste(text),
    sendRaw: (data) => viewRef.current?.sendRaw(data),
    search: (term, findNext = true) => viewRef.current?.search(term, findNext) ?? false,
    reconnect: () => viewRef.current?.reconnect(),
    setFontSize: (px) => viewRef.current?.setFontSize(px) ?? px,
  }), []);

  return (
    <TerminalView
      ref={viewRef}
      kind="serial"
      instanceName={instanceName}
      host={host}
      project={project}
      fontSize={fontSize}
      onStatus={(status, detail) => onStatus?.(asConnectionStatus(status), detail)}
    />
  );
});
