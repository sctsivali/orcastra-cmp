"use client";

import { AlertTriangle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { LiveTimestamp } from "@/components/ui/live-timestamp";
import { RefreshButton } from "@/components/ui/refresh-button";
import { STALE_AFTER_MS } from "@/lib/query-config";
import type { MetricsErrorState } from "./metrics-copy";

export function MetricsErrorAlert({ errorState }: { errorState: MetricsErrorState }) {
  return (
    <Alert variant="destructive">
      <AlertTriangle aria-hidden="true" className="h-4 w-4" />
      <AlertTitle>{errorState.title}</AlertTitle>
      <AlertDescription>
        {errorState.description} Showing the last successful readings.
      </AlertDescription>
    </Alert>
  );
}

export function MetricsToolbar({
  errorState,
  isFetching,
  onRefresh,
  updatedAt,
}: {
  errorState: MetricsErrorState | null;
  isFetching: boolean;
  onRefresh: () => Promise<unknown> | unknown;
  updatedAt: number | null;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <LiveTimestamp
        forceStale={!!errorState}
        reason={errorState?.staleReason}
        staleAfterMs={STALE_AFTER_MS.live}
        updatedAt={updatedAt}
      />
      <div className="flex items-center gap-2">
        {isFetching ? (
          <span aria-hidden="true" className="text-xs text-muted-foreground">
            Refreshing…
          </span>
        ) : null}
        <RefreshButton
          hideTimestamp
          label="Refresh instance metrics"
          onRefresh={onRefresh}
        />
      </div>
    </div>
  );
}
