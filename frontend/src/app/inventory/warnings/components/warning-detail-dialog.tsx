"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TableLink } from "@/components/ui/table-link";
import { IntentLink } from "@/components/navigation/route-intent";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { formatDateTime, formatLabel } from "@/lib/utils";
import { inventoryLinks } from "@/lib/inventory-links";
import {
  warningSeverityDisplay,
  warningStatusDisplay,
} from "./warning-severity";

// One dialog for all three warning tabs.
//
// Only the first tab ever had a drill-down, and its card told every reader to click a row while
// the Certificates and Offline Nodes tabs did nothing at all. They also each dropped the field
// that matters most: a certificate row never showed its cluster id in full, and an offline node
// clipped the connection error to two lines behind a tooltip, which is the one place the actual
// reason lives.
export type WarningKind = "platform" | "certificate" | "node";

export interface WarningRecord {
  kind: WarningKind;
  record: any;
}

export function WarningDetailDialog({
  selected,
  open,
  onOpenChange,
}: {
  selected: WarningRecord | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  if (!selected) return null;
  const { kind, record } = selected;

  if (kind === "platform") {
    const severity = warningSeverityDisplay(record.severity);
    const status = warningStatusDisplay(record.status);
    return (
      <RecordDetailDialog
        open={open}
        onOpenChange={onOpenChange}
        title={formatLabel(record.type)}
        // The uuid is what correlates this with the cluster's own warning list, and the table
        // has never shown it.
        recordId={record.uuid ?? ""}
        recordIdLabel="Warning"
        status={<Badge variant={severity.tone}>{severity.label}</Badge>}
        size="lg"
      >
        <div className="space-y-4">
          <DetailSection title="What Happened">
            <DetailGrid>
              <DetailField label="Message" value={record.last_message} span="full" />
            </DetailGrid>
          </DetailSection>
          <DetailSection title="Where and When">
            <DetailGrid cols={3}>
              {/* note: location and source_node are different things, per the type comment. */}
              <DetailField label="Location" value={record.location} />
              <DetailField label="Source Node" value={record.source_node} />
              <DetailField label="Project" value={record.project} />
              <DetailField label="Status" value={status.label} />
              <DetailField label="Seen" value={record.count?.toLocaleString?.() ?? record.count} />
              <DetailField label="First Seen" value={formatDateTime(record.first_seen_at)} />
              <DetailField label="Last Seen" value={formatDateTime(record.last_seen_at)} />
            </DetailGrid>
          </DetailSection>
        </div>
      </RecordDetailDialog>
    );
  }

  if (kind === "certificate") {
    const expired = record.warning_type === "cert_expired" || record.days_until_expiry <= 0;
    const severity = warningSeverityDisplay(record.severity);
    return (
      <RecordDetailDialog
        open={open}
        onOpenChange={onOpenChange}
        title={record.display_name || record.cluster_id}
        recordId={record.cluster_id}
        recordIdLabel="Cluster"
        status={<Badge variant={expired ? "destructive" : "secondary"}>{expired ? "Expired" : "Expiring"}</Badge>}
        size="md"
        footer={
          <Button asChild size="sm">
            <IntentLink href={inventoryLinks.certificates({ q: record.cluster_id })}>
              Renew certificate
            </IntentLink>
          </Button>
        }
      >
        <DetailGrid cols={2}>
          <DetailField label="Severity" value={severity.label} />
          <DetailField label="Warning Type" value={formatLabel(record.warning_type ?? "")} />
          <DetailField
            label="Days Until Expiry"
            value={record.days_until_expiry <= 0 ? "Expired" : `${record.days_until_expiry} days`}
          />
          <DetailField label="Expires at" value={formatDateTime(record.expires_at)} />
        </DetailGrid>
      </RecordDetailDialog>
    );
  }

  const severity = warningSeverityDisplay(record.severity || "critical");
  return (
    <RecordDetailDialog
      open={open}
      onOpenChange={onOpenChange}
      title={record.display_name || record.cluster_id}
      recordId={record.cluster_id}
      recordIdLabel="Cluster"
      status={<Badge variant={severity.tone}>{severity.label}</Badge>}
      // The connection error, in full. The table clips it to two lines behind a tooltip, which
      // no touch or keyboard user opens.
      error={record.message || "Connection failed"}
      size="md"
      footer={
        <Button asChild size="sm" variant="outline">
          <IntentLink href={inventoryLinks.clusters({ q: record.cluster_id })}>
            Open in Clusters
          </IntentLink>
        </Button>
      }
    >
      <DetailGrid>
        <DetailField
          label="Endpoint"
          span="full"
          value={
            record.endpoint ? (
              <TableLink href={record.endpoint} external title="Open the hypervisor endpoint">
                {record.endpoint}
              </TableLink>
            ) : undefined
          }
        />
      </DetailGrid>
    </RecordDetailDialog>
  );
}
