"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { clearChunkReloadMark } from "@/lib/chunk-reload";

/**
 * Restores a path's one-reload allowance once it renders after a client navigation, since its
 * chunks loaded.
 *
 * important: never on first mount. This sits above the error boundaries in Providers, so its
 * effect runs before theirs; clearing on mount would erase the mark the boundary just set,
 * and a reloaded document that still references a missing chunk (ex; two replicas on
 * different builds) would reload without bound. The mark expires on its own after a minute.
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
