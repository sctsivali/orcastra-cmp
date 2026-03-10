"use client";

import { AlertCircle, WifiOff } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";

export function ReadOnlyBanner() {
  const { isReadOnly, isLoading, isError } = useReadOnlyMode();

  if (isLoading) {
    return null;
  }

  if (isError && !isReadOnly) {
    return (
      <Alert variant="default" className="mb-4 border-yellow-500/50 text-yellow-600 dark:text-yellow-400">
        <WifiOff className="h-4 w-4" />
        <AlertTitle>Connection Issue</AlertTitle>
        <AlertDescription>
          Unable to reach the server. Some operations may fail until connectivity is restored.
        </AlertDescription>
      </Alert>
    );
  }

  if (!isReadOnly) {
    return null;
  }

  return (
    <Alert variant="destructive" className="mb-4">
      <AlertCircle className="h-4 w-4" />
      <AlertTitle>Read-Only Mode</AlertTitle>
      <AlertDescription>
        The system is currently in read-only mode. Creating, updating, or deleting resources is temporarily disabled.
      </AlertDescription>
    </Alert>
  );
}
