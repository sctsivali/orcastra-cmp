/**
 * TerminalView: one xterm surface bound to a shell or a serial console. Every text terminal in
 * the app is this component (console Text and Serial tabs, floating terminals, full pages).
 */

"use client";

import * as React from "react";
import { forwardRef, useImperativeHandle, useRef, useState } from "react";
import { AlertCircle, Loader2, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  useTerminalSession,
  type TerminalKind,
  type TerminalSessionHandle,
  type TerminalSessionStatus,
} from "@/lib/use-terminal-socket";

export interface TerminalViewProps {
  kind: TerminalKind;
  instanceName: string;
  host: string;
  project: string;
  fontSize?: number;
  className?: string;
  onStatus?: (status: TerminalSessionStatus, detail?: string) => void;
  onSessionId?: (sessionId: string) => void;
  onReady?: () => void;
}

export type TerminalViewHandle = TerminalSessionHandle;

/** The four states the console and terminal stores (and their status chips) know. */
export function asConnectionStatus(status: TerminalSessionStatus): "connecting" | "connected" | "disconnected" | "error" {
  if (status === "connected") return "connected";
  if (status === "ended") return "disconnected";
  if (status === "error") return "error";
  return "connecting";
}

export const TerminalView = forwardRef<TerminalViewHandle, TerminalViewProps>(function TerminalView(
  { kind, instanceName, host, project, fontSize, className, onStatus, onSessionId, onReady },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<TerminalSessionStatus>("connecting");
  const [detail, setDetail] = useState("");
  const session = useTerminalSession({
    containerRef,
    kind,
    instanceName,
    host,
    project,
    fontSize,
    onStatus: (next, why) => {
      setStatus(next);
      setDetail(why ?? "");
      onStatus?.(next, why);
    },
    onSessionId,
    onReady,
  });
  useImperativeHandle(ref, () => session, [session]);

  const ended = status === "ended" || status === "error";

  return (
    <div className={cn("relative flex h-full w-full flex-col bg-terminal", className)}>
      {/* note: the padding is outside the fit container; FitAddon measures the container's parent. */}
      <div className="min-h-0 flex-1 p-1">
        <div
          ref={containerRef}
          className="h-full w-full"
          onClick={() => session.focus()}
          aria-label={kind === "serial" ? `Serial console of ${instanceName}` : `Shell on ${instanceName}`}
        />
      </div>
      {status === "reconnecting" && (
        <div className="absolute right-3 top-2 flex items-center gap-2 rounded bg-terminal-raised px-2.5 py-1 text-xs text-gray-200" role="status" aria-live="polite">
          <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          Reconnecting…
        </div>
      )}
      {ended && (
        <div className="absolute right-3 top-2 flex max-w-[80%] items-center gap-2 rounded bg-terminal-raised px-2.5 py-1 text-xs text-gray-200" role="status" aria-live="polite">
          {status === "error" && <AlertCircle className="terminal-status h-3.5 w-3.5 shrink-0 text-status-offline" aria-hidden="true" />}
          <span className="min-w-0 truncate" title={detail}>{detail || (status === "ended" ? "The session ended" : "The session failed")}</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 shrink-0 gap-1 px-2 text-xs text-gray-100 hover:bg-white/10"
            onClick={() => {
              session.reconnect();
              session.focus();
            }}
          >
            <RotateCcw className="h-3 w-3" aria-hidden="true" />
            {status === "ended" ? "New session" : "Retry"}
          </Button>
        </div>
      )}
    </div>
  );
});
