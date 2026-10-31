"use client";

import { Loader2 } from "lucide-react";

import { CopyableText } from "@/components/detail/copyable-text";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { TableLink } from "@/components/ui/table-link";
import { useCertificate } from "@/hooks/use-lxd";
import { inventoryLinks } from "@/lib/inventory-links";
import { formatDateTime, plural } from "@/lib/utils";

/**
 * The cluster's client certificate as the certificate itself describes it.
 *
 * note: this tab used to show `has_certificate`, which is true on every path that reaches it, and a
 * common name read from metadata that never stores one, so it said "Yes" and nothing. It now reads
 * the certificate, and only once the tab is opened: Radix unmounts an inactive tab's content.
 */
export function ClusterCertificatePanel({ clusterId }: { clusterId: string }) {
  const { data, isLoading, isError } = useCertificate(clusterId);
  const open = (data?.superseded ?? []).filter((e) => e.state === "pending" || e.state === "failed").length;

  return (
    <DetailSection
      title="Trust"
      description="The certificate this dashboard presents to the cluster. Renewal lives on the certificates page."
    >
      {isLoading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Reading the certificate
        </p>
      ) : (
        <DetailGrid cols={2}>
          <DetailField
            label="Fingerprint"
            span="full"
            value={
              data?.fingerprint ? (
                <CopyableText value={data.fingerprint} label={`certificate fingerprint for ${clusterId}`} />
              ) : undefined
            }
          />
          <DetailField label="Common Name" value={data?.common_name} mono />
          <DetailField label="Issuer" value={data?.issuer} />
          <DetailField label="Expires" value={data?.expires_at ? formatDateTime(data.expires_at) : undefined} />
          <DetailField
            label="Pending Renewal"
            value={
              data?.pending_fingerprint ? (
                <CopyableText value={data.pending_fingerprint} label={`pending certificate fingerprint for ${clusterId}`} />
              ) : data ? "None" : undefined
            }
          />
          {open > 0 && (
            <DetailField
              label="Old Trust Entries"
              value={`${plural(open, "replaced certificate is", "replaced certificates are")} still trusted by the cluster`}
              span="full"
            />
          )}
          {isError && (
            <DetailField label="Certificate" value="The certificate could not be read." span="full" />
          )}
          <DetailField
            label="Certificates Page"
            span="full"
            value={
              <TableLink
                href={inventoryLinks.certificates({ q: clusterId })}
                title="Open this cluster in Certificates"
              >
                Open in Certificates
              </TableLink>
            }
          />
        </DetailGrid>
      )}
    </DetailSection>
  );
}
