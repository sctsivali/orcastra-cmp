/**
 * Graphics Console - single SPICE client engine for VM VGA consoles.
 *
 * Used by both the windowed console (console-window.tsx) and the full-page
 * route (app/console/[id]/page.tsx). All SPICE specifics live in lib/spice:
 * the loader (single bundle), the key tables, the input helpers and the agent
 * clipboard. This component owns only the React lifecycle, the canvas wiring
 * and the imperative handle.
 */

"use client";

import * as React from "react";
import {
  useEffect,
  useRef,
  useCallback,
  forwardRef,
  useImperativeHandle,
  useState,
} from "react";
import { useConsoleStore } from "@/lib/console-store";
import { AlertCircle, Monitor, RefreshCw, Wifi, WifiOff } from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { getGraphicsWsUrl } from "@/lib/ws-url";
import { loadSpiceScripts } from "@/lib/spice/spice-loader";
import {
  sendScanKey,
  sendKeys as sendKeysToConn,
  sendKeyWithModifiers as sendKeyWithModifiersToConn,
  sendCtrlAltDel as sendCtrlAltDelToConn,
  pasteViaKeystrokes,
  type KeyModifiers,
  type AbortFlag,
} from "@/lib/spice/spice-input";
import { enableAgentClipboard } from "@/lib/spice/spice-clipboard";

export type GraphicsStatus =
  | "loading"
  | "connecting"
  | "connected"
  | "error"
  | "disconnected";

interface GraphicsConsoleProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  width?: number;
  height?: number;
  zoom?: number; // percentage, 100 = 100%
  onReady?: () => void;
  onError?: (error: string) => void;
  onClose?: () => void;
  onStatusChange?: (status: GraphicsStatus) => void;
  className?: string;
}

export interface GraphicsConsoleHandle {
  focus: () => void;
  sendCtrlAltDel: () => void;
  sendKeys: (keys: string) => void;
  sendKeyWithModifiers: (key: string, modifiers?: KeyModifiers) => void;
  sendKeyCode: (keyCode: number) => boolean;
  pasteText: (text: string) => Promise<void>;
  getSpiceConnection: () => any;
  screenshot: () => string | null;
  reconnect: () => void;
  disconnect: () => void;
}

interface SpiceConnection {
  stop: () => void;
  sendCtrlAltDel?: () => void;
}

const MAX_CONNECTION_ATTEMPTS = 3;

export const GraphicsConsole = forwardRef<GraphicsConsoleHandle, GraphicsConsoleProps>(
  function GraphicsConsole(
    {
      consoleId,
      instanceName,
      host,
      project,
      zoom = 100,
      onReady,
      onError,
      onClose,
      onStatusChange,
      className,
    },
    ref,
  ) {
    const containerRef = useRef<HTMLDivElement>(null);
    const screenRef = useRef<HTMLDivElement>(null);
    const spiceConnectionRef = useRef<SpiceConnection | null>(null);
    const pasteAbortRef = useRef<AbortFlag | null>(null);
    const guestResizeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const [status, setStatusState] = useState<GraphicsStatus>("loading");
    const [errorMessage, setErrorMessage] = useState("");
    const [scriptsReady, setScriptsReady] = useState(false);
    const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });

    const connectionAttemptRef = useRef(0);
    const isConnectingRef = useRef(false);
    // Holds the latest connect() so the error-retry can call it without a
    // circular useCallback dependency.
    const connectRef = useRef<() => void>(() => {});

    const { updateConnectionStatus } = useConsoleStore();

    const screenId = `spice-screen-${consoleId}`;
    const scale = zoom / 100;

    // Single funnel for status so the store, the optional parent callback and
    // local state never drift.
    const applyStatus = useCallback(
      (next: GraphicsStatus, message?: string) => {
        setStatusState(next);
        // The store has no "loading" state; map it to the nearest equivalent.
        updateConnectionStatus(consoleId, next === "loading" ? "connecting" : next, message);
        onStatusChange?.(next);
      },
      [consoleId, updateConnectionStatus, onStatusChange],
    );

    const getConn = useCallback(() => spiceConnectionRef.current as any, []);

    // Tell the guest agent to enable a display sized to the console viewport. This
    // sends VD_AGENT_MONITORS_CONFIG (via spice-html5 resize_window), which is what
    // virt-viewer and LXD-UI do on agent connect: it ENABLES/wakes an idle display
    // (clears the guest's "Display output is not active") and fits the desktop to
    // the viewport. Debounced so a burst of resizes sends one config. The guest
    // queues it until the agent has tokens, so calling it early is safe.
    const requestGuestResize = useCallback(() => {
      if (guestResizeTimerRef.current) clearTimeout(guestResizeTimerRef.current);
      guestResizeTimerRef.current = setTimeout(() => {
        const sc = spiceConnectionRef.current as any;
        const box = containerRef.current;
        if (!sc || !box || typeof sc.resize_window !== "function") return;
        // Fit the guest desktop to the available console area so it never overflows
        // the viewport. The old 640x480 floor forced a phone (≈390px wide) to render a
        // 640px-wide desktop it had to pan; clamp only to a small usable VGA floor and
        // never request larger than the box. Zoom buttons cover detail on small screens.
        let w = Math.max(320, Math.floor(box.clientWidth));
        let h = Math.max(240, Math.floor(box.clientHeight));
        // Xorg requires width/height to be a multiple of 8 (see resize.js).
        if (w % 8) w += 8 - (w % 8);
        if (h % 8) h += 8 - (h % 8);
        try {
          sc.resize_window(0, w, h, 32, 0, 0);
        } catch {
          /* ignore - agent may not be ready; a later trigger retries */
        }
      }, 200);
    }, []);

    // Load the SPICE client bundle once.
    useEffect(() => {
      let mounted = true;
      loadSpiceScripts()
        .then(() => {
          if (mounted) setScriptsReady(true);
        })
        .catch((err) => {
          if (!mounted) return;
          console.error("[GraphicsConsole] Failed to load SPICE:", err);
          setErrorMessage("Failed to load SPICE client");
          applyStatus("error", "Failed to load SPICE client");
        });
      return () => {
        mounted = false;
      };
    }, [applyStatus]);

    const connect = useCallback(async () => {
      const screen = document.getElementById(screenId);
      if (!scriptsReady || !screen) return;
      if (isConnectingRef.current) return;

      if (connectionAttemptRef.current >= MAX_CONNECTION_ATTEMPTS) {
        const msg = `Connection failed after ${MAX_CONNECTION_ATTEMPTS} attempts. Click Reconnect to try again.`;
        setErrorMessage(msg);
        applyStatus("error", msg);
        return;
      }

      connectionAttemptRef.current += 1;
      isConnectingRef.current = true;

      if (spiceConnectionRef.current) {
        try {
          spiceConnectionRef.current.stop();
        } catch {
          /* ignore */
        }
        spiceConnectionRef.current = null;
      }

      applyStatus("connecting");
      setErrorMessage("");

      try {
        const wsUrl = await getGraphicsWsUrl(instanceName, host, project);
        const SpiceMainConn = (window as any).SpiceMainConn;
        if (!SpiceMainConn) {
          isConnectingRef.current = false;
          throw new Error("SpiceMainConn not available");
        }

        const sc = new SpiceMainConn({
          uri: wsUrl,
          screen_id: screenId,
          password: "",
          onerror: (e: Error | string) => {
            isConnectingRef.current = false;
            let msg = typeof e === "string" ? e : e?.message || "Connection error";
            if (msg.includes("Unexpected protocol mismatch")) {
              msg = "Console connection failed. The VM may not be running or graphics console is unavailable.";
            } else if (msg.includes("Connection refused")) {
              msg = "Cannot connect to console server. Please check if the backend is running.";
            } else if (msg.includes("Bad password")) {
              msg = "Authentication failed for console session.";
            } else if (msg.includes("Permission denied")) {
              msg = "Console session expired or access denied. Please reconnect.";
            }
            console.error("[GraphicsConsole] SPICE error:", msg);
            // Bounded auto-retry: a SPICE session opens 4 channels and one can
            // fail transiently (leaving the display channel missing -> "Display
            // output is not active"). Retry the whole connect a few times before
            // surfacing the error, instead of leaving the user to click Reconnect.
            // connectionAttemptRef is incremented per connect() and reset on
            // success, so this is strictly bounded and cannot storm.
            if (connectionAttemptRef.current < MAX_CONNECTION_ATTEMPTS) {
              applyStatus("connecting");
              setTimeout(() => connectRef.current(), 400 * connectionAttemptRef.current);
              return;
            }
            setErrorMessage(msg);
            applyStatus("error", msg);
            onError?.(msg);
          },
          onsuccess: () => {
            isConnectingRef.current = false;
            connectionAttemptRef.current = 0;
            applyStatus("connected");
            onReady?.();
          },
          onagent: () => {
            // Guest agent connected: agent clipboard becomes usable, AND this is
            // the moment to enable/wake the guest display via a monitors-config.
            // Without this the guest can stay parked on "Display output is not
            // active" until a manual Reconnect (the old mouse-nudge was unreliable).
            requestGuestResize();
          },
        });

        spiceConnectionRef.current = sc;
      } catch (error) {
        isConnectingRef.current = false;
        const msg = error instanceof Error ? error.message : "Failed to connect";
        console.error("[GraphicsConsole] Connection error:", error);
        setErrorMessage(msg);
        applyStatus("error", msg);
        onError?.(msg);
      }
    }, [instanceName, host, project, scriptsReady, screenId, applyStatus, onReady, onError, requestGuestResize]);
    connectRef.current = connect;

    const disconnect = useCallback(() => {
      isConnectingRef.current = false;
      if (pasteAbortRef.current) pasteAbortRef.current.aborted = true;
      if (spiceConnectionRef.current) {
        try {
          spiceConnectionRef.current.stop();
        } catch {
          /* ignore */
        }
        spiceConnectionRef.current = null;
      }
      applyStatus("disconnected");
      onClose?.();
    }, [applyStatus, onClose]);

    const reconnect = useCallback(() => {
      connectionAttemptRef.current = 0;
      isConnectingRef.current = false;
      if (spiceConnectionRef.current) {
        try {
          spiceConnectionRef.current.stop();
        } catch {
          /* ignore */
        }
        spiceConnectionRef.current = null;
      }
      setStatusState("loading");
      setTimeout(() => connect(), 150);
    }, [connect]);

    // Connect once scripts are ready. rAF ensures the screen div is painted
    // (mobile can lag the first paint); a bounded retry covers that.
    useEffect(() => {
      if (!scriptsReady || status !== "loading") return;
      let cancelled = false;
      let tries = 0;
      const attempt = () => {
        if (cancelled || status !== "loading") return;
        if (document.getElementById(screenId)) {
          requestAnimationFrame(() => {
            if (!cancelled) connect();
          });
        } else if (tries < 10) {
          tries += 1;
          setTimeout(attempt, 100);
        }
      };
      attempt();
      return () => {
        cancelled = true;
      };
    }, [scriptsReady, status, connect, screenId]);

    // Canvas lifecycle: track native size (no polling), keep the SPICE canvas
    // from auto-repeat spam, and re-wire if SPICE swaps the canvas element.
    useEffect(() => {
      if (status !== "connected") return;
      const screen = document.getElementById(screenId);
      if (!screen) return;

      let canvas: HTMLCanvasElement | null = null;
      let attrObserver: MutationObserver | null = null;

      const readSize = () => {
        if (!canvas) return;
        const w = canvas.width;
        const h = canvas.height;
        if (w > 0 && h > 0) {
          setCanvasSize((prev) => (prev.width === w && prev.height === h ? prev : { width: w, height: h }));
        }
      };

      // Enable/wake the guest display and keep it fitted to the viewport. The agent
      // monitors-config sent on connect (onagent -> requestGuestResize) is the
      // deterministic wake; this ResizeObserver re-sends it (debounced) when the
      // console viewport changes, so the desktop tracks the window like LXD-UI. Fire
      // once now in case the agent connected before this effect mounted.
      requestGuestResize();
      const sizeObserver = new ResizeObserver(() => requestGuestResize());
      if (containerRef.current) sizeObserver.observe(containerRef.current);

      // important: spice-html5's native handle_keydown forwards EVERY keydown,
      // including browser auto-repeat (e.repeat). A held key on connect then
      // floods the guest (the "backtick spam"). Drop repeat keydowns before the
      // native handler sees them; the guest OS does its own key repeat.
      const dropRepeat = (e: KeyboardEvent) => {
        if (e.repeat) {
          e.preventDefault();
          e.stopImmediatePropagation();
        }
      };

      const wire = (el: HTMLCanvasElement) => {
        canvas = el;
        el.addEventListener("keydown", dropRepeat, { capture: true });
        // SPICE sets canvas.width/height (backing store) on resolution change.
        attrObserver?.disconnect();
        attrObserver = new MutationObserver(readSize);
        attrObserver.observe(el, { attributes: true, attributeFilter: ["width", "height"] });
        readSize();
      };

      const existing = screen.querySelector("canvas") as HTMLCanvasElement | null;
      if (existing) wire(existing);

      // Detect (re)creation/replacement of the canvas element.
      const childObserver = new MutationObserver(() => {
        const next = screen.querySelector("canvas") as HTMLCanvasElement | null;
        if (next && next !== canvas) {
          if (canvas) canvas.removeEventListener("keydown", dropRepeat, { capture: true } as any);
          wire(next);
        }
      });
      childObserver.observe(screen, { childList: true, subtree: true });

      // No blank-canvas auto-reconnect here: a "blank" canvas during connect or an
      // idle guest is normal, and reconnecting on it stormed. The wake above clears
      // an idle display; a genuine channel failure is recovered by the bounded
      // retry in connect()'s onerror.

      return () => {
        sizeObserver.disconnect();
        childObserver.disconnect();
        attrObserver?.disconnect();
        if (guestResizeTimerRef.current) clearTimeout(guestResizeTimerRef.current);
        if (canvas) canvas.removeEventListener("keydown", dropRepeat, { capture: true } as any);
      };
    }, [status, screenId, requestGuestResize]);

    // Apply zoom by scaling the canvas CSS box (keeps mouse-coordinate accuracy;
    // the vendored spicemsg.js converts via getBoundingClientRect).
    useEffect(() => {
      if (status !== "connected" || canvasSize.width === 0) return;
      const screen = document.getElementById(screenId);
      const canvas = screen?.querySelector("canvas") as HTMLCanvasElement | null;
      if (!canvas) return;
      canvas.style.width = `${canvasSize.width * scale}px`;
      canvas.style.height = `${canvasSize.height * scale}px`;
    }, [status, screenId, scale, canvasSize]);

    // Window-level forwarder so typing works when focus is on a toolbar button
    // rather than the canvas. Skips repeats (guest handles repeat) and never
    // double-sends when the canvas itself is focused (native handler owns that).
    useEffect(() => {
      if (status !== "connected") return;

      const forward = (handlerName: "handle_keydown" | "handle_keyup") => (e: KeyboardEvent) => {
        if (handlerName === "handle_keydown" && e.repeat) return;
        const sc = spiceConnectionRef.current as any;
        if (!sc || !sc.inputs || sc.inputs.state !== "ready") return;
        const target = e.target as HTMLElement;
        if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable) {
          return;
        }
        const screen = document.getElementById(screenId);
        if (screen && (target === screen || screen.contains(target))) return; // canvas focused -> native handles it
        const win = window as any;
        if (win[handlerName]) win[handlerName].call({ sc }, e);
      };

      // Release held modifiers when the window loses focus. The browser delivers no
      // keyup for a modifier released while unfocused (alt-tab, switching apps), so
      // the guest would think Ctrl/Alt/Shift/Meta is still down and mangle the next
      // keystrokes. Sending an unconditional keyup is harmless if the key wasn't held.
      const releaseModifiers = () => {
        const sc = spiceConnectionRef.current as any;
        if (!sc || !sc.inputs || sc.inputs.state !== "ready") return;
        for (const code of [16, 17, 18, 91]) sendScanKey(sc, code, false, true);
      };

      const onKeyDown = forward("handle_keydown");
      const onKeyUp = forward("handle_keyup");
      window.addEventListener("keydown", onKeyDown, true);
      window.addEventListener("keyup", onKeyUp, true);
      window.addEventListener("blur", releaseModifiers);
      return () => {
        window.removeEventListener("keydown", onKeyDown, true);
        window.removeEventListener("keyup", onKeyUp, true);
        window.removeEventListener("blur", releaseModifiers);
      };
    }, [status, screenId]);

    useImperativeHandle(
      ref,
      () => ({
        focus: () => {
          const screen = document.getElementById(screenId);
          screen?.focus();
          const canvas = screen?.querySelector("canvas");
          if (canvas) (canvas as HTMLCanvasElement).focus();
        },
        sendCtrlAltDel: () => sendCtrlAltDelToConn(getConn()),
        sendKeys: (keys: string) => sendKeysToConn(getConn(), keys),
        sendKeyWithModifiers: (key: string, modifiers?: KeyModifiers) =>
          sendKeyWithModifiersToConn(getConn(), key, modifiers),
        sendKeyCode: (keyCode: number) => sendScanKey(getConn(), keyCode),
        pasteText: async (text: string) => {
          // "Insert" types the text where the cursor is (works in BIOS, login
          // prompts, anything - no Ctrl+V needed). Native copy/paste via the
          // guest agent runs separately (see enableAgentClipboard) for the
          // Ctrl+C / Ctrl+V experience when spice-vdagent is present.
          const sc = getConn();
          if (!sc || !text) return;
          if (pasteAbortRef.current) pasteAbortRef.current.aborted = true;
          const abort: AbortFlag = { aborted: false };
          pasteAbortRef.current = abort;
          await pasteViaKeystrokes(sc, text, abort);
        },
        getSpiceConnection: () => spiceConnectionRef.current,
        screenshot: () => {
          const screen = document.getElementById(screenId);
          const canvas = screen?.querySelector("canvas");
          if (!canvas) return null;
          try {
            return (canvas as HTMLCanvasElement).toDataURL("image/png");
          } catch {
            return null;
          }
        },
        reconnect,
        disconnect,
      }),
      [screenId, getConn, reconnect, disconnect],
    );

    // Native bidirectional clipboard via the guest agent (when spice-vdagent is
    // running): guest Ctrl+C lands in the browser clipboard, and the browser
    // clipboard is served to the guest on Ctrl+V. No-op without the agent.
    useEffect(() => {
      if (status !== "connected") return;
      return enableAgentClipboard(() => spiceConnectionRef.current);
    }, [status]);

    useEffect(() => {
      return () => {
        disconnect();
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const StatusOverlay = () => {
      if (status === "loading") {
        return (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/95 z-10">
            <Spinner className="h-8 w-8 text-white/70" />
            <p className="text-sm text-white/70">Loading SPICE client...</p>
          </div>
        );
      }
      if (status === "error") {
        return (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/95 z-10">
            <AlertCircle className="h-8 w-8 text-red-400" />
            <p className="text-sm text-white/70">{errorMessage || "Connection failed"}</p>
            <Button variant="outline" size="sm" onClick={reconnect} className="text-white border-white/30 hover:bg-white/10">
              <RefreshCw className="h-4 w-4 mr-2" />
              Reconnect
            </Button>
          </div>
        );
      }
      if (status === "connecting") {
        return (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/95 z-10">
            <div className="relative">
              <Monitor className="h-12 w-12 text-white/70" />
              <Wifi className="h-4 w-4 text-blue-400 absolute -top-1 -right-1 animate-pulse" />
            </div>
            <p className="text-sm text-white/70">Connecting to {instanceName}...</p>
            <p className="text-xs text-white/50">Establishing SPICE connection</p>
          </div>
        );
      }
      if (status === "disconnected") {
        return (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-black/95 z-10">
            <div className="relative">
              <Monitor className="h-12 w-12 text-white/50" />
              <WifiOff className="h-4 w-4 text-white/50 absolute -top-1 -right-1" />
            </div>
            <p className="text-sm text-white/70">Disconnected from {instanceName}</p>
            <Button variant="outline" size="sm" onClick={reconnect} className="text-white border-white/30 hover:bg-white/10">
              <RefreshCw className="h-4 w-4 mr-2" />
              Reconnect
            </Button>
          </div>
        );
      }
      return null;
    };

    return (
      <div
        ref={containerRef}
        className={cn("w-full h-full bg-black relative", className)}
        style={{ overflow: "auto", display: "flex", alignItems: "flex-start", justifyContent: "flex-start" }}
        onClick={() => {
          const screen = document.getElementById(screenId);
          if (screen) {
            screen.focus();
            const canvas = screen.querySelector("canvas");
            if (canvas) (canvas as HTMLCanvasElement).focus();
          }
        }}
      >
        <StatusOverlay />

        <div
          ref={screenRef}
          id={screenId}
          className="spice-screen"
          style={{ position: "relative", visibility: status === "connected" ? "visible" : "hidden", outline: "none", touchAction: "none" }}
          tabIndex={0}
          onMouseDown={(e) => e.stopPropagation()}
          onTouchStart={(e) => e.stopPropagation()}
          onTouchMove={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            const canvas = e.currentTarget.querySelector("canvas");
            if (canvas) (canvas as HTMLCanvasElement).focus();
          }}
        />

        <style jsx global>{`
          .spice-screen {
            pointer-events: auto;
          }
          .spice-screen canvas {
            display: block;
            outline: none;
            cursor: default;
            pointer-events: auto;
          }
          .spice-screen canvas:focus {
            outline: none;
          }
        `}</style>
      </div>
    );
  },
);

export default GraphicsConsole;
