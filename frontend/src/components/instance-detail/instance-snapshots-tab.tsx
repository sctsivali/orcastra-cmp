"use client";

import * as React from "react";
import { CalendarClock, Camera, History, Info } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { EmptyState } from "@/components/ui/empty-state";
import { OverflowText } from "@/components/ui/overflow-text";
import {
  PendingSnapshotAlert,
  type PendingSnapshotNotice,
} from "@/components/snapshots/pending-snapshot-alert";
import { useInstanceSnapshots, useSnapshotSchedules } from "@/hooks/use-lxd";
import { useReadOnlyMode, useSnapshotSchedulesEnabled } from "@/hooks/use-read-only-mode";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import { formatBytes } from "@/lib/utils";
import type { VMInfo } from "@/types/api";

import { SnapshotRowActions } from "@/app/inventory/snapshots/components/snapshot-row-actions";
import {
  createdCell,
  expiryCell,
  nextRunLabel,
} from "@/app/inventory/snapshots/components/snapshot-format";
import {
  ORIGIN_LABEL,
  snapshotOrigin,
} from "@/app/inventory/snapshots/components/snapshot-origin";
import {
  STATE_LABEL,
  scheduleState,
} from "@/app/inventory/snapshots/components/snapshot-schedule-vocabulary";
import { CreateSnapshotDialog } from "./create-snapshot-dialog";
import { ScheduleSnapshotsDialog } from "./schedule-snapshots-dialog";

interface InstanceSnapshotsTabProps {
  instance: VMInfo;
  /** The tab is active. The query stays idle otherwise rather than polling behind another panel. */
  enabled: boolean;
}

export function InstanceSnapshotsTab({ instance, enabled }: InstanceSnapshotsTabProps) {
  const project = instance.project || "default";
  const { data, isLoading, isError, error, refreshNow } = useInstanceSnapshots(
    instance.name,
    instance.host,
    project,
    enabled,
  );
  const { isReadOnly, isLoading: readOnlyLoading } = useReadOnlyMode();
  // Read from the settings poll the console already runs, not probed by firing a request at a
  // route that may not exist. `undefined` is not yet answered and leaves the button as it was.
  const schedulesEnabled = useSnapshotSchedulesEnabled();
  const [creating, setCreating] = React.useState(false);
  const [pendingSnapshot, setPendingSnapshot] =
    React.useState<PendingSnapshotNotice | null>(null);
  const [scheduling, setScheduling] = React.useState(false);
  // Narrowed to this instance, so the tab reads one row rather than the whole fleet's schedules,
  // and skipped entirely once the deployment has said the routes are not mounted.
  const schedules = useSnapshotSchedules(
    { host: instance.host, project, instance: instance.name },
    enabled && schedulesEnabled !== false,
  );
  const ownSchedule = schedules.data?.items?.[0] ?? null;
  const [page, setPage] = React.useState(1);
  const pageSize = PANEL_PAGE_SIZE;
  const takeSnapshotReasonId = "instance-take-snapshot-disabled-reason";

  const snapshots = data?.items ?? [];
  const policy = data?.policy;
  const operatorCount = snapshots.filter((s) => s.operator_managed).length;

  const totalPages = Math.ceil(snapshots.length / pageSize);
  const paginated = snapshots.slice((page - 1) * pageSize, page * pageSize);

  // Keep the create dialog mounted while a fresh policy read is in flight or has failed. The
  // dialog owns the ambiguous-outcome guard and draft; replacing this subtree during its Check
  // snapshots flow would erase both after a non-idempotent request may have reached the cluster.
  if (isLoading && !creating && !pendingSnapshot) {
    return <p className="text-sm text-muted-foreground py-6">Reading snapshots...</p>;
  }
  if (isError && !creating && !pendingSnapshot) {
    return (
      <p className="text-sm text-destructive py-6">
        {error instanceof Error ? error.message : "Could not read snapshots."}
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {isError && (
        <p role="alert" className="text-sm text-destructive py-2">
          {error instanceof Error ? error.message : "Could not read snapshots."}
        </p>
      )}

      {pendingSnapshot && (
        <PendingSnapshotAlert
          notice={pendingSnapshot}
          onRefresh={refreshNow}
          onDismiss={() => setPendingSnapshot(null)}
        />
      )}

      {/*
        Two kinds of schedule can now describe one instance, so neither block uses a bare
        demonstrative. "That schedule" was true only while there was exactly one on screen; the
        moment a second appears it binds to whichever the reader saw last. Each block names its own
        owner instead, reusing the words the Origin column already puts on screen.
      */}
      {policy?.externally_managed && (
        <div className="flex gap-3 rounded-md border bg-muted/40 p-3">
          <Info className="h-4 w-4 shrink-0 mt-0.5 text-muted-foreground" aria-hidden />
          <div className="space-y-1 text-sm">
            <p className="font-medium">Upstream Snapshot Schedule</p>
            <p className="text-muted-foreground">
              The metered snapshot product outside this console snapshots {instance.name} on a
              schedule
              {policy.schedule ? ` (${policy.schedule})` : ""}
              {policy.expiry ? `, keeping them for ${policy.expiry}` : ""}. Orcastra reads that
              schedule and never writes it, so it cannot be changed, paused or removed from here. A
              snapshot you take will appear in the owner&apos;s list and count toward their measured
              storage.
            </p>
            {!ownSchedule && (
              <p className="text-muted-foreground">
                You cannot add an operator schedule to this instance while that one is in place.
              </p>
            )}
          </div>
        </div>
      )}

      {ownSchedule && (
        <div className="flex gap-3 rounded-md border bg-muted/40 p-3">
          <CalendarClock className="h-4 w-4 shrink-0 mt-0.5 text-muted-foreground" aria-hidden />
          <div className="space-y-1 text-sm">
            <div className="flex items-center gap-2">
              <p className="font-medium">Operator Snapshot Schedule</p>
              <Badge variant={ownSchedule.state === "paused" ? "secondary" : "outline"}>
                {STATE_LABEL[scheduleState(ownSchedule)]}
              </Badge>
            </div>
            {ownSchedule.state === "paused" ? (
              <p className="text-muted-foreground">
                This schedule is not running.{" "}
                {ownSchedule.paused_reason ?? "It was paused."} The snapshots it already took are
                below and expire on their own.
              </p>
            ) : (
              <p className="text-muted-foreground">
                Orcastra snapshots {instance.name} {ownSchedule.summary}, keeps the last{" "}
                {ownSchedule.keep_last}, and lets each one expire after {ownSchedule.expiry_days}{" "}
                days. Next run {nextRunLabel(ownSchedule.next_run_at)}. This schedule is owned here,
                so you can pause, edit or remove it from the Snapshots page.
              </p>
            )}
          </div>
        </div>
      )}

      <div className="flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
        <div className="text-sm text-muted-foreground">
          {operatorCount} operator snapshot{operatorCount === 1 ? "" : "s"} of {snapshots.length}{" "}
          total
        </div>
        <div className="flex w-full flex-col items-start gap-1 sm:w-auto sm:items-end">
          <div className="flex flex-wrap items-center gap-2">
          {/*
            Disabled with the reason written above rather than refused after a round trip. The tab
            already holds `policy.externally_managed`, so the one instance that cannot carry a
            schedule is known before anything is clicked, and the operator reads why in the banner
            instead of meeting a 409 they have to interpret.

            The same argument covers the feature flag. SNAPSHOT_SCHEDULES_ENABLED is off by default
            and gates the router, so on a stock deployment this button was enabled, opened a dialog,
            and submitted into a 404 that names nothing an operator can act on.
          */}
          <Button
            size="sm"
            variant="outline"
            onClick={() => setScheduling(true)}
            disabled={
              isReadOnly || Boolean(policy?.externally_managed) || schedulesEnabled === false
            }
            title={
              schedulesEnabled === false
                ? "Snapshot schedules are switched off on this deployment"
                : policy?.externally_managed
                  ? "The metered product outside this console already schedules this instance"
                  : isReadOnly
                    ? "Action disabled in read-only mode"
                    : ownSchedule
                      ? "Edit the recurring snapshot schedule"
                      : "Snapshot this instance on a schedule"
            }
          >
            <CalendarClock className="h-4 w-4 mr-2" aria-hidden />
            {ownSchedule ? "Edit schedule" : "Schedule snapshots"}
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={() => setCreating(true)}
            disabled={isReadOnly || readOnlyLoading}
            aria-busy={readOnlyLoading}
            aria-describedby={
              isReadOnly || readOnlyLoading ? takeSnapshotReasonId : undefined
            }
            title={
              readOnlyLoading
                ? "Checking read-only mode"
                : isReadOnly
                ? "Action disabled in read-only mode"
                : "Take an operator snapshot of this instance"
            }
          >
            <Camera className="h-4 w-4 mr-2" aria-hidden />
            Take snapshot
          </Button>
          </div>
          {(isReadOnly || readOnlyLoading) && (
            <p
              id={takeSnapshotReasonId}
              role="status"
              className="text-xs text-muted-foreground sm:text-right"
            >
              {readOnlyLoading
                ? "Checking whether snapshot creation is available."
                : "Snapshot creation is disabled in read-only mode."}
            </p>
          )}
        </div>
      </div>

      {snapshots.length === 0 ? (
        <EmptyState
          icon={History}
          title="No snapshots yet"
          description="Take one before a risky change so this instance can be rolled back."
        />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Snapshot</TableHead>
                <TableHead>Origin</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Expires</TableHead>
                <TableHead className="w-12" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginated.length === 0 ? (
                <TableEmptyRow colSpan={6} icon={History} title="No snapshots on this page" />
              ) : (
                paginated.map((snapshot) => (
                  <TableRow key={snapshot.name}>
                    <TableCell className="font-medium">
                      <OverflowText
                        value={snapshot.name}
                        label={"snapshot " + snapshot.name}
                        mono
                      />
                    </TableCell>
                    {/*
                      The word the fleet table already uses, read from the same map.

                      This cell said "Managed elsewhere" for anything without the `ops-` prefix,
                      which is exactly the copy the three-class origin model was written to remove:
                      a snapshot an operator took by hand over SSH is theirs, not another product's.
                      The rewrite reached the fleet table and stopped there, and both specs that
                      assert it is gone read the fleet grid, so it survived here.
                    */}
                    <TableCell>{ORIGIN_LABEL[snapshotOrigin(snapshot)]}</TableCell>
                    <TableCell className="whitespace-nowrap">
                      {typeof snapshot.size === "number" ? (
                        formatBytes(snapshot.size)
                      ) : (
                        <TableEmptyValue label="Not reported" />
                      )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      {createdCell(snapshot.created_at)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      {expiryCell(snapshot.expires_at)}
                    </TableCell>
                    <TableCell>
                      <SnapshotRowActions
                        // The per-instance listing carries no host or project, because it was
                        // read for one already-known scope. Stamping them here keeps every row
                        // self-describing, which is what lets the actions take their target from
                        // the row rather than from whichever page happens to render them.
                        snapshot={{
                          ...snapshot,
                          source_name: instance.name,
                          host: instance.host,
                          project,
                        }}
                        billingLinked={Boolean(policy?.externally_managed)}
                      />
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>

          {snapshots.length > pageSize && (
            <DataTablePagination
              variant="compact"
              label={`snapshots of ${instance.name}`}
              currentPage={page}
              totalPages={totalPages}
              onPageChange={setPage}
            />
          )}
        </>
      )}

      <CreateSnapshotDialog
        open={creating}
        onOpenChange={setCreating}
        instance={instance}
        project={project}
        billingLinked={Boolean(policy?.externally_managed)}
        onPending={setPendingSnapshot}
      />

      <ScheduleSnapshotsDialog
        open={scheduling}
        onOpenChange={setScheduling}
        instance={instance}
        existing={ownSchedule}
      />
    </div>
  );
}
