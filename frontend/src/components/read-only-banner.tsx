"use client";

import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";

export function ReadOnlyBanner() {
  const { isReadOnly, isLoading } = useReadOnlyMode();

  if (isLoading || !isReadOnly) {
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
