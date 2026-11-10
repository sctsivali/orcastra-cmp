"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { Button } from "@/components/ui/button";
import { AlertTriangle, RefreshCw, Home } from "lucide-react";
import { ChunkLoadFallback } from "@/components/chunk-load-fallback";
import { IllustrationFallback } from "@/components/illustrations/illustration-frame";
import { DisconnectedIllustration } from "@/components/illustrations/scenes/states";
import { isChunkLoadError, reloadOnceForChunkError } from "@/lib/chunk-reload";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const pathname = usePathname();
  const chunkFailed = isChunkLoadError(error);

  useEffect(() => {
    // A missing chunk is the console having been updated or restarted under this page: one
    // reload fetches the current build. The helper allows a single automatic reload per path.
    if (chunkFailed && reloadOnceForChunkError(pathname)) return;
    // Log only the message, so no stack trace, URL or token reaches the browser console.
    console.error("Unhandled error:", error.message || "Unknown error");
  }, [error, chunkFailed, pathname]);

  if (chunkFailed) {
    return (
      <ChunkLoadFallback
        secondary={
          <Button variant="outline" onClick={() => (window.location.href = "/")}>
            <Home className="h-4 w-4 mr-2" />
            Home
          </Button>
        }
      />
    );
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4 p-8">
      <DisconnectedIllustration size="lg" />
      <IllustrationFallback>
        <AlertTriangle className="h-12 w-12 text-destructive" aria-hidden="true" />
      </IllustrationFallback>
      <h2 className="text-xl font-semibold">Something went wrong</h2>
      <p className="text-muted-foreground text-center max-w-md">
        {error.message || "An unexpected error occurred. Try again."}
      </p>
      <div className="flex gap-3 mt-2">
        <Button variant="outline" onClick={() => (window.location.href = "/")}>
          <Home className="h-4 w-4 mr-2" />
          Home
        </Button>
        <Button onClick={reset}>
          <RefreshCw className="h-4 w-4 mr-2" />
          Try Again
        </Button>
      </div>
    </div>
  );
}
