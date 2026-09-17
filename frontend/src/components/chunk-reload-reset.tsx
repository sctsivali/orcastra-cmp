"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { clearChunkReloadMark } from "@/lib/chunk-reload";

/**
 * A path that rendered after a client navigation is a path whose chunks loaded, so its
 * one-reload allowance is restored.
 *
 * important: never on first mount. This sits above the error boundaries in Providers, so its
 * effect runs before theirs; clearing on mount would erase the mark the boundary set a moment
 * ago and, when the reloaded document still references a missing chunk (stale HTML at an
 * edge, two replicas on different builds), the boundary would reload again without bound.
 * The mark expires on its own after a minute.
 */
export function ChunkReloadReset() {
  const pathname = usePathname();
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (previous.current !== null && previous.current !== pathname) {
      clearChunkReloadMark(pathname);
    }
    previous.current = pathname;
  }, [pathname]);
  return null;
}
