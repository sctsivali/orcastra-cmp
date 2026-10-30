/**
 * useTerminalSession: one interactive text session (an exec shell or the serial console) drawn
 * in xterm.
 *
 * Wire protocol (backend terminal_proxy.py / console_proxy.py): binary frames are raw PTY bytes
 * both ways, so xterm's streaming decoder stitches characters split across frames; text frames
 * are JSON control (resize, ack, ping out; session, ready, error, closed, pong in).
 *
 * How a session ended decides what follows: 1000 (the shell exited, the VM stopped) waits for the
 * operator, Enter starts a new session; 4403 / 4404 / 1008 are final; anything else (a dropped
 * network, a backend restart) reconnects with backoff, at once when the network or tab returns.
 */

"use client";

import { useCallback, useEffect, useRef } from "react";
import type { Terminal } from "@xterm/xterm";
import { createXterm, terminalFontReady, type XtermBundle } from "@/lib/xterm-setup";
import { ACCESS_REVOKED_MESSAGE, WS_CLOSE, mintFailureMessage, ticketedSocketUrl, type WsTicketRequest } from "@/lib/ws-url";

export type TerminalKind = "shell" | "serial";
export type TerminalSessionStatus = "connecting" | "connected" | "reconnecting" | "ended" | "error";

export interface UseTerminalSessionOptions {
  containerRef: React.RefObject<HTMLDivElement | null>;
  kind: TerminalKind;
  instanceName: string;
  host: string;
  project: string;
  enabled?: boolean;
  fontSize?: number;
  onStatus?: (status: TerminalSessionStatus, detail?: string) => void;
  onSessionId?: (sessionId: string) => void;
  onReady?: () => void;
}

export interface TerminalSessionHandle {
  search: (term: string, findNext?: boolean) => boolean;
  focus: () => void;
  /** Paste text the way a paste does (bracketed when the program asked for it). */
  paste: (text: string) => void;
  /** Send bytes as typed (an escape sequence from the on-screen keyboard). */
  sendRaw: (data: string) => void;
  fit: () => void;
  setFontSize: (px: number) => number;
  reconnect: () => void;
  copySelection: () => Promise<boolean>;
}

const encoder = new TextEncoder();
const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000, 30000, 30000];
// ACK parsed output in batches during a flood; the tail is always ACKed when output goes idle.
const ACK_THRESHOLD = 256 * 1024;
const PING_EVERY_MS = 25000;
const PONG_DEADLINE_MS = 70000;
const RESIZE_DEBOUNCE_MS = 100;
const FINAL_CODES = new Set<number>([WS_CLOSE.ACCESS_REVOKED, WS_CLOSE.UNAVAILABLE, WS_CLOSE.POLICY_VIOLATION]);

const DIM = "\x1b[2m";
const RESET = "\x1b[0m";

function note(term: Terminal | undefined, text: string, color = DIM): void {
  term?.write(`\r\n${color}${text}${RESET}\r\n`);
}

function welcome(term: Terminal, kind: TerminalKind, instanceName: string): void {
  term.writeln(`\x1b[1;36m${kind === "serial" ? "Serial console" : "Shell"}: ${instanceName}\x1b[0m`);
  // note: nothing is typed for the operator. On a serial line an Enter could pick a boot menu entry.
  term.writeln(`${DIM}${kind === "serial" ? "Connecting… Press Enter for a prompt once connected." : "Connecting…"}${RESET}`);
}

async function copyText(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

export function useTerminalSession(options: UseTerminalSessionOptions): TerminalSessionHandle {
  const { containerRef, kind, instanceName, host, project, enabled = true, fontSize } = options;

  const bundleRef = useRef<XtermBundle | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const statusRef = useRef<TerminalSessionStatus>("connecting");
  const finalRef = useRef(false);
  const attemptsRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedByUsRef = useRef(false);
  const lastPongRef = useRef(Date.now());
  const pendingRef = useRef<Uint8Array[]>([]);
  const writingRef = useRef(false);
  const ackRef = useRef(0);
  const connectRef = useRef<() => void>(() => {});
  const callbacks = useRef(options);
  callbacks.current = options;

  const setStatus = useCallback((status: TerminalSessionStatus, detail?: string) => {
    statusRef.current = status;
    callbacks.current.onStatus?.(status, detail);
  }, []);

  const sendJson = useCallback((body: unknown) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(body));
  }, []);

  // Write queued output as few writes as possible, then ACK what xterm parsed.
  const flush = useCallback(() => {
    if (writingRef.current) return;
    const term = bundleRef.current?.term;
    const queued = pendingRef.current;
    if (!term || queued.length === 0) return;
    pendingRef.current = [];
    let chunk = queued[0];
    if (queued.length > 1) {
      let total = 0;
      for (const c of queued) total += c.length;
      chunk = new Uint8Array(total);
      let offset = 0;
      for (const c of queued) {
        chunk.set(c, offset);
        offset += c.length;
      }
    }
    writingRef.current = true;
    term.write(chunk, () => {
      writingRef.current = false;
      ackRef.current += chunk.length;
      const more = pendingRef.current.length > 0;
      if ((ackRef.current >= ACK_THRESHOLD || !more) && ackRef.current > 0) {
        sendJson({ type: "ack", bytes: ackRef.current });
        ackRef.current = 0;
      }
      if (more) flush();
    });
  }, [sendJson]);

  const scheduleRetry = useCallback((reason: string) => {
    if (closedByUsRef.current) return;
    if (attemptsRef.current >= RETRY_DELAYS_MS.length) {
      finalRef.current = false;
      setStatus("error", reason);
      note(bundleRef.current?.term, `${reason}. Press Enter to try again.`, "\x1b[31m");
      return;
    }
    const delay = RETRY_DELAYS_MS[attemptsRef.current] + Math.floor(Math.random() * 400);
    attemptsRef.current += 1;
    setStatus("reconnecting", reason);
    note(bundleRef.current?.term, `${reason}. Reconnecting in ${Math.round(delay / 1000)} s…`, "\x1b[33m");
    retryTimerRef.current = setTimeout(() => connectRef.current(), delay);
  }, [setStatus]);

  const connect = useCallback(async () => {
    if (closedByUsRef.current) return;
    const state = wsRef.current?.readyState;
    if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) return;
    if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    retryTimerRef.current = null;
    finalRef.current = false;
    if (attemptsRef.current === 0) setStatus("connecting");

    const request: WsTicketRequest = kind === "serial"
      ? { kind: "console", host, project, instance: instanceName, console_type: "console" }
      : { kind: "terminal", host, project, instance: instanceName };
    let url: string;
    try {
      url = await ticketedSocketUrl(request);
    } catch (error) {
      const status = (error as { status?: number })?.status;
      const message = mintFailureMessage(error);
      if (status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 429) {
        finalRef.current = true;
        setStatus("error", message);
        note(bundleRef.current?.term, message, "\x1b[31m");
        return;
      }
      scheduleRetry(message);
      return;
    }
    if (closedByUsRef.current) return;

    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    lastPongRef.current = Date.now();

    ws.onopen = () => {
      ackRef.current = 0;
      const term = bundleRef.current?.term;
      ws.send(JSON.stringify({ width: term?.cols ?? 120, height: term?.rows ?? 40, binary: true, flow: true }));
    };

    ws.onmessage = (event) => {
      if (typeof event.data !== "string") {
        pendingRef.current.push(new Uint8Array(event.data as ArrayBuffer));
        flush();
        return;
      }
      let msg: { type?: string; [key: string]: unknown };
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      switch (msg.type) {
        case "session":
          callbacks.current.onSessionId?.(String(msg.session_id));
          break;
        case "ready":
          attemptsRef.current = 0;
          setStatus("connected");
          callbacks.current.onReady?.();
          break;
        case "error":
          note(bundleRef.current?.term, String(msg.message), "\x1b[31m");
          break;
        case "pong":
          lastPongRef.current = Date.now();
          break;
        default:
          break;
      }
    };

    ws.onclose = (event) => {
      if (wsRef.current === ws) wsRef.current = null;
      if (closedByUsRef.current) return;
      const term = bundleRef.current?.term;
      const reason = event.reason || "";
      if (event.code === WS_CLOSE.NORMAL) {
        attemptsRef.current = 0;
        setStatus("ended", reason || "The session ended");
        note(term, `${reason || "The session ended"}. Press Enter to start a new one.`);
        return;
      }
      if (FINAL_CODES.has(event.code)) {
        finalRef.current = true;
        const message = event.code === WS_CLOSE.ACCESS_REVOKED ? ACCESS_REVOKED_MESSAGE : reason || "The session cannot start";
        setStatus("error", message);
        note(term, message, "\x1b[31m");
        return;
      }
      scheduleRetry(reason || "The connection dropped");
    };
  }, [flush, host, instanceName, kind, project, scheduleRetry, setStatus]);

  connectRef.current = () => void connect();

  const reconnect = useCallback(() => {
    attemptsRef.current = 0;
    const ws = wsRef.current;
    if (ws && ws.readyState <= WebSocket.OPEN) {
      wsRef.current = null;
      ws.onclose = null;
      ws.close(1000, "Reconnect");
    }
    note(bundleRef.current?.term, "Starting a new session…");
    void connect();
  }, [connect]);

  useEffect(() => {
    if (!enabled || !containerRef.current) return;
    if (bundleRef.current) return; // StrictMode double-invoke guard
    closedByUsRef.current = false;
    attemptsRef.current = 0;
    let disposed = false;
    const cleanups: Array<() => void> = [];

    void (async () => {
      await terminalFontReady(fontSize);
      if (disposed || !containerRef.current) return;
      const bundle = createXterm(containerRef.current, { fontSize });
      bundleRef.current = bundle;
      const term = bundle.term;
      welcome(term, kind, instanceName);

      // Ctrl+Shift+C / Ctrl+Shift+V (and Ctrl/Shift+Insert) copy and paste, the convention of
      // Linux terminals; plain Ctrl+C stays an interrupt for the program.
      term.attachCustomKeyEventHandler((event) => {
        if (event.type !== "keydown") return true;
        const copy = (event.ctrlKey && event.shiftKey && event.code === "KeyC") || (event.ctrlKey && event.code === "Insert");
        const paste = (event.ctrlKey && event.shiftKey && event.code === "KeyV") || (event.shiftKey && event.code === "Insert");
        if (copy) {
          event.preventDefault();
          void copyText(term.getSelection());
          return false;
        }
        if (paste) {
          event.preventDefault();
          navigator.clipboard?.readText?.().then((text) => text && term.paste(text)).catch(() => {});
          return false;
        }
        return true;
      });

      const dataSub = term.onData((data) => {
        const status = statusRef.current;
        if ((status === "ended" || (status === "error" && !finalRef.current)) && (data === "\r" || data === "\n")) {
          reconnect();
          return;
        }
        const ws = wsRef.current;
        if (ws?.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
      });
      let resizeTimer: ReturnType<typeof setTimeout> | null = null;
      const resizeSub = term.onResize(({ cols, rows }) => {
        if (resizeTimer) clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => sendJson({ type: "resize", width: cols, height: rows }), RESIZE_DEBOUNCE_MS);
      });
      const observer = new ResizeObserver(() => bundle.fit());
      observer.observe(containerRef.current);

      const heartbeat = setInterval(() => {
        const ws = wsRef.current;
        if (ws?.readyState !== WebSocket.OPEN) return;
        // A connection that stopped answering (laptop sleep, a dead NAT entry) is replaced.
        if (Date.now() - lastPongRef.current > PONG_DEADLINE_MS) {
          ws.close(4000, "No answer");
          return;
        }
        sendJson({ type: "ping" });
      }, PING_EVERY_MS);
      const wake = () => {
        if (document.hidden) return;
        if (statusRef.current === "reconnecting") {
          attemptsRef.current = Math.min(attemptsRef.current, 1);
          void connect();
        } else {
          sendJson({ type: "ping" });
        }
      };
      document.addEventListener("visibilitychange", wake);
      window.addEventListener("online", wake);

      cleanups.push(() => {
        dataSub.dispose();
        resizeSub.dispose();
        if (resizeTimer) clearTimeout(resizeTimer);
        observer.disconnect();
        clearInterval(heartbeat);
        document.removeEventListener("visibilitychange", wake);
        window.removeEventListener("online", wake);
      });

      void connect();
    })();

    return () => {
      disposed = true;
      closedByUsRef.current = true;
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      for (const cleanup of cleanups) cleanup();
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws) {
        ws.onclose = null;
        ws.close(1000, "Closed by the operator");
      }
      pendingRef.current = [];
      bundleRef.current?.dispose();
      bundleRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, instanceName, host, project, kind]);

  const search = useCallback((query: string, findNext = true) => {
    const addon = bundleRef.current?.searchAddon;
    if (!addon || !query) return false;
    return findNext ? addon.findNext(query) : addon.findPrevious(query);
  }, []);
  const focus = useCallback(() => bundleRef.current?.term.focus(), []);
  const paste = useCallback((text: string) => bundleRef.current?.term.paste(text), []);
  const sendRaw = useCallback((data: string) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
  }, []);
  const fit = useCallback(() => bundleRef.current?.fit(), []);
  const setFontSize = useCallback((px: number) => bundleRef.current?.setFontSize(px) ?? px, []);
  const copySelection = useCallback(() => copyText(bundleRef.current?.term.getSelection() ?? ""), []);

  return { search, focus, paste, sendRaw, fit, setFontSize, reconnect, copySelection };
}
