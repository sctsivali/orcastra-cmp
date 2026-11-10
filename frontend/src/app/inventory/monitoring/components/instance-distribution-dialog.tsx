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

  // The dialog stays mounted between openings, so its page survives a close, and a new filter is
  // a different list: the page resets when the filter changes. Compared during render, since an
  // effect would paint the wrong page once. Filter only: keying on the row count would reset the
  // page whenever an instance starts or stops. The clamp below handles a list that shrank.
  const [lastFilter, setLastFilter] = useState(filter);
  if (lastFilter !== filter) {
    setLastFilter(filter);
    setPage(1);
  }

  // The list behind this dialog updates while it is open, so instances appear and vanish. The
  // page is clamped on read rather than corrected in an effect, so a shrinking list never leaves
  // the operator on an empty page past the end.
  const totalPages = Math.max(1, Math.ceil(instances.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedInstances = instances.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // No count until there is a list to count: a skeleton must not say "0 instances found", nor a
  // failed read "No instances found".
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
        // Focus the dialog itself. Radix would focus the first tabbable element, the Refresh
        // button, whose tooltip then takes the first Escape, so a keyboard user would need two
        // to close the list.
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          contentRef.current?.focus();
        }}
      >
        <DialogHeader className="pb-4">
          {/* The padding keeps the refresh control clear of the dialog's own close button, which
              is a 44px target at right-2 on a phone and smaller at right-4 from sm up. */}
          <div className="flex items-start justify-between gap-3 pr-12 sm:pr-8">
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
                  // Wraps rather than clipping: on a phone the badges move under the name instead of
                  // running past the row's edge.
                  className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 p-3 rounded-lg border hover:bg-muted/50 transition-colors"
                >
                  <div className="flex min-w-0 items-center gap-3">
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-muted">
                      {instance.type === "container" ? (
                        <Container className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
                      ) : (
                        <Monitor className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
                      )}
                    </div>
                    <div className="min-w-0 break-words">
                      {/* The name, not the row, is the control that opens the instance. */}
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
                  <div className="flex flex-wrap items-center gap-2">
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
