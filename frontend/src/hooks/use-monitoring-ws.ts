/**
 * WebSocket-backed monitoring hook.
 * Receives real-time dashboard pushes from the backend metrics collector.
 * Falls back to REST polling when WS is unavailable.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { getAccessToken } from "@/lib/api";
import { Role, resolveRoles } from "@/types/rbac";
import type { MonitoringDashboardResponse } from "@/types";

const WS_RECONNECT_BASE_MS = 2000;
const WS_RECONNECT_MAX_MS = 30000;

/**
 * Hook that subscribes to the monitoring WebSocket.
 * When data arrives it injects it into the React Query cache under
 * ["monitoring", "dashboard"], keeping the existing query consumers in sync.
 *
 * Returns { connected } so callers know when to enable polling fallback.
 */
export function useMonitoringWebSocket(enabled: boolean = true) {
  const queryClient = useQueryClient();
  const { data: session } = useSession();
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const attemptRef = useRef(0);
  const [connected, setConnected] = useState(false);

  const user = session?.user as any;
  const groups = Array.isArray(user?.groups) ? user.groups : [];
  const roles = resolveRoles(groups);
  const isAdmin = roles.includes(Role.ADMIN);
  const scopeKey = `${user?.id ?? user?.email ?? "anonymous"}:${[...groups].sort().join(",")}`;

  const connect = useCallback(() => {
    if (!enabled || !isAdmin) return;

    // Build WS URL from NEXT_PUBLIC_API_URL (direct backend connection for WebSocket)
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";
    // Append auth token as query param (browsers can't set Authorization headers on WS)
    const token = getAccessToken();
    const tokenParam = token ? `?token=${encodeURIComponent(token)}` : "";
    let wsUrl: string;
    if (apiUrl) {
      const isSecure = apiUrl.startsWith("https://");
      const protocol = isSecure ? "wss:" : "ws:";
      const host = apiUrl.replace(/^https?:\/\//, "").replace(/\/+$/, "");
      wsUrl = `${protocol}//${host}/api/v1/monitoring/ws${tokenParam}`;
    } else {
      const isHttps = typeof window !== "undefined" && window.location.protocol === "https:";
      const protocol = isHttps ? "wss:" : "ws:";
      const hostname = typeof window !== "undefined" ? window.location.hostname : "localhost";
      wsUrl = `${protocol}//${hostname}:4050/api/v1/monitoring/ws${tokenParam}`;
    }

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
        setConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const data: MonitoringDashboardResponse = JSON.parse(event.data);
          // Inject into React Query cache - all useMonitoringDashboard consumers update instantly
          queryClient.setQueryData(["monitoring", "dashboard", scopeKey], data);
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = () => {
        setConnected(false);
        scheduleReconnect();
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch {
      scheduleReconnect();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, isAdmin, queryClient, scopeKey]);

  const scheduleReconnect = useCallback(() => {
    if (!enabled || !isAdmin) return;
    const delay = Math.min(
      WS_RECONNECT_BASE_MS * 2 ** attemptRef.current,
      WS_RECONNECT_MAX_MS,
    );
    attemptRef.current += 1;
    reconnectTimer.current = setTimeout(connect, delay);
  }, [enabled, isAdmin, connect]);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
      wsRef.current = null;
      setConnected(false);
    };
  }, [connect]);

  return { connected };
}
