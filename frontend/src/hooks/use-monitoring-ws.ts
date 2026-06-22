/**
 * WebSocket-backed monitoring hook.
 * Receives real-time dashboard pushes from the backend metrics collector.
 * Falls back to REST polling when WS is unavailable.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { getMonitoringWsUrl } from "@/lib/ws-url";
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

  const connect = useCallback(async () => {
    if (!enabled || !isAdmin) return;

    // Mint a single-use monitoring ticket via the authenticated API. On failure
    // (ex; a not-yet-hydrated session token) fall back to the existing backoff.
    let wsUrl: string;
    try {
      wsUrl = await getMonitoringWsUrl();
    } catch {
      scheduleReconnect();
      return;
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
