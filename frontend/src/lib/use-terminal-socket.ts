/**
 * useTerminalSession: one interactive text session (an exec shell or the serial console) drawn
 * in xterm.
 *
 * Wire protocol (backend terminal_proxy.py / console_proxy.py): binary frames are raw PTY bytes
 * both ways, so xterm's streaming decoder stitches characters split across frames; text frames
 * are JSON control (resize, ack, ping out; session, ready, error, closed, pong in).
 *
 * How a session ended decides what follows: 1000 (the shell exited) waits for the operator, Enter
 * starts a new session; 4404 is final. After the instance powered down (1012) or a live session
 * dropped, "not running" (4404) means "not yet": it tries every few seconds until the instance is
 * back, so the serial console follows a reboot. Everything else goes through lib/socket-recovery.ts:
 * a refused ticket or a 4401 refreshes the session once (or signs in again), 4403 / 4428 / 4423 stop
 * and say why, and a dropped network or a 1011 reconnects with backoff, at once when the network or
 * tab returns.
 */

"use client";

import { useCallback, useEffect, useRef } from "react";
import type { Terminal } from "@xterm/xterm";
import { drawConnectBox, terminalSafe } from "@/lib/terminal-banner";
import { createXterm, terminalFontReady, type XtermBundle } from "@/lib/xterm-setup";
import {
  INSTANCE_RETURN_POLL_MS,
  INSTANCE_RETURN_WINDOW_MS,
  STABLE_SESSION_MS,
  WAITING_FOR_INSTANCE,
  WS_CLOSE,
  ticketedSocketUrl,
  type WsTicketRequest,
} from "@/lib/ws-url";
import { SOCKET_MESSAGES, messageFor } from "@/lib/ws-close";
import { SocketRecovery, type RecoveryStep } from "@/lib/socket-recovery";

export type TerminalKind = "shell" | "serial";
export type TerminalSessionStatus = "connecting" | "connected" | "reconnecting" | "ended" | "error";
/** What the operator can do about an error: sign in again (the session ended). */
export interface TerminalStatusHint {
  signIn?: boolean;
}

export interface UseTerminalSessionOptions {
  containerRef: React.RefObject<HTMLDivElement | null>;
  kind: TerminalKind;
  instanceName: string;
  host: string;
  project: string;
  enabled?: boolean;
  fontSize?: number;
  onStatus?: (status: TerminalSessionStatus, detail?: string, hint?: TerminalStatusHint) => void;
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
// ACK parsed output in batches during a flood; the tail is always ACKed when output goes idle.
const ACK_THRESHOLD = 256 * 1024;
const PING_EVERY_MS = 25000;
const PONG_DEADLINE_MS = 70000;
const RESIZE_DEBOUNCE_MS = 100;

const DIM = "\x1b[2m";
const YELLOW = "\x1b[33m";
const RED = "\x1b[31m";
const GREEN = "\x1b[1;32m";
const RESET = "\x1b[0m";
// A session id only ever goes to the screen in this shape, whatever the server sends.
const SESSION_ID = /^[\w.:-]{1,128}$/;
// Abort any half-read sequence, then end mouse reporting, bracketed paste and a hidden cursor. A
// dropped vim leaves all of that on, and the new shell would start inside it.
const END_DEAD_PROGRAM = "\x18\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[?25h\x1b[0m";
// important: only from the alternate screen. On the normal one this restores the saved cursor,
// which is the top left corner, and the next line overwrites the connect box.
const LEAVE_ALT_SCREEN = "\x1b[?1049l";

function note(term: Terminal | undefined, text: string, color = DIM): void {
  term?.write(`\r\n${color}${terminalSafe(text)}${RESET}\r\n`);
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
  const recoveryRef = useRef<SocketRecovery | null>(null);
  recoveryRef.current ??= new SocketRecovery();
  // A session in this view reached "ready" at least once (so a sign-in that ended is not automatic).
  const liveRef = useRef(false);
  // A live session dropped and is being replaced: the next one is a new shell, and says so.
  const replacingRef = useRef(false);
  // Bumped by every connect and by an operator's reconnect, so a slow refresh cannot act on a newer one.
  const generationRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedByUsRef = useRef(false);
  // When a live session dropped or its instance powered down; null while connected.
  const droppedAtRef = useRef<number | null>(null);
  const readyAtRef = useRef<number | null>(null);
  // Why the server refused the session (its error frame); the close follows at once and reports it.
  const refusalRef = useRef<{ message: string; code?: number } | null>(null);
  const lastPongRef = useRef(Date.now());
  const pendingRef = useRef<Uint8Array[]>([]);
  const writingRef = useRef(false);
  const ackRef = useRef(0);
  const sessionsRef = useRef(0);
  const connectRef = useRef<() => void>(() => {});
  const callbacks = useRef(options);
  callbacks.current = options;

  const setStatus = useCallback((status: TerminalSessionStatus, detail?: string, hint?: TerminalStatusHint) => {
    statusRef.current = status;
    callbacks.current.onStatus?.(status, detail, hint);
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

  // Carry out what the recovery decided, unless the view closed or reconnected meanwhile.
  const apply = useCallback((step: RecoveryStep, generation: number) => {
    if (closedByUsRef.current || generation !== generationRef.current) return;
    const term = bundleRef.current?.term;
    if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    retryTimerRef.current = null;
    switch (step.action) {
      case "retry":
        setStatus("reconnecting", step.message || undefined);
        if (step.message) note(term, step.message, YELLOW);
        retryTimerRef.current = setTimeout(() => connectRef.current(), step.delayMs);
        return;
      case "leaving":
        finalRef.current = true;
        setStatus("connecting", step.message);
        note(term, step.message, YELLOW);
        return;
      case "stop":
        // Enter tries again only after giving up on a network; a refusal would be refused again.
        finalRef.current = !step.retry;
        setStatus("error", step.message, step.signIn ? { signIn: true } : undefined);
        note(term, step.message, RED);
        return;
      case "own":
        return;
    }
  }, [setStatus]);

  const connect = useCallback(async () => {
    if (closedByUsRef.current) return;
    const state = wsRef.current?.readyState;
    if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) return;
    if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    retryTimerRef.current = null;
    finalRef.current = false;
    const recovery = recoveryRef.current!;
    const generation = ++generationRef.current;
    if (recovery.used === 0 && statusRef.current !== "reconnecting") setStatus("connecting");

    const request: WsTicketRequest = kind === "serial"
      ? { kind: "console", host, project, instance: instanceName, console_type: "console" }
      : { kind: "terminal", host, project, instance: instanceName };
    let url: string;
    try {
      url = await ticketedSocketUrl(request);
    } catch (error) {
      if (closedByUsRef.current || generation !== generationRef.current) return;
      apply(await recovery.onMintError(error, { liveSocket: liveRef.current }), generation);
      return;
    }
    if (closedByUsRef.current || generation !== generationRef.current) return;

    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    lastPongRef.current = Date.now();
    let opened = false;

    ws.onopen = () => {
      opened = true;
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
        case "session": {
          const term = bundleRef.current?.term;
          const sessionId = String(msg.session_id ?? "");
          // note: not on serial. That line is the guest's own console, and the program is still
          // running behind it.
          if (kind === "shell" && sessionsRef.current > 0) {
            term?.write(END_DEAD_PROGRAM);
            if (term?.buffer.active.type === "alternate") term.write(LEAVE_ALT_SCREEN);
          }
          sessionsRef.current += 1;
          if (SESSION_ID.test(sessionId)) term?.writeln(`${GREEN}✓ Session established: ${sessionId}${RESET}`);
          callbacks.current.onSessionId?.(sessionId);
          break;
        }
        case "ready":
          readyAtRef.current = Date.now();
          droppedAtRef.current = null;
          recoveryRef.current?.ready();
          liveRef.current = true;
          if (replacingRef.current) note(bundleRef.current?.term, SOCKET_MESSAGES.newShell, YELLOW);
          replacingRef.current = false;
          bundleRef.current?.term.writeln(`${GREEN}✓ Connected${RESET}`);
          // note: nothing is typed for the operator. On a serial line an Enter could pick a boot
          // menu entry.
          if (kind === "serial") bundleRef.current?.term.writeln(`${DIM}Press Enter for a prompt.${RESET}`);
          setStatus("connected");
          callbacks.current.onReady?.();
          break;
        case "error":
          // note: kept for the close, so it prints once, and in full where a close reason may be cut.
          refusalRef.current = { message: String(msg.message), code: typeof msg.code === "number" ? msg.code : undefined };
          break;
        case "pong":
          lastPongRef.current = Date.now();
          break;
        default:
          break;
      }
    };

    ws.onclose = async (event) => {
      if (wsRef.current === ws) wsRef.current = null;
      if (closedByUsRef.current || generation !== generationRef.current) return;
      const term = bundleRef.current?.term;
      const errorFrame = refusalRef.current;
      const reason = errorFrame?.message || event.reason || "";
      refusalRef.current = null;
      const readyAt = readyAtRef.current;
      readyAtRef.current = null;
      if (readyAt !== null && Date.now() - readyAt >= STABLE_SESSION_MS) recovery.stable();
      const wasLive = statusRef.current === "connected";
      if (wasLive || event.code === WS_CLOSE.POWERED_DOWN) droppedAtRef.current ??= Date.now();
      const droppedAt = droppedAtRef.current;
      if (
        (event.code === WS_CLOSE.POWERED_DOWN || event.code === WS_CLOSE.UNAVAILABLE) &&
        droppedAt !== null &&
        Date.now() - droppedAt < INSTANCE_RETURN_WINDOW_MS
      ) {
        // Said once as the wait begins, not on every poll while the instance is down.
        if (wasLive || event.code === WS_CLOSE.POWERED_DOWN) note(term, `${reason || "The instance went away"}. Reconnecting when it starts again…`, YELLOW);
        setStatus("reconnecting", WAITING_FOR_INSTANCE);
        retryTimerRef.current = setTimeout(() => connectRef.current(), INSTANCE_RETURN_POLL_MS);
        return;
      }
      if (event.code === WS_CLOSE.NORMAL) {
        recovery.restart();
        setStatus("ended", reason || "The session ended");
        note(term, `${reason || "The session ended"}. Press Enter to start a new one.`);
        return;
      }
      if (wasLive) replacingRef.current = true;
      const step = await recovery.onClose(event.code, reason, { opened, errorFrame, liveSocket: liveRef.current });
      if (step.action === "own") {
        // 4404 outside the return window, 4409, or a power-down: final, in the server's words.
        finalRef.current = true;
        const message = messageFor(step.kind, reason);
        setStatus("error", message);
        note(term, message, RED);
        return;
      }
      apply(step, generation);
    };
  }, [apply, flush, host, instanceName, kind, project, setStatus]);

  connectRef.current = () => void connect();

  const reconnect = useCallback(() => {
    recoveryRef.current?.restart();
    generationRef.current += 1;
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
    recoveryRef.current?.restart();
    let disposed = false;
    const cleanups: Array<() => void> = [];

    void (async () => {
      await terminalFontReady(fontSize);
      if (disposed || !containerRef.current) return;
      const bundle = createXterm(containerRef.current, { fontSize });
      bundleRef.current = bundle;
      const term = bundle.term;
      // Before the first row is written, so the box takes the window's real size.
      bundle.fitNow();
      const connectBox = drawConnectBox(
        term,
        kind === "serial" ? "Orcastra Serial Console" : "Orcastra Terminal",
        `Connecting to ${instanceName}…`,
      );
      cleanups.push(() => connectBox.dispose());

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
        // A wait for the backoff, or a give-up after five tries, ends when the network or tab returns.
        if (statusRef.current === "reconnecting" || (statusRef.current === "error" && !finalRef.current)) {
          recoveryRef.current?.restart();
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
