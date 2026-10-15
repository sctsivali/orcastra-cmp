"use client";

import { useEffect, useState } from "react";

function remainingMs(deadline: number | null): number | null {
  return deadline === null ? null : Math.max(0, deadline - Date.now());
}

/**
 * Milliseconds left until a deadline, refreshed while the tab is visible.
 *
 * note: same cadence rule as useRelativeTime: every second in the last minute, otherwise every
 * 30 seconds, and nothing while the tab is hidden.
 */
export function useCountdown(deadline: string | null | undefined): number | null {
  const target = deadline ? Date.parse(deadline) : null;
  const valid = target !== null && Number.isFinite(target) ? target : null;
  const [left, setLeft] = useState(() => remainingMs(valid));

  useEffect(() => {
    setLeft(remainingMs(valid));
    if (valid === null) return;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      const now = remainingMs(valid) ?? 0;
      if (now <= 0) return;
      timer = setTimeout(tick, now < 60_000 ? 1_000 : 30_000);
    };
    const tick = () => {
      setLeft(remainingMs(valid));
      schedule();
    };
    const onVisibilityChange = () => {
      if (timer) clearTimeout(timer);
      if (document.visibilityState === "visible") tick();
    };

    schedule();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [valid]);

  return left;
}

/** "2h 14m", "14m 05s", or "under a minute". */
export function formatCountdown(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return seconds > 0 ? "under a minute" : "now";
}
