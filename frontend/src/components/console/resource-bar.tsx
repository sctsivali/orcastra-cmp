/**
 * Console Resource Bar: collapsible bottom status bar for console/terminal pages.
 *
 * Displays real-time session intelligence:
 * - Connection status & duration (live counter)
 * - User info & access role
 * - Session bandwidth (download/upload rates + totals)
 * - Instance resource usage (CPU, memory, storage, network)
 *
 * Two modes:
 * - Compact (40px): Single-line summary with key metrics
 * - Expanded (~220px): Full detail view with progress bars and gauges
 *
 * Toggle with click or keyboard shortcut (Ctrl+I).
 * Panel state persisted in localStorage.
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
import { cn, formatBytes, formatNetworkRate } from "@/lib/utils";
import { useSession } from "next-auth/react";
import { useInstanceMetrics } from "@/hooks";
import { OverflowText } from "@/components/ui/overflow-text";
import { InstanceResourceChips, InstanceResourcesPanel } from "./instance-resources";

// ============ Types ============

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
  /** Session ID for tracking (from WebSocket handshake) */
  sessionId: string | null;
  /** Instance name */
  instanceName: string;
  /** Host node name */
  host: string;
  /** LXD project */
  project?: string;
  /** Connection status from the console/terminal page */
  connectionStatus: "connecting" | "connected" | "disconnected" | "error" | "loading";
  /** Protocol type */
  protocol?: "terminal" | "graphics" | "files";
  /** Compact layout for inline/floating console windows (smaller expanded height, 2-col grid) */
  compact?: boolean;
}

// ============ Storage key for panel state ============
const PANEL_EXPANDED_KEY = "orcastra-resource-bar-expanded";

// ============ Utility functions ============

/** Format seconds as live duration string (00h 15m 32s) */
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

/** Format role name for display */
function formatRole(role: string): string {
  if (!role || role === "unknown") return "User";
  // Clean up authentik group names (e.g., "authentik Admins" -> "Admin")
  const cleaned = role
    .replace(/^authentik\s+/i, "")
    .replace(/s$/i, ""); // remove trailing 's'
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

// ============ Component ============

export function ConsoleResourceBar({
  sessionId,
  instanceName,
  host,
  project = "default",
  connectionStatus,
  protocol = "terminal",
  compact = false,
}: ConsoleResourceBarProps) {
  // ---- Self-contained data fetching ----
  // User info from NextAuth session (available via AuthProvider in root layout)
  const { data: authSession } = useSession();
  const userEmail = authSession?.user?.email ?? "";
  const userRole = (authSession?.user as any)?.roles?.[0]
    ?? (authSession?.user as any)?.groups?.[0]
    ?? "unknown";

  // Instance metrics via existing React Query hook (available via QueryClientProvider in root layout)
  const { data: instanceMetrics, isLoading: metricsLoading } = useInstanceMetrics(
    host,
    instanceName,
    project,
    connectionStatus === "connected"
  );

  // Panel expanded state (persisted in localStorage)
  const [expanded, setExpanded] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem(PANEL_EXPANDED_KEY) === "true";
  });

  // Live duration counter
  const [liveDuration, setLiveDuration] = useState(0);
  const sessionStartRef = useRef<number | null>(null);
  // Accumulate duration across reconnect cycles
  const accumulatedDurationRef = useRef(0);

  // Session stats from backend polling
  const [sessionStats, setSessionStats] = useState<SessionStats | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Toggle expanded state
  const toggleExpanded = useCallback(() => {
    setExpanded((prev) => {
      const next = !prev;
      localStorage.setItem(PANEL_EXPANDED_KEY, String(next));
      return next;
    });
  }, []);

  // Keyboard shortcut: Ctrl+I to toggle.
  // important: never while a terminal or the guest display has focus. Ctrl+I is Tab in a shell
  // and a key the guest's own programs use; taking it there broke both.
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

  // Live duration counter - update every second, accumulates across reconnects
  useEffect(() => {
    if (connectionStatus === "connected" && !sessionStartRef.current) {
      sessionStartRef.current = Date.now() / 1000;
    }

    if (connectionStatus === "disconnected" || connectionStatus === "error") {
      // Accumulate the duration from this session segment before stopping
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

  // Poll session stats from backend (every 5s when connected).
  // Two strategies: (1) direct session ID lookup, (2) active sessions fallback (for SPICE/graphics mode
  // where  sessionId is not available from the frontend WebSocket handshake).
  useEffect(() => {
    if (connectionStatus !== "connected") {
      return;
    }

    const fetchStats = async () => {
      // Nobody reads the bar of a hidden tab; the next visible beat catches up.
      if (document.hidden) return;
      try {
        const { getAccessToken } = await import("@/lib/api");
        const token = getAccessToken();
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;

        if (sessionId) {
          // Direct lookup by session ID (terminal / text mode)
          const response = await fetch(
            `/api/v1/console/session/${encodeURIComponent(sessionId)}/stats`,
            { headers }
          );
          if (response.ok) {
            setSessionStats(await response.json());
          }
        } else {
          // Fallback: look up active session by instance name + host (SPICE/graphics mode)
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
            // Pick the most recent active session for this instance
            if (sessions.length > 0) {
              const latest = [...sessions].sort(
                (a: SessionStats, b: SessionStats) => b.started_at - a.started_at
              )[0];
              setSessionStats(latest);
            }
          }
        }
      } catch {
        // Silently fail - stats are nice-to-have, not critical
      }
    };

    // Fetch immediately, then every 5 seconds
    fetchStats();
    pollIntervalRef.current = setInterval(fetchStats, 5000);

    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, [sessionId, connectionStatus, instanceName, host]);

  // Derive display values
  const isConnected = connectionStatus === "connected";
  const isConnecting = connectionStatus === "connecting" || connectionStatus === "loading";
  const displayDuration = sessionStats?.duration_seconds ?? liveDuration;
  const displayRole = formatRole(
    sessionStats?.user?.role || userRole || "unknown"
  );
  const displayEmail = sessionStats?.user?.email || userEmail || "";
  const displayProtocol = protocol === "graphics" ? "SPICE (VGA)" : protocol === "files" ? "SFTP" : "WebSocket";

  // Bandwidth from session stats
  const dlRate = sessionStats?.bandwidth?.download_rate_bps ?? 0;
  const ulRate = sessionStats?.bandwidth?.upload_rate_bps ?? 0;
  const dlTotal = sessionStats?.bandwidth?.download_bytes ?? 0;
  const ulTotal = sessionStats?.bandwidth?.upload_bytes ?? 0;

  // Connection status indicator
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

  // Expanded panel height adapts to context
  const expandedHeight = compact ? 160 : 220;

  return (
    <div
      className={cn(
        "bg-terminal-deep border-t border-terminal-border transition-all duration-200 ease-in-out select-none",
        "flex flex-col"
      )}
      style={{ height: expanded ? expandedHeight : 40, minHeight: 40 }}
    >
      {/* ====== Compact Bar (always visible) ====== */}
      <div
        className={cn(
          // One line at any width: below sm the duration and the rates live in the expanded panel.
          "flex items-center h-10 min-h-[40px] gap-3 overflow-hidden whitespace-nowrap cursor-pointer hover:bg-terminal-raised/60",
          compact ? "px-2" : "px-3"
        )}
        onClick={toggleExpanded}
        title="Ctrl+I to toggle resource panel"
      >
        {/* Expand/Collapse indicator */}
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

        {/* Connection status */}
        <div className="flex items-center gap-1.5">
          <StatusIcon
            className={cn("h-3.5 w-3.5", statusColor, isConnecting && "animate-spin")}
          />
          <span className={cn("text-xs font-medium", statusColor)}>
            {statusLabel}
          </span>
        </div>

        <div className="h-3 w-px shrink-0 bg-terminal-border" />

        {/* Duration */}
        <div className="hidden items-center gap-1 text-xs text-gray-400 sm:flex">
          <Clock className="h-3 w-3" />
          <span className="font-mono">{formatDuration(displayDuration)}</span>
        </div>

        <div className="hidden h-3 w-px bg-terminal-border sm:block" />

        {instanceMetrics && <InstanceResourceChips metrics={instanceMetrics} />}

        {/* Network bandwidth */}
        <div className="hidden items-center gap-2 text-xs text-gray-400 sm:flex">
          <Network className="h-3 w-3" aria-hidden="true" />
          {/* A direction, not a state: the arrow carries it. */}
          <span className="text-gray-300 font-mono">↓{formatNetworkRate(dlRate, 1)}</span>
          <span className="text-gray-300 font-mono">↑{formatNetworkRate(ulRate, 1)}</span>
        </div>

        <div className="flex-1" />


      </div>

      {/* ====== Expanded Detail Panel ====== */}
      {expanded && (
        <div className={cn(
          "flex-1 overflow-auto px-4 py-3 gap-4 text-xs",
          compact ? "grid grid-cols-2" : "grid grid-cols-3"
        )}>
          {/* Section 1: Connection Info */}
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
              <InfoRow label="Session" value={sessionId ?? "-"} mono />
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

          {/* Section 2: User Info */}
          <div className="space-y-2">
            <h4 className="text-gray-400 font-semibold uppercase tracking-wider text-[10px]">
              User Info
            </h4>
            <div className="space-y-1.5 bg-terminal rounded-lg p-3">
              <InfoRow label="Email" value={displayEmail || "—"} />
              <InfoRow label="Role" value={displayRole} />
              <InfoRow label="Instance" value={instanceName} mono />
              <InfoRow label="Cluster" value={host} mono />
              <InfoRow label="Project" value={project} />
            </div>
          </div>

          {/* Section 3: Instance Resources */}
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

// ============ Sub-components ============

/** Single row in the expanded info sections */
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
      {/* important: this used to be title={value} on the element rendering {value}, so the
          reveal repeated whatever it was given. Its Session caller passed a 12 character slice,
          which made the tooltip echo the cut string rather than reveal the rest. The panel is
          pinned over a live console and cannot grow, so the reveal is an overlay. */}
      <span className={cn("max-w-[200px] text-gray-300", valueClass)}>
        <OverflowText value={value} label={label.toLowerCase()} mode="overlay" tone="chrome" mono={mono} />
      </span>
    </div>
  );
}
