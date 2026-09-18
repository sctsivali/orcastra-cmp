"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { IntentLink } from "@/components/navigation/route-intent";
import { TableLink } from "@/components/ui/table-link";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { formatBytes, formatDateTime, formatLabel } from "@/lib/utils";
import { inventoryLinks } from "@/lib/inventory-links";
import { NodeLink } from "@/components/ui/node-link";

// Neither sessions table had anything clickable, so a session id, the one value that correlates
// a row with the console audit log, was unreachable from the UI entirely.
export type SessionKind = "active" | "history";

export interface SessionRecord {
  kind: SessionKind;
  record: any;
}

export function SessionDetailDialog({
  selected,
  open,
  onOpenChange,
}: {
  selected: SessionRecord | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  // important: usePermissions, NOT useRequireRole. useRequireRole is a page-level GATE: it
  // redirects to "/" on a positive denial. Calling it here ran it on every render of this
  // dialog, including while closed, so every tenant who opened /inventory/sessions was thrown
  // to the home page and the sessions list became unreachable for the exact audience it is
  // built for. A display decision needs a question, not a gate.
  const { hasRole } = usePermissions();
  const isPartner = hasRole(Role.PARTNER);

  if (!selected) return null;
  const { kind, record } = selected;
  const isActive = kind === "active";

  const download = isActive ? record.bandwidth?.download_bytes : record.download_bytes;
  const upload = isActive ? record.bandwidth?.upload_bytes : record.upload_bytes;
  const started = isActive
    ? new Date((record.started_at ?? 0) * 1000).toISOString()
    : record.created_at;

  return (
    <RecordDetailDialog
      open={open}
      onOpenChange={onOpenChange}
      title={record.instance_name}
      recordId={record.session_id ?? String(record.id ?? "")}
      recordIdLabel="Session"
      subtitle={<><NodeLink clusterId={record.host} /> / {record.project}</>}
      status={<Badge variant={isActive ? "default" : "secondary"}>{isActive ? "Active" : formatLabel(record.event_type ?? "")}</Badge>}
      size="md"
      footer={
        isPartner ? (
          <Button asChild size="sm" variant="outline">
            <IntentLink href={inventoryLinks.operations({ q: record.instance_name, node: record.host })}>
              Manage in Operations
            </IntentLink>
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-4">
        <DetailSection title="Connection">
          <DetailGrid cols={3}>
            <DetailField label="Protocol" value={formatLabel(record.protocol ?? "")} />
            <DetailField
              label="Instance"
              value={
                <TableLink
                  href={inventoryLinks.instances({ q: record.instance_name, project: record.project ?? "default", host: record.host })}
                  title={`Open ${record.instance_name} in Instances`}
                >
                  {record.instance_name}
                </TableLink>
              }
            />
            <DetailField label="Started" value={formatDateTime(started)} />
          </DetailGrid>
        </DetailSection>

        <DetailSection title="Transfer">
          <DetailGrid cols={2}>
            <DetailField label="Downloaded" value={download != null ? formatBytes(download) : undefined} />
            <DetailField label="Uploaded" value={upload != null ? formatBytes(upload) : undefined} />
          </DetailGrid>
        </DetailSection>

        {(record.user_email || record.user_role || record.is_break_glass) && (
          <DetailSection title="Who">
            <DetailGrid cols={3}>
              <DetailField label="User" value={record.user_email} />
              <DetailField label="Role" value={record.user_role ? formatLabel(record.user_role) : undefined} />
              <DetailField
                label="Break-Glass"
                value={record.is_break_glass ? <Badge variant="destructive">Yes</Badge> : "No"}
              />
            </DetailGrid>
          </DetailSection>
        )}
      </div>
    </RecordDetailDialog>
  );
}
