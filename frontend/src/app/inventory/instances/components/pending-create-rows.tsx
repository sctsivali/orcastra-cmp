"use client";

import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TableCell, TableRow } from "@/components/ui/table";
import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { formatInstanceType } from "@/lib/utils";
import {
  isStalePending,
  removePendingCreate,
  updatePendingCreate,
  type PendingCreate,
} from "@/lib/pending-creates";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useManualRefresh } from "@/hooks/use-manual-refresh";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { CREATE_PARAM } from "@/components/instance-create/prefill";

interface PendingCreateRowsProps {
  pending: readonly PendingCreate[];
  onViewInstance: (pending: PendingCreate) => void;
  /** The list's own manual refresh, the same one the page's RefreshButton runs. */
  onRefresh: () => Promise<unknown>;
}

function pendingLabel(pending: PendingCreate): string {
  if (pending.pollingHalted) return "Status unavailable";
  if (pending.jobStatus === "failure" || pending.status === "Error") return "Failed";
  if (pending.jobStatus === "partial") return "Partially created";
  if (pending.jobStatus === "unknown") return "Outcome unknown";
  if (isStalePending(pending)) return "No longer reporting";
  if (pending.jobStatus === "success") return "Created, refreshing inventory";
  if (pending.progress) return `Creating… ${pending.progress}`;
  if (pending.phase === "pending") return "Queued";
  if (pending.phase === "preflighting") return "Checking create request";
  if (pending.phase === "submitting") return "Submitting create request";
  if (pending.phase === "starting") return "Starting instance";
  if (pending.phase === "verifying") return "Verifying created instance";
  return "Creating…";
}

// important: the full document reload this replaced did two jobs. It read the list again and,
// because lib/pending-creates.ts persists no verdict, it read the durable job again too, clearing
// pollingHalted on the way. The list read alone would leave a halted row stuck for good, and an
// unknown one waiting out its poll delay, so the job is asked for here as well.
function ReloadCurrentState({
  pending,
  onRefresh,
}: {
  pending: PendingCreate;
  onRefresh: () => Promise<unknown>;
}) {
  const { scopeKey } = useActorQueryScope();
  const { pending: reloading, run } = useManualRefresh(
    () => {
      updatePendingCreate(scopeKey, pending.id, { pollingHalted: false, recheckAt: Date.now() });
      return onRefresh();
    },
    { errorTitle: "Could not reload instances" },
  );
  return (
    <Button
      variant="ghost"
      size="sm"
      aria-busy={reloading}
      disabled={reloading}
      onClick={() => void run()}
    >
      Reload current state
    </Button>
  );
}

// The "Creating..." placeholder rows, rendered above the real rows and outside the filters,
// the sort and the pager: a create in flight is always visible, whatever the list is showing.
export function PendingCreateRows({ pending, onViewInstance, onRefresh }: PendingCreateRowsProps) {
  const { scopeKey } = useActorQueryScope();
  const { navigate } = useNavigationProgress();
  return (
    <>
      {pending.map((p) => (
        <TableRow key={`pending-${p.id}`} data-state="pending">
          <TableCell className="font-medium">{p.name}</TableCell>
          <TableCell>
            <Badge
              variant={
                p.status === "Error" || p.jobStatus === "failure"
                  ? "negative"
                  : isStalePending(p) || p.jobStatus === "partial" || p.jobStatus === "unknown"
                    ? "caution"
                    : "outline"
              }
              className="gap-1"
            >
              {p.status !== "Error" &&
                p.jobStatus !== "failure" &&
                p.jobStatus !== "partial" &&
                p.jobStatus !== "unknown" &&
                !isStalePending(p) && (
                <Loader2 className="h-3 w-3 animate-spin" />
              )}
              {pendingLabel(p)}
            </Badge>
            {p.error ? (
              <TableSubText className="text-destructive">{p.error}</TableSubText>
            ) : p.pollingHalted ? (
              <TableSubText>Polling stopped. Review configuration or dismiss this row.</TableSubText>
            ) : isStalePending(p) ? (
              <TableSubText>
                The cluster stopped reporting on this create. Refresh to see whether
                the instance exists.
              </TableSubText>
            ) : null}
          </TableCell>
          <TableCell>{formatInstanceType(p.type)}</TableCell>
          <TableCell className="whitespace-nowrap">{p.host}</TableCell>
          <TableCell className="whitespace-nowrap">{p.project || "default"}</TableCell>
          <TableCell><TableEmptyValue /></TableCell>
          <TableCell><TableEmptyValue /></TableCell>
          <TableCell><TableEmptyValue /></TableCell>
          <TableCell><TableEmptyValue /></TableCell>
          {/* Pinned like the live rows' actions cell; the row state carries the tint. */}
          <TableCell pinned="end" className="text-right">
            {/* A failed create no longer disappears on a timer, because an operator who
                looked away used to lose the only account of why it never arrived. It
                needs a way out, so it gets an explicit one. A row still creating has
                nothing to dismiss. */}
            {(p.status === "Error" || p.jobStatus === "failure" || p.jobStatus === "partial" || p.jobStatus === "unknown") && (
              <div className="flex justify-end gap-1">
                {(p.jobStatus === "failure" || (p.status === "Error" && !p.jobStatus)) && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      const params = new URLSearchParams();
                      params.set(CREATE_PARAM.node, p.host);
                      params.set(CREATE_PARAM.type, p.type === "virtual-machine" ? "vm" : "container");
                      params.set(CREATE_PARAM.project, p.project || "default");
                      navigate(`/inventory/instances/create?${params.toString()}`);
                    }}
                  >
                    Review configuration
                  </Button>
                )}
                {p.jobStatus === "partial" && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => onViewInstance(p)}
                  >
                    View instance
                  </Button>
                )}
                {p.jobStatus === "unknown" && (
                  <ReloadCurrentState pending={p} onRefresh={onRefresh} />
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Dismiss create operation for ${p.name}`}
                  onClick={() => removePendingCreate(scopeKey, p.id)}
                >
                  Dismiss
                </Button>
              </div>
            )}
          </TableCell>
        </TableRow>
      ))}
    </>
  );
}
