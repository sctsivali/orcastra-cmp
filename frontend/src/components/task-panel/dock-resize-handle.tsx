"use client";

import * as React from "react";
import { DOCK_DEFAULT, DOCK_MIN } from "@/lib/dock-cookie";
import { cn } from "@/lib/utils";

const STEP_PX = 24;
const SHIFT_MULTIPLIER = 4;
const KEY_COMMIT_MS = 300;
const RESIZE_DEBOUNCE_MS = 150;
const MAX_VIEWPORT_FRACTION = 0.7;
const DESKTOP_QUERY = "(min-width: 768px)";

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

const desktopListeners = new Set<() => void>();
let desktopQuery: MediaQueryList | null = null;

function subscribeDesktop(listener: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  if (!desktopQuery) {
    desktopQuery = window.matchMedia(DESKTOP_QUERY);
    desktopQuery.addEventListener("change", () => desktopListeners.forEach((fn) => fn()));
  }
  desktopListeners.add(listener);
  return () => {
    desktopListeners.delete(listener);
  };
}

/**
 * Whether the viewport is at or past the `md` breakpoint. Hydration-safe: the server and the
 * first client render both say desktop, and a phone corrects itself in the same commit.
 */
export function useDesktopViewport(): boolean {
  return React.useSyncExternalStore(
    subscribeDesktop,
    () => (desktopQuery ?? window.matchMedia(DESKTOP_QUERY)).matches,
    () => true,
  );
}

/** The tallest the dock may be: 70% of the viewport, re-read on resize with a short debounce. */
export function useMaxDockHeight(): number {
  const read = () =>
    typeof window === "undefined"
      ? DOCK_DEFAULT
      : Math.max(DOCK_MIN, Math.floor(window.innerHeight * MAX_VIEWPORT_FRACTION));
  const [max, setMax] = React.useState<number>(read);

  React.useEffect(() => {
    setMax(read());
    let timer: number | undefined;
    const onResize = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setMax(read()), RESIZE_DEBOUNCE_MS);
    };
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
    };
  }, []);

  return max;
}

export interface DockResizeHandleProps {
  height: number;
  min: number;
  max: number;
  /** Every intermediate height while dragging or stepping. */
  onChange: (height: number) => void;
  /** The height to persist, once per gesture. */
  onCommit: (height: number) => void;
}

/**
 * The 6 px strip on the dock's top edge. A separator with the ARIA value contract, so a
 * keyboard reaches the same range a pointer does.
 *
 * important: while a pointer drag is live the body loses pointer events and text selection.
 * An xterm canvas or an iframe under the pointer would otherwise take the move events and the
 * drag would stall the moment the cursor crossed one. Pointer capture keeps delivering to this
 * element regardless, which is why it opts itself back in.
 */
export function DockResizeHandle({ height, min, max, onChange, onCommit }: DockResizeHandleProps) {
  const desktop = useDesktopViewport();
  const drag = React.useRef<{ startY: number; startHeight: number; last: number } | null>(null);
  const keyTimer = React.useRef<number | undefined>(undefined);
  const latest = React.useRef(height);
  latest.current = height;

  React.useEffect(() => () => window.clearTimeout(keyTimer.current), []);

  if (!desktop) return null;

  const endDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    const committed = drag.current.last;
    drag.current = null;
    event.currentTarget.removeAttribute("data-dragging");
    document.body.classList.remove("select-none", "pointer-events-none");
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    onCommit(committed);
  };

  const stepTo = (next: number) => {
    const clamped = clamp(next, min, max);
    onChange(clamped);
    window.clearTimeout(keyTimer.current);
    keyTimer.current = window.setTimeout(() => onCommit(clamped), KEY_COMMIT_MS);
  };

  return (
    <div
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize task panel"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={height}
      tabIndex={0}
      className={cn(
        "group absolute inset-x-0 top-0 z-10 flex h-1.5 cursor-row-resize touch-none items-center justify-center",
        "pointer-events-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
      )}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        // A keyboard step still waiting to commit must not land after this pointer gesture.
        window.clearTimeout(keyTimer.current);
        drag.current = { startY: event.clientY, startHeight: latest.current, last: latest.current };
        event.currentTarget.setPointerCapture(event.pointerId);
        event.currentTarget.setAttribute("data-dragging", "");
        document.body.classList.add("select-none", "pointer-events-none");
      }}
      onPointerMove={(event) => {
        if (!drag.current) return;
        const next = clamp(drag.current.startHeight + (drag.current.startY - event.clientY), min, max);
        if (next === drag.current.last) return;
        drag.current.last = next;
        onChange(next);
      }}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={() => {
        window.clearTimeout(keyTimer.current);
        onChange(DOCK_DEFAULT);
        onCommit(DOCK_DEFAULT);
      }}
      onKeyDown={(event) => {
        const step = STEP_PX * (event.shiftKey ? SHIFT_MULTIPLIER : 1);
        switch (event.key) {
          case "ArrowUp":
            event.preventDefault();
            stepTo(latest.current + step);
            break;
          case "ArrowDown":
            event.preventDefault();
            stepTo(latest.current - step);
            break;
          case "Home":
            event.preventDefault();
            stepTo(min);
            break;
          case "End":
            event.preventDefault();
            stepTo(max);
            break;
          default:
        }
      }}
    >
      <span
        aria-hidden="true"
        className="h-0.5 w-8 rounded-full bg-border transition-colors group-hover:bg-muted-foreground group-focus-visible:bg-muted-foreground group-data-[dragging]:bg-muted-foreground motion-reduce:transition-none"
      />
    </div>
  );
}
