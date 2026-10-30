/**
 * Graphics Console: a VM's display over SPICE (spice-html5 driven by lib/spice/session.ts).
 *
 * The picture fits the space it is given (shrinking only, so it stays sharp), or follows a manual
 * zoom. With the guest agent running the guest's resolution follows the space instead, which is
 * what keeps text crisp. The session stays connected while its tab is hidden.
 */

"use client";

import * as React from "react";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { AlertCircle, FileUp, Loader2, Monitor, MousePointer2, RefreshCw, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { OverflowText } from "@/components/ui/overflow-text";
import { cn } from "@/lib/utils";
import { sendCombo, type SpiceInputConnection } from "@/lib/spice/input";
import type { KeyCombo } from "@/lib/spice/keys";
import {
  SpiceSession,
  type CursorShape,
  type FileTransferEvent,
  type SpiceStats,
  type SpiceStatus,
} from "@/lib/spice/session";
import type { MouseMode } from "@/lib/spice/pointer";

export type ConsoleZoom = "fit" | number;

export interface GraphicsConsoleProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  /** "fit" shrinks the picture into the space; a number is a fixed zoom in percent. */
  zoom?: ConsoleZoom;
  /** Let the guest's resolution follow the console's size when its agent runs. */
  resizeGuest?: boolean;
  /** False while the tab is hidden: the session stays open, the guest is not resized. */
  active?: boolean;
  onStatus?: (status: SpiceStatus, detail?: string) => void;
  onStats?: (stats: SpiceStats) => void;
  onAgent?: (connected: boolean) => void;
  onMouseMode?: (mode: MouseMode) => void;
  onGuestClipboard?: (text: string) => void;
  /** The guest's resolution and the scale it is shown at, for the connection details. */
  onView?: (view: { surface: { width: number; height: number } | null; scale: number }) => void;
  className?: string;
}

export interface GraphicsConsoleHandle {
  focus: () => void;
  reconnect: () => void;
  disconnect: () => void;
  sendCombo: (combo: Pick<KeyCombo, "keys">) => Promise<boolean>;
  /** A named key ("F2") or one character, with optional modifiers. */
  sendKey: (key: string, modifiers?: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean }) => boolean;
  typeText: (text: string, options?: { signal?: AbortSignal; onProgress?: (done: number, total: number) => void }) => Promise<number>;
  /** Puts text on the guest's clipboard through its agent; false without an agent. */
  setGuestClipboard: (text: string) => boolean;
  sendFiles: (files: FileList | File[]) => number;
  getSpiceConnection: () => SpiceInputConnection | null;
  getCanvas: () => HTMLCanvasElement | null;
  screenshot: () => string | null;
  /** The guest's resolution, or null before the first frame. */
  surfaceSize: () => { width: number; height: number } | null;
}

const OVERLAY = "absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-black/95 px-6 text-center";

export const GraphicsConsole = forwardRef<GraphicsConsoleHandle, GraphicsConsoleProps>(function GraphicsConsole(
  {
    consoleId,
    instanceName,
    host,
    project,
    zoom = "fit",
    resizeGuest = true,
    active = true,
    onStatus,
    onStats,
    onAgent,
    onMouseMode,
    onGuestClipboard,
    onView,
    className,
  },
  ref,
) {
  const screenId = `spice-screen-${consoleId}`;
  const viewportRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<SpiceSession | null>(null);
  const [status, setStatus] = useState<SpiceStatus>("loading");
  const [detail, setDetail] = useState<string>("");
  const [surface, setSurface] = useState<{ width: number; height: number } | null>(null);
  const [viewport, setViewport] = useState<{ width: number; height: number }>({ width: 0, height: 0 });
  const [mouseMode, setMouseMode] = useState<MouseMode>("client");
  const [locked, setLocked] = useState(false);
  const [agent, setAgent] = useState(false);
  const [cursor, setCursor] = useState<{ shape: CursorShape | null; x: number; y: number }>({ shape: null, x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [transfers, setTransfers] = useState<FileTransferEvent[]>([]);

  // Latest callbacks without re-creating the session.
  const callbacks = useRef({ onStatus, onStats, onAgent, onMouseMode, onGuestClipboard, onView });
  callbacks.current = { onStatus, onStats, onAgent, onMouseMode, onGuestClipboard, onView };

  useEffect(() => {
    const session = new SpiceSession(screenId, { instanceName, host, project }, {
      onStatus: (next, why) => {
        setStatus(next);
        setDetail(why ?? "");
        callbacks.current.onStatus?.(next, why);
      },
      onSurface: (width, height) => setSurface({ width, height }),
      onStats: (stats) => callbacks.current.onStats?.(stats),
      onMouseMode: (mode) => {
        setMouseMode(mode);
        callbacks.current.onMouseMode?.(mode);
      },
      onPointerLock: setLocked,
      onAgent: (connected) => {
        setAgent(connected);
        callbacks.current.onAgent?.(connected);
      },
      onCursor: (shape, x, y) => setCursor({ shape, x, y }),
      onGuestClipboard: (text) => callbacks.current.onGuestClipboard?.(text),
      onFileTransfer: (event) =>
        setTransfers((list) => {
          const others = list.filter((t) => t.id !== event.id);
          return event.state === "sending" ? [...others, event] : [...others, event].slice(-5);
        }),
    });
    sessionRef.current = session;
    void session.connect();
    return () => {
      session.dispose();
      sessionRef.current = null;
    };
  }, [screenId, instanceName, host, project]);

  // The space the picture gets: drives fit and the guest's resolution.
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setViewport((prev) =>
        Math.abs(prev.width - width) < 1 && Math.abs(prev.height - height) < 1 ? prev : { width, height },
      );
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!active || zoom !== "fit" || viewport.width < 100 || viewport.height < 100) return;
    sessionRef.current?.requestGuestSize(viewport.width, viewport.height, resizeGuest);
  }, [active, zoom, resizeGuest, viewport.width, viewport.height, agent]);

  // Finished transfers leave the list after a moment.
  useEffect(() => {
    if (!transfers.some((t) => t.state !== "sending")) return;
    const timer = window.setTimeout(() => setTransfers((list) => list.filter((t) => t.state === "sending")), 4000);
    return () => window.clearTimeout(timer);
  }, [transfers]);

  const scale = (() => {
    if (!surface) return 1;
    if (zoom !== "fit") return zoom / 100;
    if (!viewport.width || !viewport.height) return 1;
    return Math.min(1, viewport.width / surface.width, viewport.height / surface.height);
  })();

  useEffect(() => {
    callbacks.current.onView?.({ surface, scale });
  }, [surface, scale]);

  const focus = useCallback(() => sessionRef.current?.focus(), []);

  useImperativeHandle(
    ref,
    () => ({
      focus,
      reconnect: () => sessionRef.current?.reconnect(),
      disconnect: () => sessionRef.current?.dispose(),
      sendCombo: async (combo) => {
        const ok = await sendCombo(sessionRef.current?.connection as SpiceInputConnection | null, combo);
        return ok;
      },
      sendKey: (key, modifiers) => sessionRef.current?.sendNamedOrCharacter(key, modifiers) ?? false,
      typeText: (text, options) => sessionRef.current?.typeText(text, options) ?? Promise.resolve(text.length),
      setGuestClipboard: (text) => sessionRef.current?.setGuestClipboard(text) ?? false,
      sendFiles: (files) => sessionRef.current?.sendFiles(files) ?? 0,
      getSpiceConnection: () => (sessionRef.current?.connection as SpiceInputConnection | null) ?? null,
      getCanvas: () => sessionRef.current?.surface ?? null,
      screenshot: () => sessionRef.current?.screenshot() ?? null,
      surfaceSize: () => surface,
    }),
    [focus, surface],
  );

  const reconnect = () => sessionRef.current?.reconnect();
  const scaledWidth = surface ? Math.round(surface.width * scale) : 0;
  const scaledHeight = surface ? Math.round(surface.height * scale) : 0;
  const canDrop = agent && status === "connected";

  return (
    <div
      ref={viewportRef}
      className={cn(
        "relative h-full w-full bg-black focus-within:ring-2 focus-within:ring-inset focus-within:ring-ring",
        zoom === "fit" ? "overflow-hidden" : "overflow-auto",
        className,
      )}
      onDragOver={(event) => {
        if (!canDrop || !event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        setDragging(true);
      }}
      onDragLeave={(event) => {
        if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
        setDragging(false);
      }}
      onDrop={(event) => {
        if (!canDrop) return;
        event.preventDefault();
        setDragging(false);
        sessionRef.current?.sendFiles(event.dataTransfer.files);
      }}
    >
      <div
        className="relative"
        style={{
          width: scaledWidth || "100%",
          height: scaledHeight || "100%",
          // Centered when it is smaller than the space; scrolls from the corner when larger.
          margin: zoom === "fit" || (scaledWidth < viewport.width && scaledHeight < viewport.height) ? "0 auto" : undefined,
          top: zoom === "fit" && scaledHeight && scaledHeight < viewport.height ? (viewport.height - scaledHeight) / 2 : 0,
        }}
      >
        <div
          id={screenId}
          className="spice-screen absolute left-0 top-0 origin-top-left"
          style={{
            transform: scale !== 1 ? `scale(${scale})` : undefined,
            visibility: status === "connected" || status === "reconnecting" ? "visible" : "hidden",
            touchAction: "none",
            cursor: locked ? "none" : undefined,
          }}
          onPointerDown={(event) => event.stopPropagation()}
          onMouseDown={(event) => event.stopPropagation()}
          onTouchStart={(event) => event.stopPropagation()}
        />
        {locked && cursor.shape?.url && (
          // The guest's own pointer, where the guest says it is (server mouse mode).
          // eslint-disable-next-line @next/next/no-img-element
          <img
            alt=""
            aria-hidden="true"
            src={cursor.shape.url}
            width={cursor.shape.width}
            height={cursor.shape.height}
            className="pointer-events-none absolute left-0 top-0 z-[1]"
            style={{
              transform: `translate(${(cursor.x - cursor.shape.hotX) * scale}px, ${(cursor.y - cursor.shape.hotY) * scale}px) scale(${scale})`,
              transformOrigin: "0 0",
            }}
          />
        )}
      </div>

      {status === "connected" && mouseMode === "server" && !locked && (
        <div className="pointer-events-none absolute left-1/2 top-3 z-[2] -translate-x-1/2 rounded bg-black/80 px-3 py-1.5 text-xs text-white/90">
          <MousePointer2 className="mr-1.5 inline h-3.5 w-3.5" aria-hidden="true" />
          Click the screen to control the mouse. Press Esc to release it.
        </div>
      )}

      {dragging && (
        <div className="pointer-events-none absolute inset-2 z-20 flex items-center justify-center rounded-md border-2 border-dashed border-white/60 bg-black/60 text-sm text-white">
          <FileUp className="mr-2 h-5 w-5" aria-hidden="true" />
          Drop files to copy them to the guest
        </div>
      )}

      {transfers.length > 0 && (
        <ul className="absolute bottom-3 right-3 z-20 w-64 space-y-1.5" aria-live="polite">
          {transfers.map((t) => (
            <li key={t.id} className="rounded bg-black/85 px-3 py-2 text-xs text-white/90">
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0">
                  <OverflowText value={t.name} label="file name" tone="chrome" copy="none" />
                </span>
                <span className="shrink-0 tabular-nums text-white/70">
                  {t.state === "sending" ? `${Math.round((t.sent / Math.max(1, t.size)) * 100)}%` : t.state === "done" ? "Copied" : t.state === "cancelled" ? "Cancelled" : "Failed"}
                </span>
              </div>
              {t.state === "sending" && (
                <div className="mt-1 h-1 overflow-hidden rounded bg-white/20">
                  <div className="h-full bg-primary" style={{ width: `${Math.round((t.sent / Math.max(1, t.size)) * 100)}%` }} />
                </div>
              )}
              {t.state === "error" && t.error && <p className="mt-1 text-white/60">{t.error}</p>}
            </li>
          ))}
        </ul>
      )}

      {(status === "loading" || status === "connecting") && (
        <div className={OVERLAY} role="status" aria-live="polite">
          <Loader2 className="h-7 w-7 animate-spin text-white/70 motion-reduce:animate-none" aria-hidden="true" />
          <p className="text-sm text-white/80">
            {status === "loading" ? "Loading the console…" : `Connecting to ${instanceName}…`}
          </p>
        </div>
      )}

      {status === "reconnecting" && (
        <div className="absolute left-1/2 top-3 z-10 flex -translate-x-1/2 items-center gap-2 rounded bg-black/85 px-3 py-1.5 text-xs text-white/90" role="status" aria-live="polite">
          <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          Reconnecting…
          <button type="button" className="underline underline-offset-2 hover:text-white" onClick={reconnect}>
            Now
          </button>
        </div>
      )}

      {(status === "ended" || status === "error") && (
        <div className={OVERLAY} role="alert">
          {status === "ended" ? (
            <div className="relative">
              <Monitor className="h-10 w-10 text-white/50" aria-hidden="true" />
              <WifiOff className="absolute -right-1 -top-1 h-4 w-4 text-white/50" aria-hidden="true" />
            </div>
          ) : (
            <AlertCircle className="terminal-status h-8 w-8 text-status-offline" aria-hidden="true" />
          )}
          <p className="max-w-md text-sm text-white/80">{detail || (status === "ended" ? "The console session ended." : "The console connection failed.")}</p>
          <Button variant="outline" size="sm" onClick={reconnect} className="border-white/30 text-white hover:bg-white/10">
            <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
            Reconnect
          </Button>
        </div>
      )}
    </div>
  );
});

export default GraphicsConsole;
