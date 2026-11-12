/**
 * Collapsible status bar at the bottom of the console and terminal pages.
 *
 * The compact bar (40 px) shows connection status, duration, resource chips and network rates on
 * one line; the expanded panel adds connection, user and instance resource details. A click or
 * Ctrl+I toggles it, and the choice is kept in localStorage.
 */

"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import {
  ChevronUp,
  ChevronDown,
  Wifi,
  WifiOff,
  Network,
  Clock,
  ArrowDownToLine,
  ArrowUpFromLine,
  Loader2,
} from "lucide-react";
import { cn, EMPTY_VALUE } from "@/lib/utils";
import { GRAPHICS_SESSION_PROTOCOL, sessionProtocolLabel } from "@/lib/session-display";
import { useSession } from "next-auth/react";
import { useInstanceMetrics } from "@/hooks";
import { useByteFormat } from "@/hooks/use-byte-format";
import { OverflowText } from "@/components/ui/overflow-text";
import { InstanceResourceChips, InstanceResourcesPanel } from "./instance-resources";

export interface SessionStats {
  session_id: string;
  instance_name: string;
  host: string;
  project: string;
  protocol: string;
  started_at: number;
  duration_seconds: number;
  user: {
    id: string;
    email: string;
    role: string;
  };
  bandwidth: {
    download_bytes: number;
    upload_bytes: number;
    download_rate_bps: number;
    upload_rate_bps: number;
  };
  connection_status: string;
}

interface ConsoleResourceBarProps {
  /** From the WebSocket handshake; null when the bar looks the session up by instance. */
  sessionId: string | null;
  instanceName: string;
  /** The cluster the instance runs on. */
  host: string;
  project?: string;
  /** Connection status from the console/terminal page */
  connectionStatus: "connecting" | "connected" | "disconnected" | "error" | "loading";
  protocol?: "terminal" | "graphics" | "files" | "credentials";
  /** Compact layout for inline/floating console windows (smaller expanded height, 2-col grid) */
  compact?: boolean;
}

const PANEL_EXPANDED_KEY = "orcastra-resource-bar-expanded";

/** Seconds as `05m 32s`, or `01h 05m 32s` from an hour on. */
function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const parts: string[] = [];
  if (h > 0) parts.push(`${h.toString().padStart(2, "0")}h`);
  parts.push(`${m.toString().padStart(2, "0")}m`);
  parts.push(`${s.toString().padStart(2, "0")}s`);
  return parts.join(" ");
}

/** An IdP group name as a role label, ex; "authentik Admins" becomes "Admin". */
function formatRole(role: string): string {
  if (!role || role === "unknown") return "User";
  const cleaned = role
    .replace(/^authentik\s+/i, "")
    .replace(/s$/i, "");
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

export function ConsoleResourceBar({
  sessionId,
  instanceName,
  host,
  project = "default",
  connectionStatus,
  protocol = "terminal",
  compact = false,
}: ConsoleResourceBarProps) {
  const { data: authSession } = useSession();
  const { formatBytes, formatNetworkRate } = useByteFormat();
  const userEmail = authSession?.user?.email ?? "";
  const userRole = (authSession?.user as any)?.roles?.[0]
    ?? (authSession?.user as any)?.groups?.[0]
    ?? "unknown";

  const { data: instanceMetrics, isLoading: metricsLoading } = useInstanceMetrics(
    host,
    instanceName,
    project,
    connectionStatus === "connected"
  );

  const [expanded, setExpanded] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem(PANEL_EXPANDED_KEY) === "true";
  });

  const [liveDuration, setLiveDuration] = useState(0);
  const sessionStartRef = useRef<number | null>(null);
  // Earlier connected segments, so the duration adds up across reconnects.
  const accumulatedDurationRef = useRef(0);

  const [sessionStats, setSessionStats] = useState<SessionStats | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const toggleExpanded = useCallback(() => {
    setExpanded((prev) => {
      const next = !prev;
      localStorage.setItem(PANEL_EXPANDED_KEY, String(next));
      return next;
    });
  }, []);

  // Ctrl+I toggles the panel.
  // important: never while a terminal or the guest display has focus. Ctrl+I is Tab in a shell
  // and a key the guest's own programs use.
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target?.closest?.(".xterm, [role='application'], input, textarea, [contenteditable='true']")) return;
      if (e.ctrlKey && e.key === "i" && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        toggleExpanded();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleExpanded]);

  // Ticks every second while connected, adding up across reconnects.
  useEffect(() => {
    if (connectionStatus === "connected" && !sessionStartRef.current) {
      sessionStartRef.current = Date.now() / 1000;
    }

    if (connectionStatus === "disconnected" || connectionStatus === "error") {
      // Adds this segment to the total before stopping.
      if (sessionStartRef.current) {
        accumulatedDurationRef.current += (Date.now() / 1000) - sessionStartRef.current;
        sessionStartRef.current = null;
      }
      return;
    }

    const interval = setInterval(() => {
      if (sessionStartRef.current) {
        const currentSegment = (Date.now() / 1000) - sessionStartRef.current;
        setLiveDuration(accumulatedDurationRef.current + currentSegment);
      }
    }, 1000);

    return () => clearInterval(interval);
  }, [connectionStatus]);

  // Polls session stats every 5 seconds while connected: by session id when the handshake gave
  // one, otherwise the instance's latest active session (the graphics console has no id here).
  useEffect(() => {
    if (connectionStatus !== "connected") {
      return;
    }

    const fetchStats = async () => {
      // Skipped while the tab is hidden; the next visible poll catches up.
      if (document.hidden) return;
      try {
        const { authHeaders } = await import("@/lib/api");
        const headers = authHeaders();

        if (sessionId) {
          const response = await fetch(
            `/api/v1/console/session/${encodeURIComponent(sessionId)}/stats`,
            { headers }
          );
          if (response.ok) {
            setSessionStats(await response.json());
          }
        } else {
          const params = new URLSearchParams({
            instance_name: instanceName,
            host: host,
          });
          const response = await fetch(
            `/api/v1/console/sessions/active?${params}`,
            { headers }
          );
          if (response.ok) {
            const sessions = await response.json();
            if (sessions.length > 0) {
              const latest = [...sessions].sort(
                (a: SessionStats, b: SessionStats) => b.started_at - a.started_at
              )[0];
              setSessionStats(latest);
            }
          }
        }
      } catch {
        // Stats are optional: the duration falls back to the local counter.
      }
    };

    fetchStats();
    pollIntervalRef.current = setInterval(fetchStats, 5000);

    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, [sessionId, connectionStatus, instanceName, host]);

  const isConnected = connectionStatus === "connected";
  const isConnecting = connectionStatus === "connecting" || connectionStatus === "loading";
  const displayDuration = sessionStats?.duration_seconds ?? liveDuration;
  const displayRole = formatRole(
    sessionStats?.user?.role || userRole || "unknown"
  );
  const displayEmail = sessionStats?.user?.email || userEmail || "";
  // The display session is named as the Sessions page lists it, so the bar and the table agree.
  const displayProtocol =
    protocol === "graphics"
      ? sessionProtocolLabel(GRAPHICS_SESSION_PROTOCOL)
      : protocol === "files" ? "SFTP" : protocol === "credentials" ? "LXD exec" : "WebSocket";

  const dlRate = sessionStats?.bandwidth?.download_rate_bps ?? 0;
  const ulRate = sessionStats?.bandwidth?.upload_rate_bps ?? 0;
  const dlTotal = sessionStats?.bandwidth?.download_bytes ?? 0;
  const ulTotal = sessionStats?.bandwidth?.upload_bytes ?? 0;

  const StatusIcon = isConnected ? Wifi : isConnecting ? Loader2 : WifiOff;
  // The session vocabulary's tones (components/terminal/session-status.tsx). `terminal-status`
  // rebinds them to their dark values, which this fixed dark bar needs in either page theme.
  // Disconnected is not an error.
  const statusColor = cn(
    "terminal-status",
    isConnected
      ? "text-status-online"
      : isConnecting
      ? "text-status-pending"
      : connectionStatus === "error"
      ? "text-status-offline"
      : "text-status-unknown",
  );
  const statusLabel = isConnected
    ? "Connected"
    : isConnecting
    ? "Connecting"
    : connectionStatus === "error"
    ? "Error"
    : "Disconnected";

  const expandedHeight = compact ? 160 : 220;

  return (
    <div
      className={cn(
        "bg-terminal-deep border-t border-terminal-border transition-all duration-200 ease-in-out select-none",
        "flex flex-col"
      )}
      style={{ height: expanded ? expandedHeight : 40, minHeight: 40 }}
    >
      <div
        className={cn(
          // One line at any width: below sm the duration and the rates live in the expanded panel.
          "flex items-center h-10 min-h-[40px] gap-3 overflow-hidden whitespace-nowrap cursor-pointer hover:bg-terminal-raised/60",
          compact ? "px-2" : "px-3"
        )}
        onClick={toggleExpanded}
        title="Ctrl+I to toggle resource panel"
      >
        <button
          className="text-gray-500 hover:text-gray-300 transition-colors"
          aria-label={expanded ? "Collapse panel" : "Expand panel"}
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronUp className="h-4 w-4" />
          )}
        </button>

        <div className="flex items-center gap-1.5">
          <StatusIcon
            className={cn("h-3.5 w-3.5", statusColor, isConnecting && "animate-spin")}
          />
          <span className={cn("text-xs font-medium", statusColor)}>
            {statusLabel}
          </span>
        </div>

        <div className="h-3 w-px shrink-0 bg-terminal-border" />

        <div className="hidden items-center gap-1 text-xs text-gray-400 sm:flex">
          <Clock className="h-3 w-3" />
          <span className="font-mono">{formatDuration(displayDuration)}</span>
        </div>

        <div className="hidden h-3 w-px bg-terminal-border sm:block" />

        {instanceMetrics && <InstanceResourceChips metrics={instanceMetrics} />}

        <div className="hidden items-center gap-2 text-xs text-gray-400 sm:flex">
          <Network className="h-3 w-3" aria-hidden="true" />
          {/* Neutral colour, since the arrow shows only a direction. */}
          <span className="text-gray-300 font-mono">↓{formatNetworkRate(dlRate, 1)}</span>
          <span className="text-gray-300 font-mono">↑{formatNetworkRate(ulRate, 1)}</span>
        </div>

        <div className="flex-1" />


      </div>

      {expanded && (
        <div className={cn(
          "flex-1 overflow-auto px-4 py-3 gap-4 text-xs",
          compact ? "grid grid-cols-2" : "grid grid-cols-3"
        )}>
          <div className="space-y-2">
            <h4 className="text-gray-400 font-semibold uppercase tracking-wider text-[10px]">
              Connection Info
            </h4>
            <div className="space-y-1.5 bg-terminal rounded-lg p-3">
              <InfoRow label="Status" value={statusLabel} valueClass={statusColor} />
              <InfoRow
                label="Duration"
                value={formatDuration(displayDuration)}
                mono
              />
              <InfoRow label="Protocol" value={displayProtocol} />
              <InfoRow label="Session" value={sessionId ?? EMPTY_VALUE} mono />
              <div className="border-t border-terminal-border my-1.5 pt-1.5">
                <InfoRow
                  label="Download"
                  value={`${formatNetworkRate(dlRate, 1)} (${formatBytes(dlTotal, 1)} total)`}
                />
                <InfoRow
                  label="Upload"
                  value={`${formatNetworkRate(ulRate, 1)} (${formatBytes(ulTotal, 1)} total)`}
                />
              </div>
            </div>
          </div>

          <div className="space-y-2">
            <h4 className="text-gray-400 font-semibold uppercase tracking-wider text-[10px]">
              User Info
            </h4>
            <div className="space-y-1.5 bg-terminal rounded-lg p-3">
              <InfoRow label="Email" value={displayEmail || EMPTY_VALUE} />
              <InfoRow label="Role" value={displayRole} />
              <InfoRow label="Instance" value={instanceName} mono />
              <InfoRow label="Cluster" value={host} mono />
              <InfoRow label="Project" value={project} />
            </div>
          </div>

          <div className="space-y-2">
            <h4 className="text-gray-400 font-semibold uppercase tracking-wider text-[10px]">
              Instance Resources
            </h4>
            <div className="space-y-2 bg-terminal rounded-lg p-3">
              <InstanceResourcesPanel metrics={instanceMetrics} loading={metricsLoading} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function InfoRow({
  label,
  value,
  valueClass,
  mono = false,
}: {
  label: string;
  value: string;
  valueClass?: string;
  mono?: boolean;
}) {
  return (
    <div className="flex justify-between items-center">
      <span className="text-gray-500">{label}</span>
      {/* The reveal is an overlay because the panel is pinned over a live console and cannot
          grow. Callers pass the full value: OverflowText cuts and reveals it, and a pre-cut
          string would only echo itself. */}
      <span className={cn("max-w-[200px] text-gray-300", valueClass)}>
        <OverflowText value={value} label={label.toLowerCase()} mode="overlay" tone="chrome" mono={mono} />
      </span>
    </div>
  );
}
