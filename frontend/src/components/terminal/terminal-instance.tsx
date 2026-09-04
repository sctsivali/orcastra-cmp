/**
 * Terminal Instance - xterm.js wrapper component.
 * Handles the actual terminal rendering and WebSocket communication.
 */

"use client";

import * as React from "react";
import { useEffect, useRef, useCallback, forwardRef, useImperativeHandle } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { SearchAddon } from "@xterm/addon-search";
import { useTerminalStore, TerminalStatus } from "@/lib/terminal-store";
import "@xterm/xterm/css/xterm.css";
import { TERMINAL_THEME } from "@/lib/terminal-theme";

interface TerminalInstanceProps {
  terminalId: string;
  instanceName: string;
  host: string;
  project: string;
  onReady?: () => void;
  onError?: (error: string) => void;
  onClose?: () => void;
}

// Expose search and focus methods to parent
export interface TerminalInstanceHandle {
  search: (term: string, findNext?: boolean) => void;
  focus: () => void;
}

// WebSocket URL builder - shared utility for proxy-safe URLs
import { getTerminalWsUrl } from "@/lib/ws-url";

export const TerminalInstance = forwardRef<TerminalInstanceHandle, TerminalInstanceProps>(function TerminalInstance({
  terminalId,
  instanceName,
  host,
  project,
  onReady,
  onError,
  onClose,
}, ref) {
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const searchAddonRef = useRef<SearchAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const reconnectAttempts = useRef(0);
  const maxReconnectAttempts = 10; // Increased for better resilience

  const { updateTerminalStatus, setTerminalSessionId } = useTerminalStore();

  // Initialize terminal
  const initTerminal = useCallback(() => {
    if (!terminalRef.current || xtermRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "block",
      fontSize: 14,
      fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", Consolas, monospace',
      theme: TERMINAL_THEME,
      allowProposedApi: true,
      scrollback: 10000,
      convertEol: true,
    });

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon();
    const searchAddon = new SearchAddon();

    term.loadAddon(fitAddon);
    term.loadAddon(webLinksAddon);
    term.loadAddon(searchAddon);

    term.open(terminalRef.current);
    fitAddon.fit();

    xtermRef.current = term;
    fitAddonRef.current = fitAddon;
    searchAddonRef.current = searchAddon;

    // Handle user input
    term.onData((data) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "input", data }));
      }
    });

    // Handle resize
    term.onResize(({ cols, rows }) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "resize", width: cols, height: rows }));
      }
    });

    // Draw properly aligned welcome box with fixed-width monospace calculation
    // Use exact character counting for proper alignment
    const boxWidth = 50;
    const innerWidth = boxWidth - 4; // 2 for borders, 2 for padding
    
    const title = "Orcastra Terminal";
    const connectText = `Connecting to ${instanceName} ...`;
    
    // Calculate exact padding needed
    const titlePadding = Math.max(0, innerWidth - title.length);
    const connectPadding = Math.max(0, innerWidth - connectText.length);
    
    const topBorder = "┌" + "─".repeat(boxWidth - 2) + "┐";
    const bottomBorder = "└" + "─".repeat(boxWidth - 2) + "┘";
    
    // Build lines with exact character counts
    term.writeln("\x1b[1;34m" + topBorder + "\x1b[0m");
    term.writeln("\x1b[1;34m│\x1b[0m \x1b[1;36m" + title + "\x1b[0m" + " ".repeat(titlePadding + 1) + "\x1b[1;34m│\x1b[0m");
    term.writeln("\x1b[1;34m│\x1b[0m " + connectText + " ".repeat(connectPadding + 1) + "\x1b[1;34m│\x1b[0m");
    term.writeln("\x1b[1;34m" + bottomBorder + "\x1b[0m");
    term.writeln("");

    return term;
  }, [instanceName]);

  // Connect WebSocket
  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const wsUrl = getTerminalWsUrl(instanceName, host, project);

    updateTerminalStatus(terminalId, "connecting");
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      reconnectAttempts.current = 0;

      // Send initial size
      if (xtermRef.current) {
        const { cols, rows } = xtermRef.current;
        ws.send(JSON.stringify({ width: cols, height: rows }));
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);

        switch (msg.type) {
          case "session":
            setTerminalSessionId(terminalId, msg.session_id);
            xtermRef.current?.writeln(`\x1b[1;32m✓ Session established: ${msg.session_id}\x1b[0m`);
            break;

          case "ready":
            updateTerminalStatus(terminalId, "connected");
            xtermRef.current?.writeln("\x1b[1;32m✓ Terminal ready\x1b[0m\n");
            // Reset reconnect counter on successful connection
            reconnectAttempts.current = 0;
            onReady?.();
            break;

          case "output":
            if (msg.encoding === "base64") {
              // Decode base64 data
              const decoded = atob(msg.data);
              xtermRef.current?.write(decoded);
            } else {
              xtermRef.current?.write(msg.data);
            }
            break;

          case "error":
            xtermRef.current?.writeln(`\x1b[1;31m✗ Error: ${msg.message}\x1b[0m`);
            updateTerminalStatus(terminalId, "error", msg.message);
            onError?.(msg.message);
            break;

          case "closed":
            xtermRef.current?.writeln("\n\x1b[1;33m⚠ Connection closed by server\x1b[0m");
            updateTerminalStatus(terminalId, "disconnected");
            onClose?.();
            break;

          case "ping":
            // Server ping - respond immediately with pong
            if (wsRef.current?.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({ type: "pong" }));
            }
            break;

          case "pong":
            // Server responded to our ping - connection is alive
            break;

          default:
            // Unknown message type - ignore silently
            break;
        }
      } catch (e) {
        // Raw text message
        xtermRef.current?.write(event.data);
      }
    };

    ws.onerror = (error) => {
      // Only log error once per connection attempt to avoid spamming console
      if (reconnectAttempts.current === 0) {
        console.error("[Terminal] WebSocket error:", error);
      }
      xtermRef.current?.writeln("\x1b[1;31m✗ Connection error\x1b[0m");
    };

    ws.onclose = (event) => {
      // Reconnect for abnormal closures (not user-initiated)
      // code 1000 = normal closure, 1001 = going away
      if (event.code !== 1000 && event.code !== 1001 && reconnectAttempts.current < maxReconnectAttempts) {
        reconnectAttempts.current++;
        // Start with 2s delay, then increase: 2s, 4s, 6s, 8s... max 10s
        const delay = Math.min(2000 * reconnectAttempts.current, 10000);
        xtermRef.current?.writeln(
          `\x1b[1;33m⚠ Reconnecting in ${(delay / 1000).toFixed(1)}s... (attempt ${reconnectAttempts.current}/${maxReconnectAttempts})\x1b[0m`
        );
        updateTerminalStatus(terminalId, "connecting");

        reconnectTimeoutRef.current = setTimeout(() => {
          connect();
        }, delay);
      } else if (reconnectAttempts.current >= maxReconnectAttempts) {
        xtermRef.current?.writeln("\x1b[1;31m✗ Max reconnection attempts reached. Click to retry.\x1b[0m");
        updateTerminalStatus(terminalId, "error", "Connection failed after multiple attempts");
      }
    };
  }, [terminalId, instanceName, host, project, updateTerminalStatus, setTerminalSessionId, onReady, onError, onClose]);

  // Handle container resize
  const handleResize = useCallback(() => {
    if (fitAddonRef.current && xtermRef.current) {
      try {
        fitAddonRef.current.fit();
      } catch (e) {
        console.warn("[Terminal] Fit error:", e);
      }
    }
  }, []);

  // Search functionality
  const search = useCallback((term: string, findNext = true) => {
    if (searchAddonRef.current) {
      if (findNext) {
        searchAddonRef.current.findNext(term);
      } else {
        searchAddonRef.current.findPrevious(term);
      }
    }
  }, []);

  // Focus terminal
  const focus = useCallback(() => {
    xtermRef.current?.focus();
  }, []);

  // Expose search and focus methods to parent via ref
  useImperativeHandle(ref, () => ({
    search,
    focus,
  }), [search, focus]);

  // Track if initialized
  const initializedRef = useRef(false);

  // Initialize on mount
  useEffect(() => {
    // Prevent double initialization in React Strict Mode
    if (initializedRef.current) return;
    initializedRef.current = true;

    initTerminal();
    connect();

    // Handle window resize
    window.addEventListener("resize", handleResize);

    // Heartbeat to keep connection alive (every 30 seconds)
    // This is a lightweight keepalive - actual connection health is managed by
    // the WebSocket library's native ping/pong at the protocol level
    const heartbeatInterval = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        try {
          wsRef.current.send(JSON.stringify({ type: "ping" }));
        } catch (e) {
          console.warn("[Terminal] Failed to send heartbeat", e);
        }
      }
    }, 30000);

    // Send ping when terminal regains focus after being idle
    // This helps "wake up" the connection immediately when user returns
    const handleVisibilityChange = () => {
      if (!document.hidden && wsRef.current?.readyState === WebSocket.OPEN) {
        // Send immediate ping to wake up connection
        wsRef.current.send(JSON.stringify({ type: "ping" }));
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    // Also send ping when window gets focus (more reliable than visibilitychange)
    const handleWindowFocus = () => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "ping" }));
      }
    };
    window.addEventListener("focus", handleWindowFocus);

    return () => {
      window.removeEventListener("resize", handleResize);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("focus", handleWindowFocus);
      clearInterval(heartbeatInterval);

      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }

      if (wsRef.current) {
        wsRef.current.close(1000, "Component unmounted");
        wsRef.current = null;
      }

      if (xtermRef.current) {
        xtermRef.current.dispose();
        xtermRef.current = null;
      }
      
      // Reset initialization flag on unmount
      initializedRef.current = false;
    };
  }, []); // Empty deps - only run once on mount

  // Expose resize handler for parent
  useEffect(() => {
    // Create resize observer for container
    const resizeObserver = new ResizeObserver(() => {
      handleResize();
    });

    if (terminalRef.current) {
      resizeObserver.observe(terminalRef.current);
    }

    return () => {
      resizeObserver.disconnect();
    };
  }, [handleResize]);

  return (
    <div
      ref={terminalRef}
      className="w-full h-full bg-terminal rounded-b-lg overflow-hidden"
      onClick={focus}
    />
  );
});

export default TerminalInstance;
