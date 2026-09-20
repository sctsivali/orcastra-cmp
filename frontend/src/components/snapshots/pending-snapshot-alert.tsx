"use client";

import { Clock3, X } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { OverflowText } from "@/components/ui/overflow-text";
import { RefreshButton } from "@/components/ui/refresh-button";

export interface PendingSnapshotNotice {
  host: string;
  project: string;
  instanceName: string;
  snapshotName: string;
  message: string;
}

interface PendingSnapshotAlertProps {
  notice: PendingSnapshotNotice;
  onRefresh: () => Promise<unknown> | unknown;
  onDismiss: () => void;
}

/** A pending create is accepted work, not a completed restore point. Keep that distinction visible. */
export function PendingSnapshotAlert({
  notice,
  onRefresh,
  onDismiss,
}: PendingSnapshotAlertProps) {
  return (
    <Alert role="status">
      <Clock3 className="h-4 w-4" aria-hidden />
      <div className="min-w-0 pr-12 sm:pr-8">
        <AlertTitle>Snapshot creation is in progress</AlertTitle>
        <AlertDescription className="space-y-3">
          <p className="min-w-0">
            <OverflowText
              value={notice.snapshotName}
              label="pending snapshot name"
              lines="none"
              mono
              copy="none"
            />{" "}
            for{" "}
            <OverflowText
              value={notice.instanceName}
              label="pending snapshot instance"
              lines="none"
              mono
              copy="none"
            />{" "}
            in{" "}
            <OverflowText
              value={notice.project}
              label="pending snapshot project"
              lines="none"
              mono
              copy="none"
            />{" "}
            on{" "}
            <OverflowText
              value={notice.host}
              label="pending snapshot cluster"
              lines="none"
              mono
              copy="none"
            />{" "}
            was accepted, but the cluster has not confirmed completion yet. Refresh before relying
            on it as a restore point.
          </p>
          <p className="break-words text-muted-foreground">{notice.message}</p>
          <div className="flex flex-wrap items-center gap-2">
            <RefreshButton
              label="Refresh snapshots"
              hideTimestamp
              size="sm"
              className="min-h-11 sm:min-h-9"
              onRefresh={onRefresh}
            />
            <span className="text-xs text-muted-foreground">Check for the named snapshot.</span>
          </div>
        </AlertDescription>
      </div>
      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label="Dismiss snapshot progress"
        className="absolute right-2 top-2 h-11 w-11 sm:h-8 sm:w-8"
        onClick={onDismiss}
      >
        <X className="h-4 w-4" aria-hidden />
      </Button>
    </Alert>
  );
}
