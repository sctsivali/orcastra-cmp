"use client";

import * as React from "react";
import { Camera, History, Info } from "lucide-react";

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
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { EmptyState } from "@/components/ui/empty-state";
import { useInstanceSnapshots } from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import { formatBytes } from "@/lib/utils";
import type { VMInfo } from "@/types/api";

import { SnapshotRowActions } from "@/app/inventory/snapshots/components/snapshot-row-actions";
import {
  expiryLabel,
  relativeAge,
} from "@/app/inventory/snapshots/components/snapshot-format";
import { CreateSnapshotDialog } from "./create-snapshot-dialog";

interface InstanceSnapshotsTabProps {
  instance: VMInfo;
  /** The tab is active. The query stays idle otherwise rather than polling behind another panel. */
  enabled: boolean;
}

export function InstanceSnapshotsTab({ instance, enabled }: InstanceSnapshotsTabProps) {
  const project = instance.project || "default";
  const { data, isLoading, isError, error } = useInstanceSnapshots(
    instance.name,
    instance.host,
    project,
    enabled,
  );
  const { isReadOnly } = useReadOnlyMode();
  const [creating, setCreating] = React.useState(false);
  const [page, setPage] = React.useState(1);
  const pageSize = PANEL_PAGE_SIZE;

  const snapshots = data?.items ?? [];
  const policy = data?.policy;
  const operatorCount = snapshots.filter((s) => s.operator_managed).length;

  const totalPages = Math.ceil(snapshots.length / pageSize);
  const paginated = snapshots.slice((page - 1) * pageSize, page * pageSize);

  if (isLoading) {
    return <p className="text-sm text-muted-foreground py-6">Reading snapshots...</p>;
  }
  if (isError) {
    return (
      <p className="text-sm text-destructive py-6">
        {error instanceof Error ? error.message : "Could not read snapshots."}
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {/* A source under an external schedule belongs to the metered product upstream. Saying so
          is what stops an operator reading an unfamiliar snapshot as someone's mistake. */}
      {policy?.externally_managed && (
        <div className="flex gap-3 rounded-md border bg-muted/40 p-3">
          <Info className="h-4 w-4 shrink-0 mt-0.5 text-muted-foreground" aria-hidden />
          <div className="space-y-1 text-sm">
            <p className="font-medium">Managed snapshot schedule</p>
            <p className="text-muted-foreground">
              Something outside this console snapshots this instance on a schedule
              {policy.schedule ? ` (${policy.schedule})` : ""}
              {policy.expiry ? `, keeping them for ${policy.expiry}` : ""}. That schedule is shown
              here but is not edited from this console. A snapshot you take will appear in the
              owner&apos;s list and count toward their measured storage.
            </p>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between gap-4">
        <div className="text-sm text-muted-foreground">
          {operatorCount} operator snapshot{operatorCount === 1 ? "" : "s"} of {snapshots.length}{" "}
          total
        </div>
        <Button
          size="sm"
          onClick={() => setCreating(true)}
          disabled={isReadOnly}
          title={
            isReadOnly
              ? "Action disabled in read-only mode"
              : "Take an operator snapshot of this instance"
          }
        >
          <Camera className="h-4 w-4 mr-2" aria-hidden />
          Take snapshot
        </Button>
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
                <TableHead>Size</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Expires</TableHead>
                <TableHead className="w-12" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginated.length === 0 ? (
                <TableEmptyRow colSpan={5} icon={History} title="No snapshots on this page" />
              ) : (
                paginated.map((snapshot) => (
                  <TableRow key={snapshot.name}>
                    <TableCell className="font-medium">
                      <span className="font-mono">{snapshot.name}</span>
                      {!snapshot.operator_managed && (
                        <TableSubText>Managed elsewhere</TableSubText>
                      )}
                    </TableCell>
                    <TableCell>
                      {typeof snapshot.size === "number" ? (
                        formatBytes(snapshot.size)
                      ) : (
                        <TableEmptyValue label="Not reported" />
                      )}
                    </TableCell>
                    <TableCell>
                      {snapshot.created_at ? (
                        relativeAge(snapshot.created_at)
                      ) : (
                        <TableEmptyValue label="Unknown" />
                      )}
                    </TableCell>
                    <TableCell>{expiryLabel(snapshot.expires_at)}</TableCell>
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
      />
    </div>
  );
}
