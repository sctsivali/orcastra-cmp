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
import { MapPin, Loader2 } from "lucide-react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api } from "@/lib/api";
import { useToast } from "@/hooks/use-toast";
import type { ClusterInfo, Organization } from "@/types/api";
import { inventoryLinks } from "@/lib/inventory-links";

// Everything the clusters table already receives and shows none of.
//
// `error_message`, `last_checked`, `certificate_common_name`, `has_certificate` and `created_at`
// are all in the list payload and none of them reach the screen, so an operator looking at a
// cluster in an error state can see that it is unhappy and not why. `location` is the starkest:
// the full GeoLocation arrives and is used only as a boolean tint on a button.
//
// note: `merchant_id` IS rendered, and the reasoning is worth keeping because the cautious
// instinct points the other way. It carries no authorization meaning: it appears nowhere in
// app/core or db_models, and scope comes from organization_clusters bindings instead. It is a
// free-text label captured at registration and stored in the cluster's Vault metadata
// (services/lxd_cert_manager.py:58). Meanwhile the list it comes from is org-scoped by
// apply_cluster_filter (routers/cluster_registration.py:636), so a partner only ever sees
// clusters bound to their own organization and the value shown is their own. Hiding a
// non-secret from the person who owns it protects nothing and costs them a support ticket when
// they need to correlate a cluster with its registration.
//
// The rejected alternative is worth naming too: gating it on Admin would put the only role
// branch inside these dialog components, and it is the absence of role branches that makes
// fixture-based tests sufficient for all three roles. That is a large price for hiding
// something that is not sensitive.
export function ClusterDetailDialog({
  cluster,
  open,
  onOpenChange,
  footer,
  onOrganizationChange,
}: {
  cluster: ClusterInfo | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  footer?: React.ReactNode;
  onOrganizationChange?: (organization_id: number | null, organization_name: string | null) => void;
}) {
  if (!cluster) return null;

  const location = cluster.location;
  const statusMeta = clusterStatusMeta(cluster.status);

  return (
    <RecordDetailDialog
      open={open}
      onOpenChange={onOpenChange}
      title={cluster.display_name}
      recordId={cluster.cluster_id}
      recordIdLabel="Cluster"
      subtitle={
        <TableLink href={cluster.endpoint} external title="Open the hypervisor endpoint">
          {cluster.endpoint}
        </TableLink>
      }
      status={
        <Badge variant={statusMeta.tone}>{statusMeta.label}</Badge>
      }
      // The reason a cluster is unreachable, which the table has never had room for.
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
                      shared placeholder says so rather than rendering an empty field. */}
                  <DetailField label="Merchant ID" value={cluster.merchant_id} mono copyable />
                  <ClusterOrganizationField
                    cluster={cluster}
                    onOrganizationChange={onOrganizationChange}
                  />
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
              <DetailField label="Country" value={location.country_name} />
              <DetailField label="City" value={location.city} />
              <DetailField label="Continent" value={formatContinent(location.continent)} />
              <DetailField label="Timezone" value={location.timezone} />
              <DetailField
                label="Coordinates"
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
          content: (
            <DetailSection
              title="Trust"
              description="Expiry and renewal live on the certificates page; this is what the registration knows."
            >
              <DetailGrid cols={2}>
                <DetailField label="Certificate Present" value={cluster.has_certificate ? "Yes" : "No"} />
                <DetailField label="Common Name" value={cluster.certificate_common_name} mono />
                <DetailField
                  label="Certificates Page"
                  span="full"
                  value={
                    <TableLink
                      href={inventoryLinks.certificates({ q: cluster.cluster_id })}
                      title="Open this cluster in Certificates"
                    >
                      Open in Certificates
                    </TableLink>
                  }
                />
              </DetailGrid>
            </DetailSection>
          ),
        },
      ]}
    />
  );
}


function ClusterOrganizationField({
  cluster,
  onOrganizationChange,
}: {
  cluster: ClusterInfo;
  onOrganizationChange?: (organization_id: number | null, organization_name: string | null) => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const current = cluster.organization_id != null ? String(cluster.organization_id) : "__none__";
  const [selected, setSelected] = React.useState(current);

  React.useEffect(() => {
    setSelected(cluster.organization_id != null ? String(cluster.organization_id) : "__none__");
  }, [cluster.cluster_id, cluster.organization_id]);

  const { data: orgs } = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.organizations.list(),
  });

  const mutation = useMutation({
    mutationFn: (organizationId: number | null) =>
      api.organizations.reassignCluster(cluster.cluster_id, organizationId),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      queryClient.invalidateQueries({ queryKey: ["organizations"] });
      onOrganizationChange?.(result.organization_id, result.organization_name);
      toast({ title: "Organization updated", description: "Cluster organization binding saved." });
    },
    onError: (err: Error) => {
      toast({ title: "Failed to update organization", description: err.message, variant: "destructive" });
    },
  });

  const dirty = selected !== current;

  return (
    <div className="min-w-0 sm:col-span-2">
      <dt className="text-xs text-muted-foreground">Organization</dt>
      <dd className="mt-0.5 flex items-center gap-2">
        <Select value={selected} onValueChange={setSelected}>
          <SelectTrigger className="h-8 text-sm">
            <SelectValue placeholder="None (unbound)" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__none__">None (unbound)</SelectItem>
            {(orgs || []).map((org: Organization) => (
              <SelectItem key={org.id} value={String(org.id)}>
                {org.name} ({org.id})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          size="sm"
          className="h-8"
          disabled={!dirty || mutation.isPending}
          onClick={() => mutation.mutate(selected === "__none__" ? null : Number(selected))}
        >
          {mutation.isPending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
          Apply
        </Button>
      </dd>
    </div>
  );
}
