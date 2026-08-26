"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { TableLink } from "@/components/ui/table-link";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { KeyValueList } from "@/components/used-by/panels/config-panel";
import { formatDateTime, formatLabel } from "@/lib/utils";
import type { OperationInfo } from "@/types/api";

// The operations table truncates the id to eight characters and appends an ellipsis, so the one
// value an operator needs when correlating with cluster logs is the one they cannot copy. It
// also drops `metadata` and `err` entirely, which between them are the whole story of why an
// operation is stuck or failed.
//
// note: deliberately no lazy fetch of GET /operations/{id}. The list polls every 15 seconds, so
// a second request would show a different status than the row that was clicked, and an
// operation whose status changes under the reader is worse than one shown a few seconds stale.
export function OperationDetailDialog({
  operation,
  open,
  onOpenChange,
  instanceName,
  footer,
}: {
  operation: (OperationInfo & { node_name?: string; project?: string }) | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  instanceName?: string;
  footer?: React.ReactNode;
}) {
  if (!operation) return null;

  return (
    <RecordDetailDialog
      open={open}
      onOpenChange={onOpenChange}
      title={formatLabel(operation.class_name)}
      recordId={operation.id}
      recordIdLabel="Operation"
      subtitle={operation.description || undefined}
      status={<Badge variant="outline">{operation.status}</Badge>}
      error={operation.err || null}
      footer={footer}
      size="lg"
    >
      <div className="space-y-4">
        <DetailSection title="Where and When">
          <DetailGrid cols={3}>
            <DetailField label="Node" value={operation.node_name} />
            <DetailField label="Project" value={operation.project || "default"} />
            <DetailField
              label="Instance"
              value={
                instanceName && operation.node_name ? (
                  <TableLink
                    href={`/inventory/instances?q=${encodeURIComponent(instanceName)}&project=${encodeURIComponent(operation.project || "default")}&host=${encodeURIComponent(operation.node_name)}`}
                    title={`Open ${instanceName} in Instances`}
                  >
                    {instanceName}
                  </TableLink>
                ) : (
                  instanceName
                )
              }
            />
            <DetailField label="Created" value={formatDateTime(operation.created_at)} />
            <DetailField label="Updated" value={formatDateTime(operation.updated_at)} />
            <DetailField label="Status Code" value={operation.status_code} />
            <DetailField label="Cancellable" value={operation.may_cancel ? "Yes" : "No"} />
          </DetailGrid>
        </DetailSection>

        {Object.keys(operation.metadata ?? {}).length > 0 && (
          <DetailSection
            title="Metadata"
            description="What the cluster reports about this operation's progress."
          >
            <KeyValueList entries={operation.metadata as Record<string, string>} />
          </DetailSection>
        )}
      </div>
    </RecordDetailDialog>
  );
}
