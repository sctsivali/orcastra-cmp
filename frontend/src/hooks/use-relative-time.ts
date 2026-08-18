"use client";

import { useEffect, useState } from "react";
import { formatRelativeTime } from "@/lib/utils";

/**
 * Live-updating relative age of a timestamp ("just now", "42s ago", "3m ago").
 *
 * note: ticks every second only while the value is under a minute old, then
 * backs off to 30s. Pauses entirely while the tab is hidden, matching the
 * refetchIntervalInBackground: false convention the query layer already uses.
 */
export function useRelativeTime(timestamp: number | null | undefined): string {
  const [label, setLabel] = useState(() => formatRelativeTime(timestamp));

  useEffect(() => {
    setLabel(formatRelativeTime(timestamp));
    if (!timestamp) return;

    let timer: ReturnType<typeof setTimeout> | null = null;

    const schedule = () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      const ageMs = Date.now() - timestamp;
      timer = setTimeout(tick, ageMs < 60_000 ? 1_000 : 30_000);
    };

    const tick = () => {
      setLabel(formatRelativeTime(timestamp));
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
  }, [timestamp]);

  return label;
}
