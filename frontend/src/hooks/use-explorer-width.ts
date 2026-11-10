"use client";

import { useEffect, useRef, useState } from "react";

export type ExplorerWidth = "wide" | "medium" | "narrow";

function modeOf(width: number): ExplorerWidth {
  if (width >= 720) return "wide";
  return width >= 520 ? "medium" : "narrow";
}

/**
 * The explorer's own width. A viewport breakpoint means nothing inside a floating console window.
 *
 * A hidden tab measures 0, and laying out for 0 would throw away the columns a person just
 * chose to see, so a zero reading is ignored.
 */
export function useExplorerWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [mode, setMode] = useState<ExplorerWidth>("wide");
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      const width = entry?.contentRect.width ?? 0;
      if (width > 0) setMode(modeOf(width));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return { ref, mode };
}
