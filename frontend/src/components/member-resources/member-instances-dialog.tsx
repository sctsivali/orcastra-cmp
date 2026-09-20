"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Activity, Layers } from "lucide-react";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { formatInstanceType } from "@/lib/utils";
import { instanceStatusDisplay } from "@/lib/instance-status";
import type { MemberResources, VMInfo } from "@/types/api";

interface MemberInstancesDialogProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  memberName: string | null;
  filter: "running" | "stopped" | null;
  instances: VMInfo[];
  isLoading: boolean;
  error: string | null;
  resources: MemberResources | null;
}

/**
 * The instance drill-down behind the Running and Stopped counts.
 *
 * important: it filters the tenant-scoped /instances list rather than the member scan that
 * produced the count. That scan applies no tenant ACL, so listing its names here would leak
 * across organizations. The list can therefore be shorter than the count, and the copy says
 * "visible to you" rather than pretending otherwise. Do not "fix" the mismatch by filtering
 * the count.
 */
export function MemberInstancesDialog({
  isOpen,
  onOpenChange,
  memberName,
  filter,
  instances,
  isLoading,
  error,
  resources,
}: MemberInstancesDialogProps) {
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(PANEL_PAGE_SIZE);

  // important: this dialog stays mounted between openings, because the parent renders it beside
  // its own dialog rather than behind the open flag. Without this the reader reopens on whatever
  // page the previous drill-down left behind, on a different member's list. Reset during render
  // rather than in an effect, which would paint the stale page first.
  const drillDown = `${memberName ?? ""}:${filter ?? ""}`;
  const [pagedDrillDown, setPagedDrillDown] = React.useState(drillDown);
  if (pagedDrillDown !== drillDown) {
    setPagedDrillDown(drillDown);
    setPage(1);
  }

  // note: clamped on read rather than corrected in an effect. The list refetches while the dialog
  // is open, so a shrinking result would otherwise strand the reader on a page past the end.
  const totalPages = Math.max(1, Math.ceil(instances.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedInstances = instances.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="pb-4">
          <DialogTitle>
            {filter === "running" ? "Running" : "Stopped"} Instances on {memberName}
          </DialogTitle>
          <DialogDescription>
            {`${instances.length} ${filter} instance${instances.length === 1 ? "" : "s"} visible to you`}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="pr-2">
        {isLoading ? (
          <div className="flex items-center justify-center py-12">
            <Activity className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        ) : instances.length > 0 ? (
          <div className="space-y-3">
            {/* note: keyed on the instance rather than its index, which now numbers a position
                within the page and would reuse a card across a page change. */}
            {pagedInstances.map((instance) => (
              <Card key={`${instance.host}/${instance.project || "default"}/${instance.name}`}>
                <CardContent className="pt-6">
                  <div className="flex items-start justify-between">
                    <div className="space-y-1 flex-1">
                      <div className="flex items-center gap-2">
                        <h4 className="font-semibold">{instance.name}</h4>
                        <Badge
                          variant={instanceStatusDisplay(instance.status).tone}
                          className="text-xs"
                        >
                          {instanceStatusDisplay(instance.status).label}
                        </Badge>
                        <span className="text-xs text-muted-foreground">
                          {formatInstanceType(instance.type)}
                        </span>
                      </div>
                      <div className="text-sm text-muted-foreground space-y-1">
                        {instance.ip_addresses && instance.ip_addresses.length > 0 && (
                          <div className="flex items-center gap-2">
                            <span className="font-medium">IP:</span>
                            <span className="font-mono text-xs">{instance.ip_addresses.join(", ")}</span>
                          </div>
                        )}
                        <div className="flex items-center gap-4">
                          <span>
                            <span className="font-medium">Type:</span> {formatInstanceType(instance.type)}
                          </span>
                          <span>
                            <span className="font-medium">Project:</span> {instance.project || "default"}
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>
                </CardContent>
              </Card>
            ))}
            {/* Absent rather than disabled when everything fits on one page. No mt-4 wrapper: the
                list is already a space-y-3 stack, whose gap wins over a margin on the child. */}
            {totalPages > 1 && (
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
            )}
          </div>
        ) : error ? (
          <EmptyState
            icon={Layers}
            title="Could not load instances"
            description={error}
          />
        ) : (
          <EmptyState
            icon={Layers}
            title={`No ${filter} instances visible to you`}
            description={
              `The member reports ${
                filter === "running"
                  ? resources?.instances_running ?? 0
                  : (resources?.instances_count || 0) - (resources?.instances_running || 0)
              } ${filter}. Any not listed here belong to an organization you cannot see.`
            }
          />
        )}
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
