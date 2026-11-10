"use client";

import type { ReactNode } from "react";
import { usePathname } from "next/navigation";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { clearChunkReloadMark } from "@/lib/chunk-reload";

/**
 * What an error boundary shows when a page chunk could not be downloaded and the automatic
 * reload has already been spent. The button reloads the document (lib/chunk-reload.ts
 * explains why that is safe here); `reset()` would only re-request the same missing chunk.
 */
export function ChunkLoadFallback({ secondary }: { secondary?: ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4 p-8" role="alert">
      <AlertTriangle className="h-12 w-12 text-status-pending" />
      <h2 className="text-xl font-semibold">Could not download part of this page</h2>
      <p className="text-muted-foreground text-center max-w-md">
        This happens when the console is updated or restarted while a page is open. Reloading
        fetches the current version.
      </p>
      <div className="flex gap-3 mt-2">
        {secondary}
        <Button
          onClick={() => {
            clearChunkReloadMark(pathname);
            window.location.reload();
          }}
        >
          <RefreshCw className="h-4 w-4 mr-2" />
          Reload
        </Button>
      </div>
    </div>
  );
}
