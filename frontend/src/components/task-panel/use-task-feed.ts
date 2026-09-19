"use client";

/**
 * The task feed: one WebSocket per tab to /api/v1/tasks/ws, patched straight into the React
 * Query caches.
 *
 * A module-level store read through useSyncExternalStore (the lib/pending-creates.ts shape),
 * because two surfaces read it (the panel owns the socket, the Operations page only reads the
 * state) and neither may open a second socket.
 *
 * States: idle -> connecting -> connected -> reconnecting (attempt < 3) -> polling (attempt >= 3)
 * -> connected; paused once the tab has been hidden for 60 s (socket closed, reopened on
 * visible). Backoff 2 s doubling to 30 s. A 1008 close (authentication refused) stops every retry
 * until the session token changes; a token rotation while connected closes the socket for a
 * silent reconnect that carries the new token and counts as attempt 0.
 */

import { useEffect, useSyncExternalStore } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { getAccessToken } from "@/lib/api";
import { getTasksWsUrl } from "@/lib/ws-url";
import type { TaskFeedFrame } from "@/types/tasks";
import { appendLogInCaches, logKeys, taskKeys, upsertTaskInCaches } from "./hooks/use-tasks";

export type FeedState = "idle" | "connecting" | "connected" | "reconnecting" | "polling" | "paused";

export interface TaskFeedSnapshot {
  state: FeedState;
  attempt: number;
  nextRetryInSeconds: number | null;
  followerActive: boolean | null;
  serverOffsetMs: number;
  lastFrameAt: number | null;
}

const BACKOFF_BASE_MS = 2000;
const BACKOFF_MAX_MS = 30000;
const POLLING_AFTER_ATTEMPTS = 3;
const HIDDEN_CLOSE_MS = 60000;
const HIDDEN_FLUSH_MS = 50;
const CLOSE_POLICY_VIOLATION = 1008;
const FRAME_TYPES: ReadonlySet<string> = new Set(["hello", "task", "log", "follower", "resync"]);

const IDLE: TaskFeedSnapshot = {
  state: "idle",
  attempt: 0,
  nextRetryInSeconds: null,
  followerActive: null,
  serverOffsetMs: 0,
  lastFrameAt: null,
};

type Timer = ReturnType<typeof setTimeout>;
type Ticker = ReturnType<typeof setInterval>;

let snapshot: TaskFeedSnapshot = IDLE;
const listeners = new Set<() => void>();
const frameListeners = new Set<(frame: TaskFeedFrame) => void>();

let refs = 0;
let queryClient: QueryClient | null = null;
let socket: WebSocket | null = null;
let attempt = 0;
let retryTimer: Timer | null = null;
let countdownTimer: Ticker | null = null;
let hiddenTimer: Timer | null = null;
let paused = false;
let halted = false;
let haltedToken: string | null = null;
let openedWithToken: string | null = null;
let silentReconnect = false;
let helloCount = 0;
let buffer: TaskFeedFrame[] = [];
let flushScheduled = false;
let visibilityBound = false;

function emit(): void {
  for (const listener of listeners) listener();
}

function setSnapshot(patch: Partial<TaskFeedSnapshot>): void {
  snapshot = { ...snapshot, ...patch };
  emit();
}

function clearTimer(timer: Timer | null): null {
  if (timer !== null) clearTimeout(timer);
  return null;
}

function clearRetry(): void {
  retryTimer = clearTimer(retryTimer);
  if (countdownTimer !== null) clearInterval(countdownTimer);
  countdownTimer = null;
}

/** True while something should keep a socket open. */
function wanted(): boolean {
  return refs > 0 && !halted && !paused && typeof window !== "undefined";
}

// ---------------------------------------------------------------------------------------------
// Frames
// ---------------------------------------------------------------------------------------------

function invalidateAll(): void {
  if (!queryClient) return;
  void queryClient.invalidateQueries({ queryKey: taskKeys.all });
  void queryClient.invalidateQueries({ queryKey: logKeys.all });
}

function handleFrame(frame: TaskFeedFrame): void {
  switch (frame.type) {
    case "hello": {
      helloCount += 1;
      const serverTime = Date.parse(frame.server_time);
      setSnapshot({
        followerActive: frame.follower_active,
        serverOffsetMs: Number.isFinite(serverTime) ? serverTime - Date.now() : 0,
      });
      // The first hello lands before the queries have fetched anything worth refreshing. Every
      // later one means the socket was down for a while, and the caches may have missed frames.
      if (helloCount > 1) invalidateAll();
      return;
    }
    case "task":
      if (queryClient) upsertTaskInCaches(queryClient, frame.task);
      return;
    case "log":
      if (queryClient) appendLogInCaches(queryClient, frame.entry);
      return;
    case "follower":
      setSnapshot({ followerActive: frame.active });
      return;
    case "resync":
      invalidateAll();
      return;
  }
}

/** Frames are coalesced per animation frame, so a burst costs one render, not one per row. */
function flush(): void {
  flushScheduled = false;
  const frames = buffer;
  buffer = [];
  if (frames.length === 0) return;
  for (const frame of frames) handleFrame(frame);
  setSnapshot({ lastFrameAt: Date.now() });
  for (const frame of frames) {
    for (const listener of frameListeners) {
      try {
        listener(frame);
      } catch {
        // A listener's failure must not stall the feed for everyone else.
      }
    }
  }
}

function enqueue(frame: TaskFeedFrame): void {
  buffer.push(frame);
  if (flushScheduled) return;
  flushScheduled = true;
  // requestAnimationFrame never fires in a hidden tab, and the socket stays open there for
  // up to a minute, so a hidden tab drains on a short timer instead.
  if (document.visibilityState === "hidden") setTimeout(flush, HIDDEN_FLUSH_MS);
  else requestAnimationFrame(flush);
}

function parseFrame(raw: unknown): TaskFeedFrame | null {
  if (typeof raw !== "string") return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as { type?: unknown }).type === "string" &&
      FRAME_TYPES.has((parsed as { type: string }).type)
    ) {
      return parsed as TaskFeedFrame;
    }
  } catch {
    // A malformed frame is dropped; the next resync or hello repairs the caches.
  }
  return null;
}

// ---------------------------------------------------------------------------------------------
// Socket lifecycle
// ---------------------------------------------------------------------------------------------

function closeSocket(): void {
  const ws = socket;
  socket = null;
  if (!ws) return;
  // Detach first so this close never schedules a retry of its own.
  ws.onopen = null;
  ws.onmessage = null;
  ws.onclose = null;
  ws.onerror = null;
  try {
    ws.close();
  } catch {
    // Already closed.
  }
}

function scheduleRetry(): void {
  clearRetry();
  attempt += 1;
  const delay = Math.min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_MAX_MS);
  let remaining = Math.ceil(delay / 1000);
  setSnapshot({
    state: attempt >= POLLING_AFTER_ATTEMPTS ? "polling" : "reconnecting",
    attempt,
    nextRetryInSeconds: remaining,
  });
  countdownTimer = setInterval(() => {
    remaining = Math.max(0, remaining - 1);
    setSnapshot({ nextRetryInSeconds: remaining });
  }, 1000);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, delay);
}

function connect(): void {
  if (socket || !wanted()) return;
  clearRetry();
  if (!silentReconnect) {
    setSnapshot({
      state: attempt === 0 ? "connecting" : snapshot.state,
      nextRetryInSeconds: null,
    });
  }

  let ws: WebSocket;
  try {
    ws = new WebSocket(getTasksWsUrl());
  } catch {
    silentReconnect = false;
    scheduleRetry();
    return;
  }
  socket = ws;
  openedWithToken = getAccessToken();

  ws.onopen = () => {
    if (socket !== ws) return;
    attempt = 0;
    silentReconnect = false;
    setSnapshot({ state: "connected", attempt: 0, nextRetryInSeconds: null });
  };

  ws.onmessage = (event: MessageEvent) => {
    if (socket !== ws) return;
    const frame = parseFrame(event.data);
    if (frame) enqueue(frame);
  };

  ws.onclose = (event: CloseEvent) => {
    if (socket !== ws) return;
    socket = null;
    if (event.code === CLOSE_POLICY_VIOLATION) {
      // Authentication refused: retrying with the same token only repeats the refusal.
      halted = true;
      haltedToken = openedWithToken;
      silentReconnect = false;
      setSnapshot({ state: "polling", nextRetryInSeconds: null });
      return;
    }
    if (!wanted()) {
      setSnapshot({ state: refs === 0 ? "idle" : snapshot.state, nextRetryInSeconds: null });
      return;
    }
    if (silentReconnect) {
      // The token rotated: reconnect at once with the new one, attempt 0, no visible flap.
      silentReconnect = false;
      connect();
      return;
    }
    scheduleRetry();
  };

  ws.onerror = () => {
    try {
      ws.close();
    } catch {
      // The close handler carries on from here.
    }
  };
}

function onVisibilityChange(): void {
  if (document.visibilityState === "hidden") {
    hiddenTimer = clearTimer(hiddenTimer);
    hiddenTimer = setTimeout(() => {
      hiddenTimer = null;
      if (refs === 0) return;
      paused = true;
      clearRetry();
      closeSocket();
      setSnapshot({ state: "paused", nextRetryInSeconds: null });
    }, HIDDEN_CLOSE_MS);
    return;
  }
  hiddenTimer = clearTimer(hiddenTimer);
  if (!paused) return;
  paused = false;
  attempt = 0;
  connect();
}

function acquire(client: QueryClient): void {
  queryClient = client;
  refs += 1;
  if (!visibilityBound && typeof document !== "undefined") {
    visibilityBound = true;
    document.addEventListener("visibilitychange", onVisibilityChange);
  }
  if (refs === 1) {
    attempt = 0;
    paused = false;
    connect();
    // Mounted in a background tab: start the same 60 s clock a hide would have started.
    if (document.visibilityState === "hidden") onVisibilityChange();
  }
}

function release(): void {
  refs = Math.max(0, refs - 1);
  if (refs > 0) return;
  clearRetry();
  hiddenTimer = clearTimer(hiddenTimer);
  closeSocket();
  attempt = 0;
  helloCount = 0;
  paused = false;
  silentReconnect = false;
  buffer = [];
  snapshot = { ...IDLE, followerActive: snapshot.followerActive };
  emit();
}

/**
 * The session token as the panel sees it. A rotation closes the open socket so the reconnect
 * carries the new token; a new token also lifts a 1008 halt.
 *
 * note: lib/api.ts holds one onTokenRefreshed slot and providers.tsx owns it, so the feed
 * watches the session directly instead of taking that slot away.
 */
function noteToken(token: string | null): void {
  if (halted) {
    if (token !== null && token !== haltedToken) {
      halted = false;
      haltedToken = null;
      attempt = 0;
      connect();
    }
    return;
  }
  if (!socket || token === null || openedWithToken === null || token === openedWithToken) return;
  silentReconnect = true;
  try {
    socket.close();
  } catch {
    // The close handler reconnects either way.
  }
}

// ---------------------------------------------------------------------------------------------
// Public surface
// ---------------------------------------------------------------------------------------------

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const getSnapshot = (): TaskFeedSnapshot => snapshot;
const getServerSnapshot = (): TaskFeedSnapshot => IDLE;

/** Every frame after it has been applied to the caches, for toasts and the live region. */
export function subscribeTaskFrames(fn: (frame: TaskFeedFrame) => void): () => void {
  frameListeners.add(fn);
  return () => {
    frameListeners.delete(fn);
  };
}

/** Mounts the singleton socket while `enabled`; the panel is the one caller. */
export function useTaskFeed(enabled: boolean): TaskFeedSnapshot {
  const client = useQueryClient();
  const { data: session } = useSession();
  const token = session?.accessToken ?? null;

  useEffect(() => {
    if (!enabled) return;
    acquire(client);
    return release;
  }, [enabled, client]);

  useEffect(() => {
    if (enabled) noteToken(token);
  }, [enabled, token]);

  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

/** Read-only subscriber for surfaces that must never open a second socket. */
export function useTaskFeedState(): TaskFeedSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
