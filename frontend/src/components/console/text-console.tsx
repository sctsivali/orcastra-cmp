/**
 * Text Console - xterm.js wrapper for shell access.
 * Provides interactive terminal access to containers and VMs.
 */

"use client";

import * as React from "react";
import { useEffect, useRef, useCallback, forwardRef, useImperativeHandle } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { SearchAddon } from "@xterm/addon-search";
import { useConsoleStore } from "@/lib/console-store";
import "@xterm/xterm/css/xterm.css";

interface TextConsoleProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  onReady?: () => void;
  onError?: (error: string) => void;
  onClose?: () => void;
}

export interface TextConsoleHandle {
  search: (term: string, findNext?: boolean) => void;
  focus: () => void;
  paste: (text: string) => void;
}

// WebSocket URL builder - shared utility for proxy-safe URLs
import { getTerminalWsUrl } from "@/lib/ws-url";

export const TextConsole = forwardRef<TextConsoleHandle, TextConsoleProps>(function TextConsole({
  consoleId,
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
  const maxReconnectAttempts = 10;
  const isMountedRef = useRef(true);
  const isConnectingRef = useRef(false);
  const isInitializedRef = useRef(false); // Track if already initialized
  
  // Store callbacks in refs to avoid dependency changes
  const onReadyRef = useRef(onReady);
  const onErrorRef = useRef(onError);
  const onCloseRef = useRef(onClose);
  
  // Update refs when callbacks change
  useEffect(() => {
    onReadyRef.current = onReady;
    onErrorRef.current = onError;
    onCloseRef.current = onClose;
  }, [onReady, onError, onClose]);

  // Use stable store actions (get them once, they don't change)
  const updateConnectionStatus = useConsoleStore((s) => s.updateConnectionStatus);
  const setConsoleSessionId = useConsoleStore((s) => s.setConsoleSessionId);

  // Initialize terminal
  const initTerminal = useCallback(() => {
    if (!terminalRef.current || xtermRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "block",
      fontSize: 14,
      fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", Consolas, monospace',
      theme: {
        background: "#1a1b26",
        foreground: "#a9b1d6",
        cursor: "#c0caf5",
        cursorAccent: "#1a1b26",
        selectionBackground: "#33467c",
        selectionForeground: "#c0caf5",
        black: "#32344a",
        red: "#f7768e",
        green: "#9ece6a",
        yellow: "#e0af68",
        blue: "#7aa2f7",
        magenta: "#ad8ee6",
        cyan: "#449dab",
        white: "#787c99",
        brightBlack: "#444b6a",
        brightRed: "#ff7a93",
        brightGreen: "#b9f27c",
        brightYellow: "#ff9e64",
        brightBlue: "#7da6ff",
        brightMagenta: "#bb9af7",
        brightCyan: "#0db9d7",
        brightWhite: "#acb0d0",
      },
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

    // Welcome message
    const boxWidth = 50;
    const innerWidth = boxWidth - 4;
    const title = "Orcastra Console";
    const connectText = `Connecting to ${instanceName} ...`;
    const titlePadding = Math.max(0, innerWidth - title.length);
    const connectPadding = Math.max(0, innerWidth - connectText.length);
    const topBorder = "┌" + "─".repeat(boxWidth - 2) + "┐";
    const bottomBorder = "└" + "─".repeat(boxWidth - 2) + "┘";
    
    term.writeln("\x1b[1;34m" + topBorder + "\x1b[0m");
    term.writeln("\x1b[1;34m│\x1b[0m \x1b[1;36m" + title + "\x1b[0m" + " ".repeat(titlePadding + 1) + "\x1b[1;34m│\x1b[0m");
    term.writeln("\x1b[1;34m│\x1b[0m " + connectText + " ".repeat(connectPadding + 1) + "\x1b[1;34m│\x1b[0m");
    term.writeln("\x1b[1;34m" + bottomBorder + "\x1b[0m");
    term.writeln("");

    return term;
  }, [instanceName]);

  // Connect WebSocket
  const connect = useCallback(() => {
    // Prevent connection if component is unmounted or already connecting/connected
    if (!isMountedRef.current) return;
    if (isConnectingRef.current) return;
    if (wsRef.current?.readyState === WebSocket.OPEN || 
        wsRef.current?.readyState === WebSocket.CONNECTING) return;

    isConnectingRef.current = true;
    const wsUrl = getTerminalWsUrl(instanceName, host, project);
    updateConnectionStatus(consoleId, "connecting");
    
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      isConnectingRef.current = false;
      if (!isMountedRef.current) {
        ws.close(1000, "Component unmounted");
        return;
      }
      reconnectAttempts.current = 0;
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
            setConsoleSessionId(consoleId, msg.session_id);
            xtermRef.current?.writeln(`\x1b[1;32m✓ Session established: ${msg.session_id}\x1b[0m`);
            break;

          case "ready":
            updateConnectionStatus(consoleId, "connected");
            xtermRef.current?.writeln("\x1b[1;32m✓ Terminal ready\x1b[0m\n");
            reconnectAttempts.current = 0;
            onReadyRef.current?.();
            break;

          case "output":
            if (msg.encoding === "base64") {
              const decoded = atob(msg.data);
              xtermRef.current?.write(decoded);
            } else {
              xtermRef.current?.write(msg.data);
            }
            break;

          case "error":
            xtermRef.current?.writeln(`\x1b[1;31m✗ Error: ${msg.message}\x1b[0m`);
            updateConnectionStatus(consoleId, "error", msg.message);
            onErrorRef.current?.(msg.message);
            break;

          case "closed":
            xtermRef.current?.writeln("\x1b[1;33m⚠ Connection closed\x1b[0m");
            updateConnectionStatus(consoleId, "disconnected");
            onCloseRef.current?.();
            break;
        }
      } catch {
        // Binary data - write directly
        xtermRef.current?.write(event.data);
      }
    };

    ws.onerror = (error) => {
      isConnectingRef.current = false;
      // Skip error handling if component is unmounted (expected during cleanup)
      if (!isMountedRef.current) return;
      // Only log error once, don't spam console
      if (reconnectAttempts.current === 0) {
        console.error("[TextConsole] WebSocket error:", error);
      }
      updateConnectionStatus(consoleId, "error", "Connection error");
    };

    ws.onclose = (event) => {
      isConnectingRef.current = false;
      // Skip close handling if component is unmounted (expected during cleanup)
      if (!isMountedRef.current) return;
      updateConnectionStatus(consoleId, "disconnected");
      
      // Only attempt reconnect if not a clean close and within retry limit
      // Use longer initial delay to avoid rapid reconnection loops
      if (!event.wasClean && reconnectAttempts.current < maxReconnectAttempts) {
        reconnectAttempts.current++;
        // Start with 2 seconds, then exponential backoff up to 30 seconds
        const delay = Math.min(2000 * Math.pow(2, reconnectAttempts.current - 1), 30000);
        xtermRef.current?.writeln(`\x1b[1;33m⚠ Reconnecting in ${delay / 1000}s (attempt ${reconnectAttempts.current}/${maxReconnectAttempts})...\x1b[0m`);
        
        reconnectTimeoutRef.current = setTimeout(connect, delay);
      } else if (reconnectAttempts.current >= maxReconnectAttempts) {
        xtermRef.current?.writeln(`\x1b[1;31m✗ Max reconnection attempts reached. Please try reconnecting manually.\x1b[0m`);
      }
    };
  }, [consoleId, instanceName, host, project, updateConnectionStatus, setConsoleSessionId]);

  // Expose methods
  useImperativeHandle(ref, () => ({
    search: (term: string, findNext = true) => {
      if (findNext) {
        searchAddonRef.current?.findNext(term);
      } else {
        searchAddonRef.current?.findPrevious(term);
      }
    },
    focus: () => {
      xtermRef.current?.focus();
    },
    paste: (text: string) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "input", data: text }));
      }
    },
  }), []);

  // Handle resize
  useEffect(() => {
    const handleResize = () => {
      fitAddonRef.current?.fit();
    };

    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  // Initialize and connect with proper mounting guard
  // Only run once per consoleId to prevent reconnection loops
  useEffect(() => {
    // Skip if already initialized for this session
    if (isInitializedRef.current) return;
    
    isMountedRef.current = true;
    isConnectingRef.current = false;
    isInitializedRef.current = true;
    
    initTerminal();
    
    // Small delay before connecting to avoid race conditions with React StrictMode
    // and tab switching. This prevents "WebSocket closed before established" error.
    const connectTimer = setTimeout(() => {
      if (isMountedRef.current) {
        connect();
      }
    }, 100);

    return () => {
      isMountedRef.current = false;
      isInitializedRef.current = false;
      clearTimeout(connectTimer);
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close(1000, "Component unmounting");
        wsRef.current = null;
      }
      xtermRef.current?.dispose();
      xtermRef.current = null;
      isConnectingRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [consoleId]);

  // Fit on container resize
  useEffect(() => {
    const observer = new ResizeObserver(() => {
      fitAddonRef.current?.fit();
    });

    if (terminalRef.current) {
      observer.observe(terminalRef.current);
    }

    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={terminalRef}
      className="w-full h-full min-h-[300px] bg-[#1a1b26]"
      style={{ padding: "4px" }}
    />
  );
});
