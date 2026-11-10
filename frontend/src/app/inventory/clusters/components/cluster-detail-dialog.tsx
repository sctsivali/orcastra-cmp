"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { EmptyState } from "@/components/ui/empty-state";
import { TableLink } from "@/components/ui/table-link";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { KeyValueList } from "@/components/used-by/panels/config-panel";
import { formatContinent, formatDateTime } from "@/lib/utils";
import { isWholeCountry } from "@/lib/regions/place";
import { MapPin } from "lucide-react";
import type { ClusterInfo } from "@/types/api";
import { ClusterCertificatePanel } from "./cluster-certificate-panel";

// What the clusters table receives but has no room for: the error reason, check and
// registration times, location and certificate.
//
// `merchant_id` is rendered for every role. It carries no authorization meaning (scope comes
// from organization_clusters bindings); it is a free-text label captured at registration and
// stored in the cluster's Vault metadata (services/lxd_certs/storage.py). The list it comes from
// is org-scoped by apply_cluster_filter (routers/cluster_registration.py, list_clusters), so a
// partner only sees its own organization's value. Gating it on Admin would add the only role
// branch to these dialogs, which keep fixture-based tests sufficient for all three roles.
export function ClusterDetailDialog({
  cluster,
  open,
  onOpenChange,
  footer,
}: {
  cluster: ClusterInfo | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  footer?: React.ReactNode;
}) {
  if (!cluster) return null;

  const location = cluster.location;
  const wholeCountry = location ? isWholeCountry(location) : false;
  const statusMeta = clusterStatusMeta(cluster.status);

  return (
    <RecordDetailDialog
      open={open}
      onOpenChange={onOpenChange}
      title={cluster.display_name}
      recordId={cluster.cluster_id}
      recordIdLabel="Cluster"
      subtitle={
        // A Tenant receives no endpoint; the cluster id above already names the record.
        cluster.endpoint ? (
          <TableLink href={cluster.endpoint} external title="Open the hypervisor endpoint">
            {cluster.endpoint}
          </TableLink>
        ) : undefined
      }
      status={
        <Badge variant={statusMeta.tone}>{statusMeta.label}</Badge>
      }
      // The reason a cluster is unreachable, which the table has no room for.
      error={cluster.error_message || null}
      footer={footer}
      tabs={[
        {
          value: "overview",
          label: "Overview",
          content: (
            <div className="space-y-4">
              <DetailSection title="Registration">
                <DetailGrid cols={3}>
                  <DetailField label="Zone" value={cluster.zone} />
                  {/* Free text supplied at registration, so it is often absent or stale. The
                      shared placeholder says so instead of rendering an empty field. */}
                  <DetailField label="Merchant ID" value={cluster.merchant_id} mono copyable />
                  <DetailField label="Registered" value={formatDateTime(cluster.created_at)} />
                  <DetailField label="Last Checked" value={formatDateTime(cluster.last_checked)} />
                </DetailGrid>
              </DetailSection>
              {Object.keys(cluster.labels ?? {}).length > 0 && (
                <DetailSection title="Labels">
                  <KeyValueList entries={cluster.labels} />
                </DetailSection>
              )}
            </div>
          ),
        },
        {
          value: "location",
          label: "Location",
          content: location ? (
            <DetailGrid cols={3}>
              <DetailField label="Region" value={location.region_name} />
              {location.province && <DetailField label="Province" value={location.province} />}
              <DetailField label="Country" value={location.country_name} />
              {/* A country stands for all of it: a stored city there is at most a capital from
                  an older catalogue, never where the cluster runs. */}
              {wholeCountry ? (
                <DetailField label="Placement" value="Whole country, exact site not recorded" />
              ) : (
                <DetailField label="City" value={location.city} />
              )}
              <DetailField label="Continent" value={formatContinent(location.continent)} />
              <DetailField label="Timezone" value={location.timezone} />
              <DetailField
                label={wholeCountry ? "Map point" : "Coordinates"}
                value={
                  Number.isFinite(location.latitude) && Number.isFinite(location.longitude)
                    ? `${location.latitude}, ${location.longitude}`
                    : undefined
                }
                mono
                copyable
              />
              <DetailField label="Facility" value={location.facility_name} />
              <DetailField label="Provider" value={location.facility_provider} />
            </DetailGrid>
          ) : (
            <EmptyState
              icon={MapPin}
              title="No location set"
              description="This cluster has no geographic location recorded, so it cannot be placed on the map."
            />
          ),
        },
        {
          value: "certificate",
          label: "Certificate",
          content: <ClusterCertificatePanel clusterId={cluster.cluster_id} />,
        },
      ]}
    />
  );
}
