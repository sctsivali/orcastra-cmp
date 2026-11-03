"use client";

import { useRef, useState } from "react";
import { Box, Container, Monitor } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ReadErrorState } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Skeleton } from "@/components/ui/skeleton";
import { TableLink } from "@/components/ui/table-link";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { instanceStatusDisplay } from "@/lib/instance-status";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { formatInstanceType, plural } from "@/lib/utils";
import type { VMInfo } from "@/types";

export type DistributionFilter = "running" | "stopped" | "other" | "container" | "vm";

const TITLES: Record<DistributionFilter, string> = {
  running: "Running Instances",
  stopped: "Stopped Instances",
  other: "Other Instances",
  container: "Containers",
  vm: "Virtual Machines",
};

/**
 * The rows behind one slice of the distribution card. `host` is the cluster the page is
 * filtered to, or "all": the card's figures follow that filter, so the list has to as well.
 */
export function filterDistribution(vms: VMInfo[], filter: DistributionFilter, host: string): VMInfo[] {
  const scoped = host === "all" ? vms : vms.filter((vm) => vm.host === host);
  switch (filter) {
    case "running": return scoped.filter((vm) => vm.status === "Running");
    case "stopped": return scoped.filter((vm) => vm.status === "Stopped");
    case "other": return scoped.filter((vm) => vm.status !== "Running" && vm.status !== "Stopped");
    case "container": return scoped.filter((vm) => vm.type === "container");
    case "vm": return scoped.filter((vm) => vm.type === "virtual-machine");
  }
}

interface InstanceDistributionDialogProps {
  open: boolean;
  onClose: () => void;
  filter: DistributionFilter | null;
  instances: VMInfo[];
  /** Display name of the cluster the page is filtered to, null for every cluster. */
  clusterLabel: string | null;
  /** True only while there is nothing to show yet: a background refetch keeps the rows. */
  isLoading: boolean;
  /** The read failed and there are no rows to fall back on. */
  error: unknown;
  onRetry: () => void;
  /** An on-demand live read. Must reject on failure so the button can say so. */
  onRefresh: () => Promise<unknown>;
  lastUpdatedAt: number | null;
  unreachableNodes?: string[];
  nodeErrors?: Record<string, string>;
  /** Opens the instance dialog for one row. This list closes first; modals do not stack. */
  onSelectInstance: (instance: VMInfo) => void;
}

export function InstanceDistributionDialog({
  open,
  onClose,
  filter,
  instances,
  clusterLabel,
  isLoading,
  error,
  onRetry,
  onRefresh,
  lastUpdatedAt,
  unreachableNodes,
  nodeErrors,
  onSelectInstance,
}: InstanceDistributionDialogProps) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);
  const contentRef = useRef<HTMLDivElement>(null);

  // important: this dialog stays mounted between openings, so its page survives a close. The
  // parent reopens it with a different filter (running, then containers), which is a different
  // list entirely, and without this an operator who was on page 3 lands on page 3 of a list they
  // have not seen. Compared during render rather than in an effect, which would paint the wrong
  // page once before correcting it.
  // note: filter only. Keying on the row count too would reset the page every time the poll saw
  // an instance start or stop, throwing the operator back to page 1 mid-read. A list that merely
  // shrank is what the clamp below is for.
  const [lastFilter, setLastFilter] = useState(filter);
  if (lastFilter !== filter) {
    setLastFilter(filter);
    setPage(1);
  }

  // important: the list behind this dialog is live, so instances appear and vanish under the
  // operator. Clamp the page on read rather than correcting it in an effect, or a shrinking list
  // leaves them parked past the end painting nothing.
  const totalPages = Math.max(1, Math.ceil(instances.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedInstances = instances.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // important: no count until there is a list to count. The previous dialog rendered
  // "0 instances found" over its skeleton, and a failed read as "No instances found".
  const failed = !isLoading && !!error;
  const description = isLoading
    ? "Loading instances…"
    : failed
      ? "The instance list could not be read."
      : `${plural(instances.length, "instance")} found${clusterLabel ? ` on ${clusterLabel}` : ""}`;

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        ref={contentRef}
        className="max-w-2xl max-h-[80vh] overflow-hidden flex flex-col"
        // important: focus the dialog itself, not its first control. Radix focuses the first
        // tabbable element, which is the Refresh button; its tooltip opened with the dialog and
        // took the first Escape, so a keyboard user needed two to close the list.
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          contentRef.current?.focus();
        }}
      >
        <DialogHeader className="pb-4">
          {/* pr-8 keeps the refresh control clear of the dialog's own close button. */}
          <div className="flex items-start justify-between gap-3 pr-8">
            <div className="min-w-0 space-y-1.5">
              <DialogTitle>{filter ? TITLES[filter] : "Instances"}</DialogTitle>
              <DialogDescription>{description}</DialogDescription>
            </div>
            <RefreshButton
              label="Refresh instances"
              onRefresh={onRefresh}
              lastUpdatedAt={lastUpdatedAt}
            />
          </div>
        </DialogHeader>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-2 pr-2">
          {!isLoading && !failed && (
            <UnreachableNodesBanner noun="instances" nodes={unreachableNodes} reasons={nodeErrors} />
          )}
          {isLoading ? (
            <div className="space-y-2">
              {[...Array(5)].map((_, i) => (
                <Skeleton key={i} className="h-16 w-full" />
              ))}
            </div>
          ) : failed ? (
            <ReadErrorState noun="instances" error={error} onRetry={onRetry} />
          ) : instances.length > 0 ? (
            pagedInstances.map((instance) => {
              const project = instance.project || "default";
              const status = instanceStatusDisplay(instance.status);
              return (
                <div
                  key={`${instance.host}:${project}:${instance.name}`}
                  className="flex items-center justify-between p-3 rounded-lg border hover:bg-muted/50 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted">
                      {instance.type === "container" ? (
                        <Container className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
                      ) : (
                        <Monitor className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
                      )}
                    </div>
                    <div>
                      {/* The hover tint on this row promised something for as long as it existed
                          and never delivered it. The name is now a real control. */}
                      <p className="font-medium">
                        <TableLink
                          onClick={() => onSelectInstance(instance)}
                          aria-label={`Show details for ${instance.name} on ${instance.host} in project ${project}`}
                        >
                          {instance.name}
                        </TableLink>
                      </p>
                      <p className="text-xs text-muted-foreground">{instance.host} / {project}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant={status.tone}>{status.label}</Badge>
                    <Badge variant="outline" className="text-xs">
                      {formatInstanceType(instance.type)}
                    </Badge>
                  </div>
                </div>
              );
            })
          ) : (
            <EmptyState icon={Box} title="No instances found" className="py-8" />
          )}
        </div>
        {/* Absent rather than disabled when everything fits on one page. Sits outside the
            scroll area so paging does not require scrolling to the bottom first. */}
        {totalPages > 1 && !isLoading && !failed && (
          <div className="mt-4">
            <DataTablePagination
              currentPage={currentPage}
              totalPages={totalPages}
              pageSize={pageSize}
              pageSizeOptions={[...PANEL_PAGE_SIZES]}
              totalItems={instances.length}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
