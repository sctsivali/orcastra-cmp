"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { AlertTriangle, RefreshCw, ArrowLeft } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { ChunkLoadFallback } from "@/components/chunk-load-fallback";
import { IllustrationFallback } from "@/components/illustrations/illustration-frame";
import { DisconnectedIllustration } from "@/components/illustrations/scenes/states";
import { isChunkLoadError, reloadOnceForChunkError } from "@/lib/chunk-reload";

export default function InventoryError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const chunkFailed = isChunkLoadError(error);

  useEffect(() => {
    // A missing chunk is the console having been updated or restarted under this page: one
    // reload fetches the current build. The helper allows a single automatic reload per path.
    if (chunkFailed && reloadOnceForChunkError(pathname)) return;
    console.error("Inventory page error:", error.message || "Unknown error");
  }, [error, chunkFailed, pathname]);

  if (chunkFailed) {
    return (
      <ChunkLoadFallback
        secondary={
          <Button variant="outline" onClick={() => router.back()}>
            <ArrowLeft className="h-4 w-4 mr-2" />
            Go Back
          </Button>
        }
      />
    );
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4 p-8">
      <DisconnectedIllustration size="lg" />
      <IllustrationFallback>
        <AlertTriangle className="h-12 w-12 text-status-pending" aria-hidden="true" />
      </IllustrationFallback>
      <h2 className="text-xl font-semibold">Failed to load page</h2>
      <p className="text-muted-foreground text-center max-w-md">
        {error.message || "Could not load the requested resource. The server may be unavailable."}
      </p>
      <div className="flex gap-3 mt-2">
        <Button variant="outline" onClick={() => router.back()}>
          <ArrowLeft className="h-4 w-4 mr-2" />
          Go Back
        </Button>
        <Button onClick={reset}>
          <RefreshCw className="h-4 w-4 mr-2" />
          Retry
        </Button>
      </div>
    </div>
  );
}
