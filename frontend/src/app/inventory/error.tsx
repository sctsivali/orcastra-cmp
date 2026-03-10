"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { AlertTriangle, RefreshCw, ArrowLeft } from "lucide-react";
import { useRouter } from "next/navigation";

export default function InventoryError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const router = useRouter();

  useEffect(() => {
    console.error("Inventory page error:", error);
  }, [error]);

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4 p-8">
      <AlertTriangle className="h-12 w-12 text-amber-500" />
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
