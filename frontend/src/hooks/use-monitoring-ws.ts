/**
 * WebSocket-backed monitoring hook.
 * Receives real-time dashboard pushes from the backend metrics collector.
 * Falls back to REST polling when WS is unavailable.
 */

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { getAccessToken } from "@/lib/api";
import { getActorQueryScope } from "@/hooks/use-query-scope";
import { Role, resolveRoles } from "@/types/rbac";
import type { MonitoringDashboardResponse } from "@/types";

const WS_RECONNECT_BASE_MS = 2000;
const WS_RECONNECT_MAX_MS = 30000;

/**
 * Hook that subscribes to the monitoring WebSocket.
 * When data arrives it injects it into the React Query cache under
 * the current actor's monitoring dashboard key, keeping the existing query consumers in sync.
 *
 * Returns { connected } so callers know when to enable polling fallback.
 */
export function useMonitoringWebSocket(enabled: boolean = true) {
  const queryClient = useQueryClient();
  const { data: session } = useSession();
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);

  const user = session?.user as any;
  const groups = Array.isArray(user?.groups) ? user.groups : [];
  const roles = resolveRoles(groups);
  const isAdmin = roles.includes(Role.ADMIN);
  const scopeKey = getActorQueryScope(session);

  useEffect(() => {
    let active = true;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;

    setConnected(false);
    if (!enabled || !isAdmin) {
      return () => {
        active = false;
      };
    }

    const clearReconnect = () => {
      if (reconnectTimer !== undefined) {
        clearTimeout(reconnectTimer);
        reconnectTimer = undefined;
      }
    };

    const scheduleReconnect = () => {
      if (!active) return;
      clearReconnect();
      const delay = Math.min(
        WS_RECONNECT_BASE_MS * 2 ** attempt,
        WS_RECONNECT_MAX_MS,
      );
      attempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = undefined;
        if (active) connect();
      }, delay);
    };

    const connect = () => {
      if (!active) return;

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
        socket = ws;
        wsRef.current = ws;

        ws.onopen = () => {
          if (!active || socket !== ws) return;
          attempt = 0;
          setConnected(true);
        };

        ws.onmessage = (event) => {
          if (!active || socket !== ws) return;
          try {
            const data: MonitoringDashboardResponse = JSON.parse(event.data);
            // Inject into React Query cache - all useMonitoringDashboard consumers update instantly
            queryClient.setQueryData(["monitoring", "dashboard", scopeKey], data);
          } catch {
            // ignore malformed messages
          }
        };

        ws.onclose = () => {
          if (!active || socket !== ws) return;
          socket = null;
          if (wsRef.current === ws) wsRef.current = null;
          setConnected(false);
          scheduleReconnect();
        };

        ws.onerror = () => {
          if (active && socket === ws) ws.close();
        };
      } catch {
        scheduleReconnect();
      }
    };

    connect();

    return () => {
      active = false;
      clearReconnect();
      const ws = socket;
      socket = null;
      if (wsRef.current === ws) wsRef.current = null;
      if (ws) {
        // Closing is intentional during Pause, role/session changes, and unmount. Remove the
        // old callbacks first so a late close event cannot arm another reconnect timer.
        ws.onopen = null;
        ws.onmessage = null;
        ws.onclose = null;
        ws.onerror = null;
        try {
          ws.close();
        } catch {
          // The socket may already have completed its close handshake.
        }
      }
    };
  }, [enabled, isAdmin, queryClient, scopeKey]);

  return { connected };
}
