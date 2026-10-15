"use client";

import { Hourglass } from "lucide-react";
import { formatCountdown, useCountdown } from "@/hooks/use-countdown";
import { formatDateTime } from "@/lib/utils";
import type { MigrationJob } from "@/types/migrations";
import { RETAINED_PHASES } from "./migration-format";

/** The retention window of a finished move: when the source goes, and what can still be done. */
export function MigrationRetentionPanel({
  job,
  autoCleanupEnabled,
}: {
  job: MigrationJob;
  autoCleanupEnabled?: boolean;
}) {
  const left = useCountdown(job.retention_deadline);
  if (!RETAINED_PHASES.has(job.phase) || job.intent !== "move" || job.strategy === "member_relocation") {
    return null;
  }
  const expired = left !== null && left <= 0;
  const automatic = autoCleanupEnabled === true;
  return (
    <section className="space-y-2 rounded-md border p-3" aria-labelledby="migration-retention-title">
      <div className="flex items-center gap-2">
        <Hourglass className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h3 id="migration-retention-title" className="text-sm font-semibold">
          Source Retention
        </h3>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <p className="text-xs text-muted-foreground">Retained until</p>
          <p className="text-sm font-medium">
            {job.retention_deadline ? formatDateTime(job.retention_deadline) : "Not started"}
          </p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Time left</p>
          <p className="text-sm font-medium" aria-live="off">
            {left === null ? "Unknown" : expired ? "Retention ended" : formatCountdown(left)}
          </p>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        {job.phase === "cleanup_blocked"
          ? "Automatic cleanup stopped because a safety check failed. Nothing was deleted. Roll back, retry Finalize after the cause is fixed, or ask an administrator to release the job."
          : automatic
            ? "When retention ends, Orcastra checks that the source is unchanged and the new instance is healthy, then deletes the source. Finalize deletes it now. Rollback restores it."
            : "When retention ends, Finalize deletes the source. Rollback restores it until then."}
      </p>
    </section>
  );
}
