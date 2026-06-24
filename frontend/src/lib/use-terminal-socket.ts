/**
 * useTerminalSession - the single owner of an interactive terminal session.
 *
 * Creates the xterm instance (via the shared factory), wires keyboard/resize IO,
 * and runs the WebSocket transport against the backend `/terminal/ws` endpoint.
 *
 * Wire protocol (matched by backend/app/routers/terminal.py):
 *   - Binary frames  = raw PTY bytes. Output is written to xterm verbatim (its
 *     streaming UTF-8 decoder stitches codepoints split across frames); input is
 *     sent as binary so there is no JSON/base64 tax on the hot path.
 *   - Text frames    = JSON control: session / ready / error / closed / ping /
 *     pong / resize, plus the initial {width,height} sizing message.
 */

"use client";

import { useCallback, useEffect, useRef } from "react";
import type { Terminal } from "@xterm/xterm";
import { createXterm, type XtermBundle } from "@/lib/xterm-setup";
import { getTerminalWsUrl } from "@/lib/ws-url";

export type TerminalStatus = "connecting" | "connected" | "disconnected" | "error";

export interface UseTerminalSessionOptions {
  containerRef: React.RefObject<HTMLDivElement | null>;
  instanceName: string;
  host: string;
  project: string;
  welcomeTitle?: string;
  enabled?: boolean;
  fontSize?: number;
  scrollback?: number;
  onStatus?: (status: TerminalStatus, detail?: string) => void;
  onSessionId?: (sessionId: string) => void;
  onReady?: () => void;
  onError?: (message: string) => void;
  onClose?: () => void;
}

export interface TerminalSessionHandle {
  search: (term: string, findNext?: boolean) => void;
  focus: () => void;
  paste: (text: string) => void;
  fit: () => void;
  /** Set the terminal font size in px (clamped), re-fitting the grid. Returns the
   *  size actually applied. This is how zoom works - no CSS scaling. */
  setFontSize: (px: number) => number;
}

// Reused across the whole app - allocating one encoder per keystroke is wasteful.
const encoder = new TextEncoder();
const MAX_RECONNECT_ATTEMPTS = 10;
// Output flow control: ACK parsed bytes back to the backend so it keeps its send
// window open. Batch ACKs to ~256 KB during a flood (one ACK per coalesced write would
// otherwise be one per keystroke echo); the tail is always ACKed once output goes idle.
const ACK_THRESHOLD = 256 * 1024;

function writeWelcome(term: Terminal, title: string, instanceName: string): void {
  const boxWidth = 50;
  const innerWidth = boxWidth - 4;
  const connectText = `Connecting to ${instanceName} ...`;
  const titlePad = Math.max(0, innerWidth - title.length);
  const connectPad = Math.max(0, innerWidth - connectText.length);
  const top = "┌" + "─".repeat(boxWidth - 2) + "┐";
  const bottom = "└" + "─".repeat(boxWidth - 2) + "┘";
  term.writeln("\x1b[1;34m" + top + "\x1b[0m");
  term.writeln("\x1b[1;34m│\x1b[0m \x1b[1;36m" + title + "\x1b[0m" + " ".repeat(titlePad + 1) + "\x1b[1;34m│\x1b[0m");
  term.writeln("\x1b[1;34m│\x1b[0m " + connectText + " ".repeat(connectPad + 1) + "\x1b[1;34m│\x1b[0m");
  term.writeln("\x1b[1;34m" + bottom + "\x1b[0m");
  term.writeln("");
}

export function useTerminalSession(opts: UseTerminalSessionOptions): TerminalSessionHandle {
  const {
    containerRef,
    instanceName,
    host,
    project,
    welcomeTitle = "Orcastra Terminal",
    enabled = true,
    fontSize,
    scrollback,
  } = opts;

  const bundleRef = useRef<XtermBundle | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttempts = useRef(0);
  // Set on teardown so an in-flight (re)connect does not race the cleanup.
  const manualCloseRef = useRef(false);
  // connect is recreated when params change; reconnect timers call the latest via this ref.
  const connectRef = useRef<() => void>(() => {});

  // Output flow control state. pending = frames awaiting a write; writing = a
  // term.write callback is still outstanding; ackPending = parsed bytes not yet ACKed.
  const pendingRef = useRef<Uint8Array[]>([]);
  const writingRef = useRef(false);
  const ackPendingRef = useRef(0);

  // Keep the latest callbacks without re-running the lifecycle effect.
  const cbRef = useRef(opts);
  cbRef.current = opts;

  const getTerm = () => bundleRef.current?.term ?? null;
  const setStatus = (s: TerminalStatus, detail?: string) => cbRef.current.onStatus?.(s, detail);

  // Drain queued PTY output into xterm in as few writes as possible, then ACK the
  // parsed bytes. Writing only when the previous write has been parsed (the term.write
  // callback) means a flood coalesces into a handful of large writes - and, paired with
  // the backend watermark, bounds how much output the browser ever buffers. Idle and
  // interactive output still writes immediately, so echo latency is unaffected.
  const flushOutput = useCallback(() => {
    if (writingRef.current) return;
    const term = bundleRef.current?.term;
    if (!term) {
      pendingRef.current = [];
      return;
    }
    const queued = pendingRef.current;
    if (queued.length === 0) return;
    pendingRef.current = [];

    let chunk: Uint8Array;
    if (queued.length === 1) {
      chunk = queued[0];
    } else {
      let total = 0;
      for (const c of queued) total += c.length;
      chunk = new Uint8Array(total);
      let off = 0;
      for (const c of queued) {
        chunk.set(c, off);
        off += c.length;
      }
    }

    writingRef.current = true;
    term.write(chunk, () => {
      writingRef.current = false;
      ackPendingRef.current += chunk.length;
      const moreQueued = pendingRef.current.length > 0;
      // ACK once we cross the batch threshold, or when output goes idle so the
      // backend's flow-control window is never left half-closed on the tail.
      if (ackPendingRef.current >= ACK_THRESHOLD || !moreQueued) {
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN && ackPendingRef.current > 0) {
          ws.send(JSON.stringify({ type: "ack", bytes: ackPendingRef.current }));
          ackPendingRef.current = 0;
        }
      }
      if (moreQueued) flushOutput();
    });
  }, []);

  const scheduleReconnect = useCallback((notice: string) => {
    if (manualCloseRef.current || reconnectAttempts.current >= MAX_RECONNECT_ATTEMPTS) {
      if (reconnectAttempts.current >= MAX_RECONNECT_ATTEMPTS) {
        getTerm()?.writeln("\x1b[1;31m✗ Max reconnection attempts reached. Click to retry.\x1b[0m");
        setStatus("error", "Connection failed after multiple attempts");
      }
      return;
    }
    reconnectAttempts.current++;
    // Exponential-ish backoff capped at 10s, plus jitter to avoid thundering-herd reconnects.
    const base = Math.min(2000 * reconnectAttempts.current, 10000);
    const delay = base + Math.floor(Math.random() * 500);
    getTerm()?.writeln(
      `\x1b[1;33m⚠ ${notice} (attempt ${reconnectAttempts.current}/${MAX_RECONNECT_ATTEMPTS}, ${(delay / 1000).toFixed(1)}s)\x1b[0m`,
    );
    setStatus("connecting");
    reconnectTimerRef.current = setTimeout(() => connectRef.current(), delay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const connect = useCallback(async () => {
    if (manualCloseRef.current) return;
    const existing = wsRef.current?.readyState;
    if (existing === WebSocket.OPEN || existing === WebSocket.CONNECTING) return;

    setStatus("connecting");

    // Mint a fresh single-use ticket per (re)connect: keeps the JWT out of the URL
    // and transparently refreshes an expired session token before the socket opens.
    let url: string;
    try {
      url = await getTerminalWsUrl(instanceName, host, project);
    } catch {
      scheduleReconnect("Authenticating, retrying");
      return;
    }
    if (manualCloseRef.current) return;

    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      reconnectAttempts.current = 0;
      // Fresh backend proxy => its outstanding-byte counter starts at zero, so reset
      // ours. (A stray in-flight ACK on the old buffer is harmless - the backend clamps.)
      ackPendingRef.current = 0;
      const t = getTerm();
      if (t) ws.send(JSON.stringify({ width: t.cols, height: t.rows }));
    };

    ws.onmessage = (event) => {
      if (typeof event.data !== "string") {
        // Binary = raw PTY output. Queue it and let the flow-controlled writer drain
        // it (coalesced writes + ACKs bound the browser's memory under a flood).
        pendingRef.current.push(new Uint8Array(event.data as ArrayBuffer));
        flushOutput();
        return;
      }
      let msg: { type?: string; [k: string]: unknown };
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      switch (msg.type) {
        case "session":
          cbRef.current.onSessionId?.(String(msg.session_id));
          getTerm()?.writeln(`\x1b[1;32m✓ Session established: ${String(msg.session_id).slice(0, 8)}...\x1b[0m`);
          break;
        case "ready":
          setStatus("connected");
          getTerm()?.writeln("\x1b[1;32m✓ Terminal ready\x1b[0m\n");
          reconnectAttempts.current = 0;
          cbRef.current.onReady?.();
          break;
        case "error":
          getTerm()?.writeln(`\x1b[1;31m✗ Error: ${String(msg.message)}\x1b[0m`);
          setStatus("error", String(msg.message));
          cbRef.current.onError?.(String(msg.message));
          break;
        case "closed":
          getTerm()?.writeln("\n\x1b[1;33m⚠ Connection closed by server\x1b[0m");
          setStatus("disconnected");
          cbRef.current.onClose?.();
          break;
        case "ping":
          if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: "pong" }));
          }
          break;
        default:
          break;
      }
    };

    ws.onerror = () => {
      if (reconnectAttempts.current === 0) console.error("[Terminal] WebSocket error");
      getTerm()?.writeln("\x1b[1;31m✗ Connection error\x1b[0m");
    };

    ws.onclose = (event) => {
      // 1000 normal (shell exited) / 1001 going away: do not reconnect. Any other
      // code (1006 dropped, 1008 ticket rejected) is recoverable - mint a fresh ticket.
      if (manualCloseRef.current || event.code === 1000 || event.code === 1001) return;
      setStatus("disconnected");
      scheduleReconnect("Reconnecting");
    };
  }, [instanceName, host, project, scheduleReconnect, flushOutput]);

  connectRef.current = connect;

  useEffect(() => {
    if (!enabled || !containerRef.current) return;
    if (bundleRef.current) return; // StrictMode double-invoke guard

    manualCloseRef.current = false;
    reconnectAttempts.current = 0;

    const bundle = createXterm(containerRef.current, { fontSize, scrollback });
    bundleRef.current = bundle;
    const t = bundle.term;
    writeWelcome(t, welcomeTitle, instanceName);

    const dataDisp = t.onData((d) => {
      const ws = wsRef.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(encoder.encode(d));
    });
    const resizeDisp = t.onResize(({ cols, rows }) => {
      const ws = wsRef.current;
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "resize", width: cols, height: rows }));
      }
    });

    const onWindowResize = () => bundle.fit();
    window.addEventListener("resize", onWindowResize);
    const ro = new ResizeObserver(() => bundle.fit());
    ro.observe(containerRef.current);

    const heartbeat = setInterval(() => {
      const ws = wsRef.current;
      if (ws?.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: "ping" }));
        } catch {
          /* socket closing */
        }
      }
    }, 30000);
    const wake = () => {
      const ws = wsRef.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
    };
    const onVisible = () => {
      if (!document.hidden) wake();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", wake);

    connectRef.current();

    return () => {
      manualCloseRef.current = true;
      window.removeEventListener("resize", onWindowResize);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", wake);
      clearInterval(heartbeat);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      ro.disconnect();
      dataDisp.dispose();
      resizeDisp.dispose();
      if (wsRef.current) {
        wsRef.current.close(1000, "Component unmounted");
        wsRef.current = null;
      }
      bundle.dispose();
      bundleRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, instanceName, host, project]);

  const search = useCallback((q: string, findNext = true) => {
    const s = bundleRef.current?.searchAddon;
    if (!s) return;
    if (findNext) s.findNext(q);
    else s.findPrevious(q);
  }, []);
  const focus = useCallback(() => {
    bundleRef.current?.term.focus();
  }, []);
  const paste = useCallback((text: string) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(encoder.encode(text));
  }, []);
  const fit = useCallback(() => {
    bundleRef.current?.fit();
  }, []);
  const setFontSize = useCallback((px: number) => bundleRef.current?.setFontSize(px) ?? px, []);

  return { search, focus, paste, fit, setFontSize };
}
