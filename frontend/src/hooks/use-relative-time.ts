"use client";

import { useEffect, useState } from "react";
import { formatRelativeTime } from "@/lib/utils";

/**
 * Live-updating relative age of a timestamp ("just now", "42s ago", "3m ago").
 *
 * Ticks every second while the value is under a minute old, then every 30s.
 * Pauses while the tab is hidden, like refetchIntervalInBackground: false in
 * the query layer.
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
