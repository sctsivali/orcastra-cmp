"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { TableLink } from "@/components/ui/table-link";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { TagBadge } from "@/components/tag-manager";
import { formatDateTime, formatInstanceType } from "@/lib/utils";
import { instanceStatusDisplay } from "@/lib/instance-status";
import type { VMInfo } from "@/types/api";
import { inventoryLinks } from "@/lib/inventory-links";
import { NodeLink } from "@/components/ui/node-link";
import { usePermissions } from "@/hooks/use-permissions";

// What the row already carries: description, architecture, project, timestamps, profiles, tags,
// the cluster member, and the image the instance came from.
export function InstanceOverviewTab({ instance }: { instance: VMInfo }) {
  const baseImage = instance.config?.["volatile.base_image"];
  const statusDisplay = instanceStatusDisplay(instance.status);
  // The cluster member is host detail a Tenant does not receive.
  const { isPartner } = usePermissions();

  return (
    <div className="space-y-4">
      <DetailSection title="Identity">
        <DetailGrid cols={3}>
          <DetailField label="Type" value={formatInstanceType(instance.type)} />
          {/* Raw: formatLabel would render x86_64 as "X86 64". */}
          <DetailField label="Architecture" value={instance.architecture} mono />
          <DetailField
            label="Status"
            value={<Badge variant={statusDisplay.tone}>{statusDisplay.label}</Badge>}
          />
          <DetailField label="Project" value={instance.project || "default"} />
          <DetailField label="Cluster" value={<NodeLink clusterId={instance.host} />} />
          {isPartner ? <DetailField label="Cluster Member" value={instance.location} /> : null}
          <DetailField label="Description" value={instance.description} span="full" />
        </DetailGrid>
      </DetailSection>

      <DetailSection title="Lifecycle">
        <DetailGrid cols={3}>
          <DetailField label="Created" value={formatDateTime(instance.created_at)} />
          <DetailField label="Last Used" value={formatDateTime(instance.last_used_at)} />
          {/* The whole fingerprint, wrapped rather than cut. */}
          <DetailField
            label="Base Image"
            mono
            value={
              baseImage ? (
                <TableLink
                  wrap
                  href={inventoryLinks.images({ q: baseImage, node: instance.host })}
                  title="Open this image in Images"
                >
                  {baseImage}
                </TableLink>
              ) : undefined
            }
          />
        </DetailGrid>
      </DetailSection>

      <DetailSection title="Applied">
        <DetailGrid cols={2}>
          <DetailField
            label="Profiles"
            value={
              instance.profiles?.length ? (
                <span className="flex flex-wrap gap-1.5">
                  {instance.profiles.map((profile) => (
                    <TableLink
                      key={profile}
                      href={inventoryLinks.profiles({ q: profile, node: instance.host })}
                      title={`Open ${profile} in Profiles`}
                    >
                      {profile}
                    </TableLink>
                  ))}
                </span>
              ) : undefined
            }
          />
          <DetailField
            label="Tags"
            value={
              instance.tags?.length ? (
                <span className="flex flex-wrap gap-1.5">
                  {instance.tags.map((tag) => (
                    <TagBadge key={tag} tag={tag} size="sm" />
                  ))}
                </span>
              ) : undefined
            }
          />
        </DetailGrid>
      </DetailSection>
    </div>
  );
}
