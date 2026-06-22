"use client";

import * as React from "react";
import { usePathname } from "next/navigation";

/**
 * Thin top progress bar for route navigation feedback.
 *
 * App Router has no global navigation-start event, so this starts on a qualifying
 * internal link click (capture phase) and finishes when the pathname commits.
 * It gives instant feedback on navigation without a full-page loader; the page's
 * own whale still covers the data fetch. Programmatic router.push (ex; the command
 * palette) is not caught here by design.
 */

const TRICKLE_MS = 250;
const FINISH_MS = 220;
const SAFETY_MS = 8000;

export function TopProgressBar() {
  const pathname = usePathname();
  const [visible, setVisible] = React.useState(false);
  const [progress, setProgress] = React.useState(0);
  const activeRef = React.useRef(false);
  const trickle = React.useRef<ReturnType<typeof setInterval> | null>(null);
  const finishT = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const safety = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const finish = React.useCallback(() => {
    if (!activeRef.current) return;
    activeRef.current = false;
    if (trickle.current) { clearInterval(trickle.current); trickle.current = null; }
    if (safety.current) { clearTimeout(safety.current); safety.current = null; }
    setProgress(100);
    finishT.current = setTimeout(() => {
      setVisible(false);
      setProgress(0);
      finishT.current = null;
    }, FINISH_MS);
  }, []);

  const start = React.useCallback(() => {
    if (activeRef.current) return;
    activeRef.current = true;
    if (finishT.current) { clearTimeout(finishT.current); finishT.current = null; }
    setVisible(true);
    setProgress(8);
    // Ease toward 90% and hold there until the route commits.
    trickle.current = setInterval(() => {
      setProgress((prev) =>
        prev >= 90 ? prev : Math.min(90, prev + Math.max(0.5, (90 - prev) * 0.12)),
      );
    }, TRICKLE_MS);
    safety.current = setTimeout(finish, SAFETY_MS);
  }, [finish]);

  // Start on qualifying internal link clicks.
  React.useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const anchor = (e.target as Element | null)?.closest?.("a");
      const href = anchor?.getAttribute("href");
      if (!anchor || !href) return;
      if (anchor.target && anchor.target !== "_self") return;
      if (anchor.hasAttribute("download")) return;
      if ((anchor.getAttribute("rel") || "").includes("external")) return;
      let url: URL;
      try { url = new URL(anchor.href, window.location.href); } catch { return; }
      // Only same-origin navigations to a different path (ignore hash/same-page links).
      if (url.origin !== window.location.origin) return;
      if (url.pathname === window.location.pathname) return;
      start();
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, [start]);

  // Finish when the route commits.
  React.useEffect(() => {
    finish();
  }, [pathname, finish]);

  // Clear any pending timers on unmount.
  React.useEffect(() => {
    return () => {
      if (trickle.current) clearInterval(trickle.current);
      if (finishT.current) clearTimeout(finishT.current);
      if (safety.current) clearTimeout(safety.current);
    };
  }, []);

  if (!visible) return null;

  return (
    <div
      data-testid="route-progress"
      aria-hidden="true"
      className="pointer-events-none fixed inset-x-0 top-0 z-[60] h-0.5"
    >
      <div
        className="h-full bg-primary shadow-[0_0_8px_hsl(var(--primary)/0.5)] transition-[width] duration-200 ease-out motion-reduce:transition-none"
        style={{ width: `${progress}%` }}
      />
    </div>
  );
}
