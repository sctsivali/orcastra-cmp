"use client";

import { AlertCircle, CheckCircle2, Loader2, Unplug } from "lucide-react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

// Session state has its own vocabulary and does not go through lib/cluster-status.ts, which
// describes cluster reachability. Only the tones are shared.
export type SessionStatus = "connecting" | "connected" | "disconnected" | "error";

const SESSION: Record<SessionStatus, { label: string; tone: BadgeTone; text: string }> = {
  connecting: { label: "Connecting", tone: "caution", text: "text-status-pending" },
  connected: { label: "Connected", tone: "positive", text: "text-status-online" },
  disconnected: { label: "Disconnected", tone: "neutral", text: "text-status-unknown" },
  error: { label: "Error", tone: "negative", text: "text-status-offline" },
};

function SessionIcon({ status, className }: { status: SessionStatus; className?: string }) {
  const Icon =
    status === "connecting"
      ? Loader2
      : status === "connected"
        ? CheckCircle2
        : status === "error"
          ? AlertCircle
          : Unplug;
  // Only "connecting" spins: it is the one state that is still changing.
  return (
    <Icon
      className={cn("h-3 w-3 shrink-0", status === "connecting" && "animate-spin", className)}
      aria-hidden="true"
    />
  );
}

/**
 * Solid state chip for a console or terminal session. On the fixed-dark chrome `terminal-status`
 * rebinds the --status-* pair to its dark-theme values, which is what that chrome needs in either
 * page theme. A themed surface (the floating console window, the console tray) passes
 * `chrome={false}` and takes the page theme's pair like every other chip.
 */
export function SessionStatusBadge({
  status,
  chrome = true,
  className,
}: {
  status: SessionStatus;
  chrome?: boolean;
  className?: string;
}) {
  const meta = SESSION[status];
  return (
    <Badge variant={meta.tone} className={cn(chrome && "terminal-status", "gap-1", className)}>
      <SessionIcon status={status} />
      {meta.label}
    </Badge>
  );
}

/** Icon-only state for a title bar or a tray tile, where a word does not fit. Announced. */
export function SessionStatusIcon({ status }: { status: SessionStatus }) {
  const meta = SESSION[status];
  return (
    <span role="img" aria-label={meta.label} title={meta.label} className={cn("terminal-status inline-flex", meta.text)}>
      <SessionIcon status={status} />
    </span>
  );
}
