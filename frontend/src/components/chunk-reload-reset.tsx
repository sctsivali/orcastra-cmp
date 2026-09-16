"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { clearChunkReloadMark } from "@/lib/chunk-reload";

/** A path that rendered is a path whose chunks loaded, so its one-reload allowance is restored. */
export function ChunkReloadReset() {
  const pathname = usePathname();
  useEffect(() => {
    clearChunkReloadMark(pathname);
  }, [pathname]);
  return null;
}
